"""Causal validation engine for order-flow research.
Synthetic runs are ENGINE_VALIDATION_SYNTHETIC only and never promotion evidence.
"""
import csv, hashlib, json, math, time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional
try:
 from .data_fixture import MarketTick
 from .strategies import OrderFlowStrategyBase, OrderType, SignalType
except ImportError:
 from data_fixture import MarketTick
 from strategies import OrderFlowStrategyBase, OrderType, SignalType
@dataclass
class FeeModel:
 pair:str; taker_fee_rate:float; maker_fee_rate:float; slippage_bps:float=1.0; maker_fill_assumption:str="ASSUMED_SYNTHETIC_NOT_BOOK_EXECUTABLE"; funding_model:str="NOT_MODELED"
 @classmethod
 def for_pair(cls,pair):
  p=pair.upper()
  if "ETH" in p: return cls(p,0.0004,0.0,1.0)
  return cls(p,0.0005,0.0002,1.0)
@dataclass
class Scenario:
 name:str; slippage_bps:float; taker_fee_mult:float=1.0; maker_fee_mult:float=1.0; force_taker_exit:bool=False
SCENARIOS={"BASE":Scenario("BASE",1.0),"STRESS_1":Scenario("STRESS_1",5.0),"STRESS_2":Scenario("STRESS_2",1.0,1.5,1.5,True),"STRESS_3":Scenario("STRESS_3",5.0,1.5,1.5,True)}
@dataclass
class BacktestConfig:
 pair:str="BTCUSDT"; days:int=90; initial_capital:float=10000.0; position_size_usd:float=1000.0; max_open_positions:int=1; scenario:str="BASE"; data_mode:str="synthetic"; interval_seconds:int=60; slippage_bps:Optional[float]=None; taker_fee_rate:Optional[float]=None; maker_fee_rate:Optional[float]=None
@dataclass
class Position:
 entry_index:int; entry_time:float; entry_price:float; effective_entry_price:float; side:SignalType; entry_order_type:OrderType; exit_order_type:OrderType; size_units:float; notional:float; stop_loss_price:float; take_profit_price:float; entry_fee:float; entry_slippage_cost:float; strategy_name:str; reason:str; assumed_maker_entry:bool
@dataclass
class ExecutedTrade:
 trade_id:int; entry_time:float; exit_time:float; side:str; strategy_name:str; entry_order_type:str; exit_order_type:str; entry_price:float; effective_entry_price:float; exit_price:float; effective_exit_price:float; size_units:float; position_notional:float; gross_pnl:float; entry_fee:float; exit_fee:float; total_fees:float; entry_slippage_cost:float; exit_slippage_cost:float; total_slippage_cost:float; funding:float; net_pnl:float; return_pct:float; exit_reason:str; duration_bars:int; maker_fill_status:str
 def to_dict(self): return asdict(self)
@dataclass
class PerformanceReport:
 pair:str; strategy_name:str; days:int; scenario:str; data_mode:str; disposition:str; initial_capital:float; final_equity:float; total_trades:int; long_trades:int; short_trades:int; winning_trades:int; losing_trades:int; win_rate_pct:float; gross_pnl:float; net_pnl:float; avg_trade_pnl:float; median_trade_pnl:float; avg_winner:float; avg_loser:float; profit_factor:float; expectancy:float; max_drawdown_pct:float; max_drawdown_dollars:float; max_drawdown_duration_trades:int; sharpe_ratio:float; sortino_ratio:float; exposure_time_pct:float; turnover:float; total_fees_paid:float; total_slippage_cost:float; funding_total:float; largest_winner:float; largest_loser:float; consecutive_wins:int; consecutive_losses:int; trades:List[ExecutedTrade]=field(default_factory=list)
 def to_dict(self):
  d=asdict(self); d["trades"]=[t.to_dict() for t in self.trades]; d["validation_label"]="ENGINE_VALIDATION_SYNTHETIC" if self.data_mode=="synthetic" else "HISTORICAL_VALIDATION"; d["promotion_allowed"]=False if self.data_mode=="synthetic" else None; d["funding_note"]="NOT_MODELED"; return d
