"""Frozen causal order-flow adapters. Signals use only bars through idx."""
from dataclasses import dataclass
from enum import Enum
from statistics import mean, pstdev
from typing import List, Optional
try: from .data_fixture import MarketTick
except ImportError: from data_fixture import MarketTick
class OrderType(str, Enum): TAKER="taker"; MAKER="maker"
class SignalType(str, Enum): BUY="buy"; SELL="sell"; HOLD="hold"
@dataclass
class TradeSignal:
 timestamp:float; signal_type:SignalType; entry_order_type:OrderType; exit_order_type:OrderType; price:float; stop_loss_pct:float; take_profit_pct:float; reason:str; strategy_name:str
class OrderFlowStrategyBase:
 def __init__(self,name): self.name=name
 def generate_signal(self,ticks,idx): raise NotImplementedError
class AggressionMomentumScalper(OrderFlowStrategyBase):
 """Source-equivalent adapter: standardized delta, OBI/spread proxy, prior breakout."""
 def __init__(self,lookback=20,delta_z=1.5,obi_threshold=.45,spread_proxy_max=.01,breakout_lookback=5,tp=.0035,sl=.002,time_exit=60,imbalance_threshold=None,cvd_window=None,take_profit_pct=None,stop_loss_pct=None):
  if imbalance_threshold is not None: obi_threshold=imbalance_threshold
  if cvd_window is not None: breakout_lookback=cvd_window
  if take_profit_pct is not None: tp=take_profit_pct
  if stop_loss_pct is not None: sl=stop_loss_pct
  super().__init__("Aggression Momentum Scalper"); self.lookback,self.delta_z,self.obi_threshold,self.spread_proxy_max,self.breakout_lookback,self.take_profit_pct,self.stop_loss_pct,self.time_exit=lookback,delta_z,obi_threshold,spread_proxy_max,breakout_lookback,tp,sl,time_exit
 def generate_signal(self,t,idx):
  if idx < max(self.lookback,self.breakout_lookback): return None
  x=t[idx]; hist=[z.delta for z in t[idx-self.lookback:idx]]; sd=pstdev(hist)
  if sd==0:return None
  z=(x.delta-mean(hist))/sd; prior=t[idx-self.breakout_lookback:idx]; spread=abs(x.close-x.vwap)/max(x.close,1)
  long=z>=self.delta_z and x.order_imbalance>=self.obi_threshold and spread<=self.spread_proxy_max and x.close>max(q.high for q in prior)
  short=z<=-self.delta_z and x.order_imbalance<=-self.obi_threshold and spread<=self.spread_proxy_max and x.close<min(q.low for q in prior)
  side=SignalType.BUY if long else SignalType.SELL if short else None
  return TradeSignal(x.timestamp,side,OrderType.TAKER,OrderType.TAKER,x.close,self.stop_loss_pct,self.take_profit_pct,"standardized delta + OBI + spread proxy + prior breakout",self.name) if side else None
class InstitutionalAbsorptionFade(OrderFlowStrategyBase):
 """Name retained for compatibility; logic makes no institutional attribution."""
 def __init__(self,absorption_threshold=2.5,depth_skew_threshold=.35,confirmation_intervals=2,tp=.004,sl=.0018,time_exit=60):
  super().__init__("Absorption Fade"); self.absorption_threshold,self.depth_skew_threshold,self.confirmation_intervals,self.take_profit_pct,self.stop_loss_pct,self.time_exit=absorption_threshold,depth_skew_threshold,confirmation_intervals,tp,sl,time_exit
 def generate_signal(self,t,idx):
  if idx<self.confirmation_intervals-1:return None
  w=t[idx-self.confirmation_intervals+1:idx+1]; sell=all(q.absorption_score>=self.absorption_threshold and q.order_imbalance<=-.30 and q.bid_skew>=self.depth_skew_threshold for q in w); buy=all(q.absorption_score>=self.absorption_threshold and q.order_imbalance>=.30 and q.bid_skew<=-self.depth_skew_threshold for q in w)
  side=SignalType.BUY if sell else SignalType.SELL if buy else None; x=t[idx]
  return TradeSignal(x.timestamp,side,OrderType.MAKER,OrderType.MAKER,x.close,self.stop_loss_pct,self.take_profit_pct,"two-interval absorption proxy confirmation",self.name) if side else None
class CVDDivergenceReversal(OrderFlowStrategyBase):
 def __init__(self,lookback_window=15,price_tick_threshold=.0005,delta_threshold=0.0,tp=.005,sl=.0025,time_exit=60):
  super().__init__("CVD Divergence Reversal"); self.lookback_window,self.price_tick_threshold,self.delta_threshold,self.take_profit_pct,self.stop_loss_pct,self.time_exit=lookback_window,price_tick_threshold,delta_threshold,tp,sl,time_exit
 def generate_signal(self,t,idx):
  if idx<self.lookback_window:return None
  x=t[idx]; p=t[idx-self.lookback_window:idx]; lo=min(p,key=lambda q:q.low); hi=max(p,key=lambda q:q.high)
  bull=x.low < lo.low*(1-self.price_tick_threshold) and x.cvd>lo.cvd and x.delta>=self.delta_threshold
  bear=x.high > hi.high*(1+self.price_tick_threshold) and x.cvd<hi.cvd and x.delta<=-self.delta_threshold
  side=SignalType.BUY if bull else SignalType.SELL if bear else None
  reason="Bullish CVD Divergence: causal trailing bounds" if bull else "Bearish CVD Divergence: causal trailing bounds"
  return TradeSignal(x.timestamp,side,OrderType.TAKER,OrderType.TAKER,x.close,self.stop_loss_pct,self.take_profit_pct,reason,self.name) if side else None
