"""Streaming provider normalization, event atomicity and causal availability merge."""
from dataclasses import replace
from decimal import Decimal as D
from heapq import merge
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
import pyarrow.compute as pc
from research_notebooks.orderflow_backtest.parquet_orderflow_frames import FixedPointCausalParquetFrameBuilder
from .events import BookSnapshotEvent, BookDeltaEvent, TradeEvent, MarkPriceEvent


FIELDS = ('event_time','transaction_time','event_type','first_update_id','final_update_id','prev_final_update_id','last_update_id')


def receive_ms(value):
    """Known provider epoch magnitudes: nanoseconds, microseconds, milliseconds.

    Reject other units, preserving the raw value and chosen unit in source_ref.
    Epoch magnitude discrimination is limited to the provider's 2001..2100 era.
    """
    if value is None:
        return None, 'UNAVAILABLE'
    value = int(value)
    for scale, unit in ((1000000,'ns'),(1000,'us'),(1,'ms')):
        ms=value//scale
        if 978307200000 <= ms <= 4102444800000:
            return ms,unit
    raise ValueError('unsupported provider receive timestamp epoch/unit')


def availability(event):
    return max(event.event_time,event.receive_time or event.event_time)


def book_events(paths,spec,*,first_snapshot_only=False,duration_ms=None,batch_size=32768):
    """Group exchange events across partition/batch boundaries, retain physical order.

    first_snapshot_only selects the FIRST real snapshot encountered and then all
    subsequent updates; it never initializes earlier timestamps from that snapshot.
    """
    pending=None
    rows=[]
    refs=[]
    received=[]
    started=None
    last_availability=None
    seen_snapshot=not first_snapshot_only

    def event(key,levels,source_refs,recv):
        t,transaction,kind,first,final,prev,last=key
        times=[receive_ms(x)[0] for x in recv if x is not None]
        common=dict(venue=spec.venue,market_type=spec.market_type,symbol=spec.symbol,event_time=int(t),
                    transaction_time=int(transaction) if transaction is not None else None,
                    receive_time=max(times) if times else None,source_ref='|'.join(dict.fromkeys(source_refs)),levels=tuple(levels))
        if kind=='snapshot':
            return BookSnapshotEvent(**common,last_update_id=int(last))
        if kind!='update':
            raise ValueError('SCHEMA_DRIFT: unknown book event')
        return BookDeltaEvent(**common,first_update_id=int(first),final_update_id=int(final),prev_final_update_id=int(prev))

    for path in paths:
        path=Path(path)
        pf=pq.ParquetFile(path)
        required=set(FIELDS)|{'side','price','quantity','symbol','received_time'}
        if not required.issubset(pf.schema_arrow.names):
            raise ValueError('SCHEMA_DRIFT: missing L2 columns')
        offset=0
        for batch in pf.iter_batches(batch_size=batch_size,columns=sorted(required)):
            if not seen_snapshot and pending is None:
                mask = pc.equal(batch.column(batch.schema.get_field_index('event_type')), 'snapshot')
                indices = pc.indices_nonzero(mask).to_pylist()
                if not indices:
                    offset += len(batch)
                    continue
                offset += indices[0]
                batch = batch.slice(indices[0])
            data=batch.to_pydict()
            n=len(batch)
            # Arrow converts numeric columns in batches; mutation remains native.
            prices=FixedPointCausalParquetFrameBuilder._primitive_fixed(batch.column(batch.schema.get_field_index('price')),spec.price_scale)
            quantities=FixedPointCausalParquetFrameBuilder._primitive_fixed(batch.column(batch.schema.get_field_index('quantity')),spec.quantity_scale)
            boundaries=np.zeros(n,dtype=bool)
            if n:
                boundaries[0]=True
            for field in FIELDS:
                values=np.array(data[field],dtype=object)
                boundaries[1:] |= values[1:]!=values[:-1]
            starts=np.flatnonzero(boundaries)
            for i,j in zip(starts,np.append(starts[1:],n)):
                key=tuple(data[field][i] for field in FIELDS)
                if pending is not None and pending!=key:
                    e=event(pending,rows,refs,received)
                    if not seen_snapshot and isinstance(e,BookSnapshotEvent):
                        seen_snapshot=True
                    if seen_snapshot:
                        t=availability(e)
                        # Native canonical snapshot bridge allows one-ms exchange clock overlap.
                        # Availability is monotone; a bridge remains after its snapshot.
                        if last_availability is not None and t<last_availability:
                            if isinstance(e,BookDeltaEvent) and last_availability-t<=1:
                                e=replace(e,receive_time=last_availability)
                                t=last_availability
                            else:
                                raise ValueError('out-of-order provider availability')
                        started=t if started is None else started
                        if duration_ms is not None and t>started+duration_ms:
                            return
                        yield e
                        last_availability=t
                    rows=[];refs=[];received=[]
                pending=key
                if any(s!=spec.symbol for s in data['symbol'][i:j]):
                    raise ValueError('provider symbol mismatch')
                for k in range(i,j):
                    side=data['side'][k]
                    if side not in {'bid','ask'}:
                        raise ValueError('invalid provider side')
                    rows.append((side,spec.price_from_ticks(int(prices[k])),spec.quantity_from_steps(int(quantities[k]))))
                raw=data['received_time'][i]
                _,unit=receive_ms(raw)
                refs.append(f'{path}:rows:{offset+i}:{offset+j}:receive={raw}:{unit}')
                received.extend(data['received_time'][i:j])
                if len(rows)>1000000:
                    raise ValueError('logical event exceeds configured safety bound')
            offset+=n
    if pending is not None:
        e=event(pending,rows,refs,received)
        if seen_snapshot or isinstance(e,BookSnapshotEvent):
            if last_availability is not None and availability(e)<last_availability:
                raise ValueError('out-of-order final event')
            if started is None or duration_ms is None or availability(e)<=started+duration_ms:
                yield e


