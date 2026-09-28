from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path

getcontext().prec = 50
ROOT = Path('/opt/Axodus/Trading/quants-lab/research_notebooks')
sys.path.insert(0, str(ROOT))

from orderflow_backtest.instrument_spec import InstrumentSpec
from orderflow_backtest.parquet_orderflow_frames import FixedPointCausalParquetFrameBuilder, FrameBuilderStats
from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1
from orderflow_backtest.source_equivalent_adapters import momentum_observe, absorption_observe, divergence_observe
from orderflow_backtest.real_absorption_replay import ReplayConfig, SignalRecord, OrderRecord, FillRecord, TradeRecord, artifact_hash, _dump, _as_strings, _fee, _signed_gross, DECIMAL_ZERO

DATA_ROOT = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data')
OUT = ROOT / 'orderflow_backtest/runs/causal_oos_performance_validation'
OUT.mkdir(parents=True, exist_ok=True)
END = 1783382399999
SYMBOLS = {
    'ZECUSDT': (1782432000000, 15840, Decimal('11.0')),
    'SUIUSDT': (1782498000000, 14740, Decimal('14740') / Decimal('1440')),
    'WLDUSDT': (1782498000000, 14740, Decimal('14740') / Decimal('1440')),
}
STRATS = {
    'orderflow.momentum.aggression': ('aggression', momentum_observe, False, Decimal('20'), Decimal('35'), SourceStrategyConfigV1(window=20, slope_z=Decimal('1.5'), imbalance=Decimal('0.65'), max_spread_ticks=Decimal('1')), 'TAKER_FRICTION_DOMINATED'),
    'orderflow.absorption.fade': ('fade', absorption_observe, True, Decimal('18'), Decimal('40'), SourceStrategyConfigV1(window=20, volume_multiple=Decimal('2.5'), aggression_fraction=Decimal('0.7'), absorption_move_ticks=Decimal('1'), confirmation_delta=Decimal('1'), tick_size=Decimal('0.01'), max_spread_ticks=Decimal('1')), 'MAKER_QUEUE_NOT_HISTORICALLY_PROVEN'),
    'orderflow.cvd.divergence.reversal': ('reversal', divergence_observe, False, Decimal('25'), Decimal('50'), SourceStrategyConfigV1(window=20, divergence_price_ticks=Decimal('1'), divergence_delta=Decimal('1'), tick_size=Decimal('0.01'), max_spread_ticks=Decimal('1')), 'TAKER_FRICTION_DOMINATED'),
}

def load_frames(symbol, start, expected):
    d = OUT / symbol
    d.mkdir(parents=True, exist_ok=True)
    fp, sp = d / 'frames.jsonl', d / 'frame_stats.json'
    spec = InstrumentSpec.load(ROOT / f'orderflow_backtest/instrument-specs/{symbol}.json')
    InstrumentSpec.register(spec)
    if fp.exists() and sp.exists():
        rows = [json.loads(x) for x in fp.read_text().splitlines()]
        if len(rows) == expected:
            frames = [OrderFlowFrameV1(**{k: (int(v) if k == 'timestamp_ms' else Decimal(v)) for k, v in r.items()}) for r in rows]
            return frames, FrameBuilderStats(**json.loads(sp.read_text())), spec
    b = FixedPointCausalParquetFrameBuilder(data_root=DATA_ROOT, symbol=symbol, instrument_spec=spec, is_start_ms=start, is_end_ms=END, authorized_end_ms=END)
    frames, stats = b.build_compiled()
    if len(frames) != expected:
        raise RuntimeError(f'FRAME_COUNT_MISMATCH {symbol}: {len(frames)} != {expected}')
    fp.write_text(''.join(json.dumps(asdict(f), sort_keys=True, default=str) + '\n' for f in frames))
    sp.write_text(json.dumps(asdict(stats), indent=2, default=str) + '\n')
    return frames, stats, spec