class MetricCalculator:
 @staticmethod
 def calculate(trades, initial_capital, pair, strategy_name, days, scenario, data_mode, total_bars=0):
  vals=[t.net_pnl for t in trades]; wins=[v for v in vals if v>0]; losses=[v for v in vals if v<0]
  eq=initial_capital; peak=initial_capital; maxdd=0; maxddpct=0; curdddur=0; maxdddur=0; returns=[]
  for v in vals:
   eq+=v; peak=max(peak,eq); dd=peak-eq; curdddur=curdddur+1 if dd>0 else 0; maxdddur=max(maxdddur,curdddur); maxdd=max(maxdd,dd); maxddpct=max(maxddpct,(dd/peak*100) if peak else 0); returns.append(v/initial_capital)
  gross=sum(t.gross_pnl for t in trades); fees=sum(t.total_fees for t in trades); slip=sum(t.total_slippage_cost for t in trades); funding=sum(t.funding for t in trades); net=sum(vals)
  pf=(sum(wins)/abs(sum(losses))) if losses else (float('inf') if wins else 0.0); avg=sum(vals)/len(vals) if vals else 0; med=median(vals) if vals else 0
  def streak(pos):
   best=cur=0
   for v in vals:
    ok=v>0 if pos else v<0; cur=cur+1 if ok else 0; best=max(best,cur)
   return best
  if len(returns)>1:
   m=sum(returns)/len(returns); sd=math.sqrt(sum((r-m)**2 for r in returns)/(len(returns)-1)) or 1e-12; trades_per_year=len(trades)/max(days,1)*365; sharpe=m/sd*math.sqrt(trades_per_year); neg=[r for r in returns if r<0]; dsd=math.sqrt(sum(r*r for r in neg)/len(neg)) if neg else 0; sortino=(m/dsd*math.sqrt(trades_per_year)) if dsd else 0
  else: sharpe=sortino=0
  exposure=sum(t.duration_bars for t in trades)/total_bars*100 if total_bars else 0; turnover=sum(t.position_notional*2 for t in trades)
  return PerformanceReport(pair,strategy_name,days,scenario,data_mode,"DATASET_INSUFFICIENT / RESEARCH_CONTINUE" if data_mode=="synthetic" else "HISTORICAL_REVIEW_REQUIRED",initial_capital,initial_capital+net,len(trades),sum(1 for t in trades if t.side=='buy'),sum(1 for t in trades if t.side=='sell'),len(wins),len(vals)-len(wins),len(wins)/len(vals)*100 if vals else 0,gross,net,avg,med,sum(wins)/len(wins) if wins else 0,sum(losses)/len(losses) if losses else 0,pf,avg,maxddpct,maxdd,maxdddur,sharpe,sortino,exposure,turnover,fees,slip,funding,max(vals) if vals else 0,min(vals) if vals else 0,streak(True),streak(False),trades)
class BacktestRunner:
 def __init__(self,config):
  self.config=config; self.fee_model=FeeModel.for_pair(config.pair); sc=SCENARIOS[config.scenario]; self.fee_model.slippage_bps=sc.slippage_bps; self.fee_model.taker_fee_rate*=sc.taker_fee_mult; self.fee_model.maker_fee_rate*=sc.maker_fee_mult; self.force_taker_exit=sc.force_taker_exit
  if config.slippage_bps is not None: self.fee_model.slippage_bps=config.slippage_bps
  if config.taker_fee_rate is not None: self.fee_model.taker_fee_rate=config.taker_fee_rate
  if config.maker_fee_rate is not None: self.fee_model.maker_fee_rate=config.maker_fee_rate
 def _apply_slippage(self,price,order_type,side):
  if order_type==OrderType.MAKER:return price
  f=self.fee_model.slippage_bps/10000; return price*(1+f) if side==SignalType.BUY else price*(1-f)
 def _fee(self,notional,order_type): return notional*(self.fee_model.taker_fee_rate if order_type==OrderType.TAKER else self.fee_model.maker_fee_rate)
 def evaluate_strategy(self,strategy,ticks):
  pos=None; trades=[]
  for i in range(len(ticks)-1):
   tick=ticks[i]
   if pos:
    dur=i-pos.entry_index; exit_price=None; reason=None
    if pos.side==SignalType.BUY:
     sl=tick.low<=pos.stop_loss_price; tp=tick.high>=pos.take_profit_price
     if sl: exit_price=pos.stop_loss_price; reason="Stop Loss Hit (stop-first conservative)"
     elif tp: exit_price=pos.take_profit_price; reason="Take Profit Hit"
    else:
     sl=tick.high>=pos.stop_loss_price; tp=tick.low<=pos.take_profit_price
     if sl: exit_price=pos.stop_loss_price; reason="Stop Loss Hit (stop-first conservative)"
     elif tp: exit_price=pos.take_profit_price; reason="Take Profit Hit"
    if exit_price is None and dur>=getattr(strategy,'time_exit',60): exit_price=tick.close; reason="Time Exit"
    if exit_price is not None:
     exit_side=SignalType.SELL if pos.side==SignalType.BUY else SignalType.BUY; eot=OrderType.TAKER if self.force_taker_exit else pos.exit_order_type; eff=self._apply_slippage(exit_price,eot,exit_side); exnot=eff*pos.size_units; exfee=self._fee(exnot,eot); exsl=abs(eff-exit_price)*pos.size_units; gross=(exit_price-pos.entry_price)*pos.size_units if pos.side==SignalType.BUY else (pos.entry_price-exit_price)*pos.size_units; fees=pos.entry_fee+exfee; slip=pos.entry_slippage_cost+exsl; net=gross-fees-slip
     trades.append(ExecutedTrade(len(trades)+1,pos.entry_time,tick.timestamp,pos.side.value,pos.strategy_name,pos.entry_order_type.value,eot.value,pos.entry_price,pos.effective_entry_price,exit_price,eff,pos.size_units,pos.notional,gross,pos.entry_fee,exfee,fees,pos.entry_slippage_cost,exsl,slip,0.0,net,net/pos.notional,reason,dur,"ASSUMED" if (pos.assumed_maker_entry or eot==OrderType.MAKER) else "EXECUTION_MODELED_TAKER")); pos=None
   if pos is None:
    sig=strategy.generate_signal(ticks,i)
    if sig and sig.signal_type in (SignalType.BUY,SignalType.SELL):
     nt=ticks[i+1]  # no earlier than next bar open
     px=nt.open; eff=self._apply_slippage(px,sig.entry_order_type,sig.signal_type); units=self.config.position_size_usd/eff; fee=self._fee(self.config.position_size_usd,sig.entry_order_type); slc=abs(eff-px)*units
     tp=px*(1+sig.take_profit_pct) if sig.signal_type==SignalType.BUY else px*(1-sig.take_profit_pct); sl=px*(1-sig.stop_loss_pct) if sig.signal_type==SignalType.BUY else px*(1+sig.stop_loss_pct)
     pos=Position(i+1,nt.timestamp,px,eff,sig.signal_type,sig.entry_order_type,sig.exit_order_type,units,self.config.position_size_usd,sl,tp,fee,slc,strategy.name,sig.reason,sig.entry_order_type==OrderType.MAKER)
  return MetricCalculator.calculate(trades,self.config.initial_capital,self.config.pair,strategy.name,self.config.days,self.config.scenario,self.config.data_mode,len(ticks))