def trade_events(paths,spec,start,end,batch_size=32768):
    last=None
    for path in paths:
        offset=0
        for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size):
            data=batch.to_pydict()
            required={'received_time','event_time','symbol','trade_id','price','quantity','trade_time','is_buyer_maker'}
            if not required.issubset(data):
                raise ValueError('SCHEMA_DRIFT: missing tape columns')
            for i in range(len(batch)):
                if data['symbol'][i]!=spec.symbol or type(data['is_buyer_maker'][i]) is not bool:
                    raise ValueError('invalid tape identity/aggressor')
                recv,unit=receive_ms(data['received_time'][i])
                t=max(data['event_time'][i],recv or data['event_time'][i])
                if t<start:
                    continue
                if t>end:
                    return
                if last is not None and t<last:
                    raise ValueError('out-of-order tape availability')
                last=t
                yield TradeEvent(venue=spec.venue,market_type=spec.market_type,symbol=spec.symbol,
                                 event_time=int(data['event_time'][i]),transaction_time=int(data['trade_time'][i]),receive_time=recv,
                                 source_ref=f'{path}:row:{offset+i}:receive={data["received_time"][i]}:{unit}',
                                 price=D(data['price'][i]),quantity=D(data['quantity'][i]),
                                 aggressor='SELL' if data['is_buyer_maker'][i] else 'BUY',
                                 aggressor_provenance='OBSERVED',trade_id=str(data['trade_id'][i]))
            offset+=len(batch)


def causal_merge(book_stream,trade_stream):
    # Stable tie policy: book source order precedes tape at equal availability;
    # provider trade-ID order is retained (never time-sorted after reading).
    yield from merge(book_stream,trade_stream,key=availability)


def replay(engine,events,emit_every_ms=100):
    if emit_every_ms<=0:
        raise ValueError('positive snapshot cadence required')
    next_emit=None
    for event in events:
        engine.process(event)
        if isinstance(event, MarkPriceEvent):
            continue
        t=engine.time
        if next_emit is None:
            next_emit=t+emit_every_ms
        if t>=next_emit:
            next_emit=t+emit_every_ms
            if engine.book.state.validity_state=='VALID' and engine.segment_start is not None:
                for horizon in engine.config.horizons:
                    if t-engine.segment_start>=horizon:
                        try:
                            yield engine.snapshot(horizon)
                        except ValueError as exc:
                            if 'anchor unavailable' not in str(exc):
                                raise


def bootstrap_prefix(book, paths, data_root):
    """Native bulk warmup; ONLY complete pre-window partitions, no invented snapshot.

    Uses the builder's vector conversion and original ob_push batching, preserving
    pending logical events across every Arrow/file boundary.
    """
    import ctypes
    builder=FixedPointCausalParquetFrameBuilder(Path(data_root),symbol=book.spec.symbol,instrument_spec=book.spec,
                                              is_start_ms=0,is_end_ms=2**62,authorized_end_ms=2**62)
    columns=['event_time','transaction_time','event_type','first_update_id','final_update_id',
             'prev_final_update_id','last_update_id','side','price','quantity']
    started=False
    count=0
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(columns=columns,batch_size=250000):
            if not started:
                indices=pc.indices_nonzero(pc.equal(batch.column(batch.schema.get_field_index('event_type')),'snapshot')).to_pylist()
                if not indices:
                    continue
                batch=batch.slice(indices[0])
                started=True
            rows=builder._kernel_rows(batch)
            error=book.lib.ob_push(book.handle,rows.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)),len(rows))
            if error:
                book.invalidate('INVALID_SEQUENCE')
                raise ValueError(f'native bootstrap prefix failed: {error}')
            count+=len(rows)
    if not started:
        raise ValueError('BOOTSTRAP_UNAVAILABLE')
    error=book.lib.ob_finish(book.handle,0)
    if error:
        raise ValueError(f'native bootstrap finish failed: {error}')
    book.refresh()
    if book.state.validity_state!='VALID':
        raise ValueError('BOOTSTRAP_INVALID')
    return count