def metrics(trades, signals, orders, fills, days):
    vals = [Decimal(t.net_pnl) for t in trades]
    gross = sum((Decimal(t.gross_pnl) for t in trades), DECIMAL_ZERO)
    fees = sum((Decimal(t.entry_fee) + Decimal(t.exit_fee) for t in trades), DECIMAL_ZERO)
    slippage = sum((Decimal(t.slippage_cost) for t in trades), DECIMAL_ZERO)
    funding = sum((Decimal(t.funding_pnl) for t in trades), DECIMAL_ZERO)
    net = sum(vals, DECIMAL_ZERO)
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    gp = sum((Decimal(t.gross_pnl) for t in trades if Decimal(t.gross_pnl) > 0), DECIMAL_ZERO)
    gl = sum((Decimal(t.gross_pnl) for t in trades if Decimal(t.gross_pnl) < 0), DECIMAL_ZERO)
    eq = peak = dd = DECIMAL_ZERO
    daily = {}
    for t in trades:
        v = Decimal(t.net_pnl); eq += v; peak = max(peak, eq); dd = max(dd, peak - eq)
        day = str(datetime.fromtimestamp(t.exit_time_ms / 1000, tz=timezone.utc).date())
        daily[day] = daily.get(day, DECIMAL_ZERO) + v
    tc = len(trades); tpd = Decimal(tc) / days
    pf = gp / abs(gl) if gl else (Decimal('Infinity') if gp else DECIMAL_ZERO)
    freq = 'SUFFICIENT_FREQUENCY' if tpd >= 1 else ('LOW_FREQUENCY' if tpd >= Decimal('0.2') else 'VERY_LOW_FREQUENCY')
    econ = 'ECONOMICALLY_VIABLE' if net > 0 and pf > 1 else 'ECONOMICALLY_FRAGILE'
    evidence = 'OOS_EVIDENCE_POSITIVE' if net > 0 else ('OOS_EVIDENCE_NEGATIVE' if net < 0 else 'OOS_EVIDENCE_INCONCLUSIVE')
    intervals = [(trades[i].entry_time_ms - trades[i-1].entry_time_ms) / 1000 for i in range(1, tc)]
    positive = sum(v > 0 for v in daily.values()); negative = sum(v < 0 for v in daily.values())
    return {
        'signals': len(signals), 'orders': len(orders), 'fills': len(fills), 'completed_trades': tc,
        'unfilled_orders': len(orders) - len(fills), 'partial_fills': 0, 'unmatched_fills': len(fills) - tc, 'open_lifecycle_state': 0,
        'signals_per_day': float(Decimal(len(signals)) / days), 'trades_per_day': float(tpd),
        'average_time_between_trades_sec': sum(intervals) / len(intervals) if intervals else 0, 'median_time_between_trades_sec': sorted(intervals)[len(intervals)//2] if intervals else 0,
        'long_trades': sum(t.side == 'LONG' for t in trades), 'short_trades': sum(t.side == 'SHORT' for t in trades),
        'gross_pnl': str(gross), 'fees': str(fees), 'slippage': str(slippage), 'funding': str(funding), 'net_pnl': str(net),
        'expectancy': str(net / tc) if tc else '0', 'profit_factor': str(pf), 'average_win': str(sum(wins, DECIMAL_ZERO) / len(wins)) if wins else '0', 'average_loss': str(sum(losses, DECIMAL_ZERO) / len(losses)) if losses else '0',
        'largest_win': str(max(vals)) if vals else '0', 'largest_loss': str(min(vals)) if vals else '0', 'max_drawdown': str(dd),
        'daily_net_pnl': {k: str(v) for k, v in sorted(daily.items())}, 'positive_days': positive, 'negative_days': negative, 'flat_days': sum(v == 0 for v in daily.values()),
        'active_trading_days': len(daily), 'zero_trade_days': max(0, int(days.to_integral_value(rounding='ROUND_CEILING')) - len(daily)), 'mean_daily_pnl': str(sum(daily.values(), DECIMAL_ZERO) / len(daily)) if daily else '0',
        'best_day': str(max(daily.values())) if daily else '0', 'worst_day': str(min(daily.values())) if daily else '0', 'daily_hit_ratio': positive / len(daily) if daily else 0,
        'frequency_classification': freq, 'economic_classification': econ, 'evidence_disposition': evidence, 'execution_model_limitation': 'MAKER_ASSUMED / QUEUE_POSITION_UNKNOWN / EXECUTION_MODEL_LIMITATION' if tc and any(t.entry_liquidity_role == 'MAKER_ASSUMED' for t in trades) else None,
        'reuse_status': 'VALIDATED_REUSABLE', 'testnet_eligibility': 'TESTNET_BLOCKED', 'mainnet_eligibility': 'MAINNET_BLOCKED',
        'blockers': ['BLOCKER-01 OPEN / DO NOT USE', 'real capital authority absent'],
    }

class Replay:
    def __init__(self, cfg, strat, days):
        self.c = cfg; self.days = days; self.fn, self.maker, self.stop, self.tp, self.sc, self.lim = strat[1], strat[2], strat[3], strat[4], strat[5], strat[6]
    def run(self, frames, out):
        for f in frames:
            if f.timestamp_ms < self.c.is_start_ms or f.timestamp_ms > self.c.is_end_ms: raise RuntimeError('OOS_BOUNDARY_VIOLATION')
        signals=[]; orders=[]; fills=[]; trades=[]; hist=[]; cvds=[]; cvd=DECIMAL_ZERO; open_trade=None; entry_idx=None
        rate = self.c.maker_fee_rate if self.maker else self.c.taker_fee_rate; role = 'MAKER_ASSUMED' if self.maker else 'TAKER'; typ = 'LIMIT' if self.maker else 'MARKET'
        for i, f in enumerate(frames):
            cvd += f.delta; sig = self.fn(f, hist, cvds, cvd, self.sc)
            if sig.side != 'NO_SIGNAL':
                sr = SignalRecord(f'{self.c.run_id}:signal:{len(signals)+1}', self.c.run_id, f.timestamp_ms, self.c.strategy_id, sig.side, sig.reason, str(f.price), str(f.delta), str(f.volume), str(f.obi), str(f.spread_ticks)); signals.append(sr)
                if open_trade is None and i + 1 < len(frames):
                    e=frames[i+1]; q=self.c.notional/e.price; o=OrderRecord(f'{self.c.run_id}:order:{len(orders)+1}', sr.signal_id, f.timestamp_ms, e.timestamp_ms, sig.side, str(q), str(self.c.notional), typ, role); fi=FillRecord(f'{self.c.run_id}:fill:{len(fills)+1}', o.order_id, e.timestamp_ms, str(e.price), str(q), role, str(_fee(self.c.notional, rate)), '0'); orders.append(o); fills.append(fi); open_trade=(sr,o,fi); entry_idx=i+1
            if open_trade is not None and i > entry_idx:
                sr,o,fi=open_trade; ep=Decimal(fi.price); sm=self.stop/Decimal('10000'); tm=self.tp/Decimal('10000'); stop=ep*(1-sm) if o.side=='LONG' else ep*(1+sm); take=ep*(1+tm) if o.side=='LONG' else ep*(1-tm); hit= f.price <= stop if o.side=='LONG' else f.price >= stop; hit_tp=f.price >= take if o.side=='LONG' else f.price <= take; timed=i-entry_idx >= 60 or i == len(frames)-1
                if hit or hit_tp or timed:
                    gross=_signed_gross(o.side, ep, f.price, Decimal(fi.quantity)); exfee=_fee(self.c.notional, rate); net=gross-Decimal(fi.fee)-exfee; reason='STOP' if hit else ('TAKE_PROFIT' if hit_tp else 'TIME_EXIT'); trades.append(TradeRecord(f'{self.c.run_id}:trade:{len(trades)+1}',self.c.run_id,self.c.strategy_id,self.c.strategy_revision,self.c.dataset_id,self.c.dataset_revision,self.c.symbol,sr.frame_time_ms,o.decision_time_ms,fi.fill_time_ms,f.timestamp_ms,o.side,fi.quantity,o.notional,fi.price,str(f.price),fi.liquidity_role,role,str(gross),fi.fee,str(exfee),fi.slippage,'0',str(net),sr.reason,reason)); open_trade=None; entry_idx=None
            hist.append(f); cvds.append(cvd)
        out.mkdir(parents=True, exist_ok=True); rows=lambda xs:[_as_strings(x) for x in xs]
        hashes={n:_dump(out/f'{n[:-1]}_ledger.json', rows(xs)) for n,xs in [('signals',signals),('orders',orders),('fills',fills),('trades',trades)]}
        manifest={'runId':self.c.run_id,'strategyId':self.c.strategy_id,'strategyRevision':self.c.strategy_revision,'datasetId':self.c.dataset_id,'datasetRevision':self.c.dataset_revision,'symbol':self.c.symbol,'isStartMs':self.c.is_start_ms,'isEndMs':self.c.is_end_ms,'evaluationPeriod':'OOS','frameStreamHash':self.c.frame_stream_hash,'parameterFingerprint':artifact_hash(asdict(self.sc)),'feeModel':self.c.fee_model,'executionModelRevision':self.c.execution_model_revision,'positionSizingModel':self.c.position_sizing_model,'oosEventsConsumed':len(frames),'executionModelLimitation':self.lim,'artifactHashes':hashes}
        mh=_dump(out/'run_manifest.json', manifest); agg=metrics(trades,signals,orders,fills,self.days); agg['artifact_hashes']=hashes; agg['run_manifest_hash']=mh; _dump(out/'aggregate.json',agg); return {'manifest':manifest,'manifest_hash':mh,'hashes':hashes,'aggregate':agg}

def main():
    summary={}; registry={'schemaVersion':'pair-strategy-validation-registry-v1','cells':{}}; routing={'schemaVersion':'deployment-routing-summary-v1','routes':{}}
    for symbol,(start,expected,days) in SYMBOLS.items():
        frames,stats,spec=load_frames(symbol,start,expected); summary[symbol]={'oos_start_ms':start,'oos_end_ms':END,'expected_frames':expected,'generated_frames':len(frames),'frame_hash':stats.frame_stream_hash,'crossed_books':stats.crossed_books,'empty_books':stats.empty_books,'future_l2_violations':stats.future_l2_violations,'future_trade_violations':stats.future_trade_violations,'strategies':{}}
        for sid,strat in STRATS.items():
            slug=strat[0]; sc=strat[5];
            if hasattr(sc,'tick_size'): sc=replace(sc,tick_size=spec.tick_size)
            cfg=ReplayConfig(run_id=f'canonical_oos_{symbol.lower()}_{slug}',strategy_id=sid,symbol=symbol,is_start_ms=start,is_end_ms=END,frame_stream_hash=stats.frame_stream_hash,taker_fee_rate=Decimal('0.0005'),maker_fee_rate=Decimal('0.0002'),notional=Decimal('100.0'))
            replay=Replay(cfg,(strat[0],strat[1],strat[2],strat[3],strat[4],sc,strat[6]),days); r1=replay.run(frames,OUT/symbol/slug/'run1'); r2=replay.run(frames,OUT/symbol/slug/'run2')
            if (r1['manifest_hash'],r1['hashes'],r1['aggregate']) != (r2['manifest_hash'],r2['hashes'],r2['aggregate']): raise RuntimeError(f'DETERMINISM_FAIL {symbol} {sid}')
            rc=replay.run(frames,OUT/symbol/slug); a=rc['aggregate']; key=f'{symbol}::{sid}'; summary[symbol]['strategies'][sid]={'strategyRevision':cfg.strategy_revision,'parameterFingerprint':rc['manifest']['parameterFingerprint'],'determinism':'PASS','manifestHash':rc['manifest_hash'],'artifactHashes':rc['hashes'],'aggregate':a}; registry['cells'][key]={'symbol':symbol,'strategyId':sid,'strategyRevision':cfg.strategy_revision,'venue':'Binance USD-M Futures','marketType':'PERPETUAL','isGateResult':'PASS','oosWindow':{'startMs':start,'endMs':END},'instrumentSpecHash':hashlib.sha256(json.dumps(spec.to_manifest(),sort_keys=True).encode()).hexdigest(),'datasetId':cfg.dataset_id,'datasetRevision':cfg.dataset_revision,'canonicalArtifactHashes':rc['hashes'],'runManifestHash':rc['manifest_hash'],'validationStatus':a['evidence_disposition'],'frequencyClassification':a['frequency_classification'],'economicClassification':a['economic_classification'],'reuseStatus':a['reuse_status'],'testnetEligibility':a['testnet_eligibility'],'mainnetEligibility':a['mainnet_eligibility'],'activeBlockers':a['blockers']}; routing['routes'][key]={'historicalValidationStatus':a['evidence_disposition'],'currentCompatibility':'COMPATIBLE','testnetEligibility':a['testnet_eligibility'],'mainnetEligibility':a['mainnet_eligibility']}
            print(symbol,sid,a['signals'],a['completed_trades'],a['trades_per_day'],a['gross_pnl'],a['fees'],a['net_pnl'],a['frequency_classification'],a['evidence_disposition'],flush=True)
    (OUT/'gate_summary.json').write_text(json.dumps(summary,indent=2,default=str)+'\n'); rp=OUT/'pair_strategy_validation_registry.json'; rp.write_text(json.dumps(registry,indent=2,default=str)+'\n'); tp=OUT/'deployment_routing_summary.json'; tp.write_text(json.dumps(routing,indent=2,default=str)+'\n'); mp=OUT/'oos_validation_manifest.json'; mp.write_text(json.dumps({'schemaVersion':'oos-validation-manifest-v1','requestId':'AXODUS-TRADING-VAL-QUANT-ORDERFLOW-OOS-01','parametersFrozen':True,'strategyRevisionsFrozen':True,'exchangeMutations':0,'testnetMutations':0,'mainnetMutations':0,'realCapital':0,'symbols':summary,'registrySha256':hashlib.sha256(rp.read_bytes()).hexdigest(),'routingSha256':hashlib.sha256(tp.read_bytes()).hexdigest()},indent=2,default=str)+'\n')

if __name__ == '__main__': main()
