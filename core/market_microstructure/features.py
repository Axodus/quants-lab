"""Causal features only. Forward labels live exclusively in markout.py."""
from collections import Counter, deque
from dataclasses import replace
from decimal import Decimal as D
from .events import BookSnapshotEvent, BookDeltaEvent, TradeEvent, MarkPriceEvent
from .models import FeatureConfig, FeatureProvenance, MicrostructureFeatureSnapshot, digest
from .horizons import BoundedWindow
from .ofi import ofi
from .microprice import calculate_microprice, microprice_fields
from .provenance import implementation_hash

ZERO = D(0)


def ratio(a, b):
    return a / b if b else None


def percentile(value, observations):
    return D(sum(x <= value for x in observations))/len(observations) if observations else None


class MicrostructureFeatureEngine:
    def __init__(self, book, dataset_hash, config=None):
        if len(dataset_hash) != 64 or any(c not in '0123456789abcdef' for c in dataset_hash):
            raise ValueError('SHA256 dataset identity required')
        self.book, self.dataset_hash = book, dataset_hash
        self.config = config or FeatureConfig()
        if book.depth < self.config.depth:
            raise ValueError('book view shall cover configured depth')
        self.code_hash = implementation_hash()
        self.counters = Counter()
        self.time = None
        self.mark_price_context = None
        self.emitted_until = None
        self.chain = digest({'dataset':dataset_hash,'instrument':book.spec.canonical_hash()})
        self.resolution = 1
        self._clear_windows()
        self.seen = {}
        self.seen_queue = deque()

    def _clear_windows(self):
        c = self.config
        duration = max(c.context_ms, 2*max(c.horizons))
        self.flow = BoundedWindow(duration, c.max_window_events)
        self.trades = BoundedWindow(duration, c.max_window_events)
        self.states = BoundedWindow(duration, c.max_window_events)
        self.removals = {}
        self.sweep_trades = {'BUY':deque(), 'SELL':deque()}
        self.sweeps = BoundedWindow(duration,c.max_window_events)
        self.segment_start = None

    def _dedup(self, event, t):
        identity = None
        if isinstance(event, TradeEvent) and event.trade_id is not None:
            identity = ('trade',event.trade_id)
        elif isinstance(event, BookDeltaEvent):
            identity = ('delta',event.final_update_id)
        if identity is None:
            return False
        value = digest(event)
        while self.seen_queue and self.seen_queue[0][0] < t-self.config.context_ms:
            _, key = self.seen_queue.popleft()
            self.seen.pop(key,None)
        if identity in self.seen:
            if self.seen[identity] != value:
                self.book.invalidate('INVALID_SEQUENCE')
                raise ValueError('conflicting duplicate identity')
            self.counters['duplicate_events'] += 1
            return True
        if len(self.seen) >= self.config.max_window_events:
            raise ValueError('DEDUP_CAPACITY_EXCEEDED')
        self.seen[identity] = value
        self.seen_queue.append((t,identity))
        return False

    def process(self, event):
        self.counters['events_received'] += 1
        spec = self.book.spec
        if (event.symbol,event.venue,event.market_type) != (spec.symbol,spec.venue,spec.market_type):
            self.counters['events_rejected'] += 1
            raise ValueError('cross-instrument event rejected')
        t = max(event.event_time,event.receive_time or event.event_time)
        if isinstance(event, MarkPriceEvent):
            # Separate causal context: does not advance L2/tape feature time,
            # modify the book, alter feature windows, or trigger snapshot emission.
            previous = self.mark_price_context
            if ((previous is not None and t < max(previous.event_time, previous.receive_time or previous.event_time))
                    or (self.time is not None and t < self.time)
                    or (self.emitted_until is not None and t < self.emitted_until)):
                self.counters['events_rejected'] += 1
                raise ValueError('out-of-order mark price context')
            self.mark_price_context = event
            self.counters['mark_price_events'] += 1
            self.counters['events_processed'] += 1
            return True
        if self.emitted_until is not None and t < self.emitted_until:
            self.book.invalidate('INVALID_SEQUENCE')
            self.counters['events_rejected'] += 1
            raise ValueError('event precedes already emitted feature watermark')
        if self._dedup(event,t):
            return False
        if self.time is not None and t < self.time:
            # Native sequence authority permits the known snapshot-bridge clock overlap.
            bridge = isinstance(event,BookDeltaEvent) and self.book.state.sequence is not None and event.first_update_id <= self.book.state.sequence <= event.final_update_id and self.time-t <= 1
            if not bridge:
                self.book.invalidate('INVALID_SEQUENCE')
                self.counters['events_rejected'] += 1
                raise ValueError('out-of-order event; no silent reorder')
            t = self.time
        self.time = t
        self.chain = digest((self.chain,event.event_type,event))
        self.resolution = max(self.resolution,event.effective_resolution_ms)
        for window in (self.flow,self.trades,self.states,self.sweeps):
            window.prune(t)
        if self.book.state.timestamp is not None and t-self.book.state.timestamp > self.config.stale_after_ms:
            if self.book.state.validity_state != 'STALE':
                self.counters['stale_states'] += 1
            self.book.invalidate('STALE')
        if isinstance(event,(BookSnapshotEvent,BookDeltaEvent)):
            old = self.book.state
            snapshot = isinstance(event,BookSnapshotEvent)
            if snapshot:
                self._clear_windows()
            changes = self.book.apply(event)
            self.counters['book_events'] += 1
            state = self.book.state
            if state.validity_state != 'VALID':
                self.counters['events_rejected'] += 1
                key = {'INVALID_SEQUENCE':'sequence_gaps','INVALID_CROSSED':'crossed_books',
                       'INVALID_BOOTSTRAP':'bootstrap_failures','INVALID_EMPTY':'empty_books'}.get(state.validity_state,'invalid_books')
                self.counters[key] += 1
                self._clear_windows()
                return False
            if snapshot or old.validity_state == 'STALE':
                self._clear_windows()
                self.segment_start = t
            if self.segment_start is None:
                self.segment_start = t
            flow = {f'OFI_L{n}':ofi(old,state,n) if not snapshot and old.validity_state == 'VALID' else ZERO for n in (1,5,10)}
            for side in ('bid','ask'):
                flow.update({f'{side}_add':ZERO,f'{side}_remove':ZERO,f'{side}_replenished':ZERO,f'{side}_replenishment_count':0})
            # Removals are visible quantity reductions, NOT proven cancellations/trades.
            self.removals = {key:value for key,value in self.removals.items() if value[1] > t-max(self.config.horizons)}
            for side,p,change in changes:
                name = 'ask' if side else 'bid'
                key = (side,p)
                if change < 0:
                    flow[name+'_remove'] -= change
                    prior = self.removals.get(key,(ZERO,t))[0]
                    self.removals[key] = (prior-change,t)
                elif change > 0:
                    flow[name+'_add'] += change
                    removed = self.removals.get(key,(ZERO,t))[0]
                    restored = min(removed,change)
                    if restored:
                        flow[name+'_replenished'] += restored
                        flow[name+'_replenishment_count'] += 1
                        self.removals[key] = (removed-restored,t)
            if len(self.removals) > self.config.max_window_events:
                raise ValueError('REPLENISHMENT_CAPACITY_EXCEEDED')
            flow['source_ref'] = event.source_ref
            self.flow.append(t,flow)
            self.states.append(t,state)
        elif isinstance(event,TradeEvent):
            spec.price_to_ticks(event.price)
            spec.quantity_to_steps(event.quantity)
            self.counters['trade_events'] += 1
            if self.book.state.validity_state == 'VALID':
                self.trades.append(t,event)
                self._sweep(event,t)
        self.counters['events_processed'] += 1
        return True

    def derivatives_context(self, as_of):
        event = self.mark_price_context
        if event is None or max(event.event_time, event.receive_time or event.event_time) > as_of:
            return None
        return event

    def _sweep(self,event,t):
        if event.aggressor == 'UNKNOWN':
            return
        rows = self.sweep_trades[event.aggressor]
        while rows and rows[0][0] < t-self.config.sweep_max_duration_ms:
            rows.popleft()
        if rows and ((event.aggressor == 'BUY' and event.price < rows[-1][1].price) or
                     (event.aggressor == 'SELL' and event.price > rows[-1][1].price)):
            rows.clear()
        rows.append((t,event))
        if len(rows) > self.config.max_window_events:
            raise ValueError('SWEEP_CAPACITY_EXCEEDED')
        count = len({e.price for _,e in rows})
        if count >= self.config.sweep_min_levels:
            self.sweeps.append(t,{'side':event.aggressor,'levels_consumed_proxy':count,
                                 'volume':sum((e.quantity for _,e in rows),ZERO),'start_price':rows[0][1].price,
                                 'end_price':event.price,'duration_ms':t-rows[0][0],
                                 'provenance':'INFERRED_MONOTONE_TRADE_PRICE_CLUSTER'})
            rows.clear()  # non-overlapping clusters; volume is never counted twice

    def snapshot(self,horizon,timestamp=None):
        if horizon not in self.config.horizons:
            raise ValueError('unconfigured horizon')
        t = self.time if timestamp is None else timestamp
        if t is None or self.time is None or t < self.time:
            raise ValueError('cannot create retrospective features from current state')
        state = self.book.state
        if state.timestamp is None or t-state.timestamp > self.config.stale_after_ms:
            if self.book.state.validity_state != 'STALE':
                self.counters['stale_states'] += 1
            self.book.invalidate('STALE')
            state = self.book.state
        if state.validity_state != 'VALID':
            raise ValueError('invalid book: '+state.validity_state)
        if self.segment_start is None or t-self.segment_start < horizon:
            raise ValueError('HORIZON_WARMUP_REQUIRED')
        anchor = self.states.anchor(t-horizon)
        if anchor is None or t-horizon-anchor.timestamp > self.config.stale_after_ms:
            raise ValueError('valid causal horizon anchor unavailable')
        trades = self.trades.since(t,horizon)
        flows = self.flow.since(t,horizon)
        sec = D(horizon)/1000
        f = {'available_bid_levels':len(state.bid_levels),'available_ask_levels':len(state.ask_levels)}
        for n in (1,5,10):
            bd = sum((q for _,q in state.bid_levels[:n]),ZERO)
            ad = sum((q for _,q in state.ask_levels[:n]),ZERO)
            f.update({f'bid_depth_L{n}':bd,f'ask_depth_L{n}':ad,f'queue_imbalance_L{n}':ratio(bd,bd+ad),
                      f'signed_queue_imbalance_L{n}':ratio(bd-ad,bd+ad),
                      f'OFI_L{n}':sum((row[f'OFI_L{n}'] for row in flows),ZERO)})
            f[f'normalized_OFI_L{n}'] = ratio(f[f'OFI_L{n}'],bd+ad)
        wb = sum((q*w for (_,q),w in zip(state.bid_levels,self.config.weights)),ZERO)
        wa = sum((q*w for (_,q),w in zip(state.ask_levels,self.config.weights)),ZERO)
        micro = calculate_microprice(state)
        mid = state.mid_price
        f.update(weighted_bid_depth=wb,weighted_ask_depth=wa,weighted_book_pressure=wb-wa,
                 normalized_weighted_pressure=ratio(wb-wa,wb+wa),mid=mid)
        f.update(microprice_fields(state))
        for side,name in (('BUY','buy'),('SELL','sell')):
            selected = [e for e in trades if e.aggressor == side]
            volume = sum((e.quantity for e in selected),ZERO)
            f.update({name+'_aggressive_volume':volume,name+'_trade_count':len(selected),
                      'average_'+name+'_trade_size':ratio(volume,D(len(selected)))})
        buy,sell = f['buy_aggressive_volume'],f['sell_aggressive_volume']
        all_volume = sum((e.quantity for e in trades),ZERO)
        signed = buy-sell
        previous = [e for time,e in self.trades.items if t-2*horizon < time <= t-horizon]
        prior_signed = sum((e.quantity*(1 if e.aggressor=='BUY' else -1 if e.aggressor=='SELL' else 0) for e in previous),ZERO)
        f.update(signed_aggressive_volume=signed,aggression_imbalance=ratio(signed,buy+sell),
                 unknown_aggressive_volume=all_volume-buy-sell,trade_rate=D(len(trades))/sec,
                 volume_rate=all_volume/sec,signed_volume_rate=signed/sec,trade_velocity=D(len(trades))/sec,
                 volume_velocity=all_volume/sec,signed_volume_velocity=signed/sec,
                 trade_acceleration=(D(len(trades)-len(previous))/sec**2) if t-self.segment_start>=2*horizon else None,
                 signed_volume_acceleration=(signed-prior_signed)/sec**2 if t-self.segment_start>=2*horizon else None)
        for side in ('bid','ask'):
            add,remove,replenished = (sum((row[side+'_'+kind] for row in flows),ZERO) for kind in ('add','remove','replenished'))
            f.update({side+'_add_rate':add/sec,side+'_cancel_rate':remove/sec,
                      'net_'+side+'_liquidity_change':add-remove,side+'_cancel_to_add_ratio':ratio(remove,add),
                      side+'_consumed_volume_proxy':remove,side+'_replenished_volume':replenished,
                      side+'_replenishment_ratio':ratio(replenished,remove),
                      side+'_replenishment_count':sum(row[side+'_replenishment_count'] for row in flows)})
        delta = mid-anchor.mid_price
        f.update(mid_change=delta,price_change=delta,microprice_change=(micro-calculate_microprice(anchor)) if micro is not None and calculate_microprice(anchor) is not None else None,
                 price_response_per_signed_volume=ratio(delta,signed),price_response_per_aggressive_volume=ratio(delta,buy+sell),
                 spread_absolute=state.spread,spread_bps=state.spread/mid*10000,spread_change=state.spread-anchor.spread,
                 total_depth=f['bid_depth_L10']+f['ask_depth_L10'],depth_imbalance=f['signed_queue_imbalance_L10'],
                 mid_return=delta/anchor.mid_price)
        old_depth = sum((q for _,q in anchor.bid_levels[:10]+anchor.ask_levels[:10]),ZERO)
        f['depth_change'] = f['total_depth']-old_depth
        path = [anchor]+self.states.since(t,horizon)
        returns = [(b.mid_price-a.mid_price)/a.mid_price for a,b in zip(path,path[1:])]
        f['realized_micro_volatility'] = sum((v*v for v in returns),ZERO).sqrt()
        context = self.states.since(t,self.config.context_ms)
        f['spread_percentile'] = percentile(state.spread,[x.spread for x in context])
        f['depth_percentile'] = percentile(f['total_depth'],[sum((q for _,q in x.bid_levels[:10]+x.ask_levels[:10]),ZERO) for x in context])
        counts = Counter((time//horizon) for time,_ in self.trades.items if t-self.config.context_ms < time <= t)
        complete = [D(counts.get(bucket,0))/sec for bucket in range(max((self.segment_start+horizon-1)//horizon,(t-self.config.context_ms)//horizon+1),t//horizon)]
        f['trade_rate_percentile'] = percentile(f['trade_rate'],complete)
        sweeps = self.sweeps.since(t,horizon)
        for side,name,other in (('BUY','buy','ask'),('SELL','sell','bid')):
            f[name+'_sweep_score'] = sum((x['volume']*(x['levels_consumed_proxy']-1) for x in sweeps if x['side']==side),ZERO)
            restored = f[other+'_replenished_volume']
            f[name+'_absorption_score'] = f[name+'_aggressive_volume']*min(D(1),ratio(restored,f[other+'_consumed_volume_proxy']) or ZERO)/(1+abs(delta)/self.book.spec.tick_size)
        refs = tuple(sorted({e.source_ref.split(':row')[0] for e in trades} | {row['source_ref'].split(':row')[0] for row in flows}))
        self.emitted_until = max(t,self.emitted_until or t)
        provenance = FeatureProvenance(self.dataset_hash,self.book.spec.canonical_hash(),self.code_hash,digest(self.config),
                                      self.chain,refs,t-horizon,t,self.resolution)
        self.counters['feature_snapshots'] += 1
        self.counters[f'horizon_{horizon}'] += 1
        return MicrostructureFeatureSnapshot(self.book.spec.symbol,self.book.spec.venue,self.book.spec.market_type,
                                             t,state.sequence,horizon,digest((state,self.chain)),provenance,f)