@dataclass
class BacktestResult: config:BacktestConfig; reports:Dict[str,PerformanceReport]; ticks_evaluated:int
def strategies_for_key(k):
 try: from .strategies import AggressionMomentumScalper, InstitutionalAbsorptionFade, CVDDivergenceReversal
 except ImportError: from strategies import AggressionMomentumScalper, InstitutionalAbsorptionFade, CVDDivergenceReversal
 d={"momentum":AggressionMomentumScalper(),"absorption":InstitutionalAbsorptionFade(),"divergence":CVDDivergenceReversal()}; return d.values() if k=="all" else [d[k]]
def run_backtest(pair="BTCUSDT",days=90,strategy_key="all",ticks=None,initial_capital=10000.0,position_size_usd=1000.0,slippage_bps=None,scenario="BASE",data_mode="synthetic"):
 if ticks is None:
  try: from .data_fixture import generate_synthetic_ticks
  except ImportError: from data_fixture import generate_synthetic_ticks
  ticks=generate_synthetic_ticks(pair,days)
 cfg=BacktestConfig(pair,days,initial_capital,position_size_usd,scenario=scenario,data_mode=data_mode); r=BacktestRunner(cfg); reports={}
 if slippage_bps is not None: r.fee_model.slippage_bps=slippage_bps
 for s in strategies_for_key(strategy_key): reports[s.name]=r.evaluate_strategy(s,ticks)
 return BacktestResult(cfg,reports,len(ticks))
def write_artifacts(result,outdir,metadata):
 out=Path(outdir); out.mkdir(parents=True,exist_ok=True); payload={"metadata":metadata,"config":asdict(result.config),"reports":{k:v.to_dict() for k,v in result.reports.items()}}
 raw=json.dumps(payload,sort_keys=True,default=str).encode(); run_id=hashlib.sha256(raw).hexdigest()[:16]; payload["run_id"]=run_id
 (out/"report.json").write_text(json.dumps(payload,indent=2,default=str)); (out/"manifest.json").write_text(json.dumps({"run_id":run_id,**metadata,"config":asdict(result.config),"ticks_evaluated":result.ticks_evaluated},indent=2))
 for name,rep in result.reports.items():
  safe=name.lower().replace(' ','_'); rows=[t.to_dict() for t in rep.trades]; (out/f"{safe}_trades.json").write_text(json.dumps(rows,indent=2))
  if rows:
   with (out/f"{safe}_trades.csv").open('w',newline='') as f: w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
 (out/"cost_decomposition.json").write_text(json.dumps({k:{"fees":v.total_fees_paid,"slippage":v.total_slippage_cost,"funding":"NOT_MODELED","funding_total":v.funding_total} for k,v in result.reports.items()},indent=2)); return run_id
