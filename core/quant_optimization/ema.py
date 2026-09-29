"""Research adapter of EMA pullback v2 signal rules, with causal next-close execution.

This does not replace or mutate ema-5x9-v2. Trials are research variants.
Closed-candle triggers execute at the next observed close, never retroactively
at the EMA touched earlier in a candle. No same-bar fill claim is made.
"""
from decimal import Decimal as D
from pathlib import Path
from core.quant_simulation.models import SimulatedDecision
from .models import decimal, OptimizationError
from .provenance import file_hash
from .search_space import SearchSpace, IntegerParameter, DecimalParameter

STRATEGY_ID = 'trend.ema.5x9.pullback.scalper'
BASE_REVISION = 'ema-5x9-v2'


def get_search_space():
    return SearchSpace((
        IntegerParameter('ema_fast', 9, 9, default=9),
        IntegerParameter('ema_slow', 21, 21, default=21),
        DecimalParameter('pullback_tolerance_bps', '0', '10', '1', default='2'),
        DecimalParameter('take_profit_bps', '12', '30', '1', default='15', domain='RISK'),
        DecimalParameter('stop_loss_bps', '5', '10', '1', default='10', domain='RISK'),
        IntegerParameter('cooldown_bars', 7, 7, default=7, domain='CONTROL'),
    ), (('ema_fast', 'ema_slow'),))


def source_hash():
    return file_hash(Path(__file__))


class EMAPullbackResearchAdapter:
    def __init__(self, parameters, notional, slippage_bps, count):
        self.p = parameters
        self.notional = decimal(notional)
        self.slippage = decimal(slippage_bps) / 10000
        self.count = count
        self.index = -1
        self.closes = []
        self.ema = {}
        self.previous_quantity = D(0)
        self.entry = D(0)
        self.cooldown = 0
        if parameters['ema_fast'] >= parameters['ema_slow']:
            raise OptimizationError('ema_fast must be less than ema_slow')

    def decide(self, state, quantity):
        self.index += 1
        close = decimal(state['marketPrice'])
        low, high = decimal(state['low']), decimal(state['high'])
        self.closes.append(close)
        for n in (self.p['ema_fast'], self.p['ema_slow']):
            if len(self.closes) == n:
                self.ema[n] = sum(self.closes, D(0)) / n
            elif len(self.closes) > n:
                self.ema[n] += (close - self.ema[n]) * D(2) / (n + 1)
        just_filled = bool(quantity and not self.previous_quantity)
        if just_filled:
            self.entry = close * (1 + (self.slippage if quantity > 0 else -self.slippage))
        if not quantity and self.previous_quantity:
            self.cooldown = self.p['cooldown_bars']
        self.previous_quantity = quantity
        identity = f'candidate:{self.index}'
        # Reserve final observation for execution of terminal close, not entry.
        if self.index >= self.count - 2:
            return (SimulatedDecision('EXIT', 'SELL' if quantity > 0 else 'BUY', abs(quantity), identity)
                    if quantity and self.index == self.count - 2 else SimulatedDecision.no_action(identity))
        if len(self.closes) <= self.p['ema_slow']:
            return SimulatedDecision.no_action(identity)
        fast, slow = self.ema[self.p['ema_fast']], self.ema[self.p['ema_slow']]
        if quantity:
            tp = decimal(self.p['take_profit_bps']) / 10000
            sl = decimal(self.p['stop_loss_bps']) / 10000
            # Intrabar extremes may trigger an exit only after the bar closes.
            # On the entry fill bar, only close is known to be after the fill.
            held_before_bar = not just_filled
            exit_low, exit_high = (low, high) if held_before_bar else (close, close)
            hit = ((exit_low <= self.entry*(1-sl) or exit_high >= self.entry*(1+tp) or fast < slow) if quantity > 0
                   else (exit_high >= self.entry*(1+sl) or exit_low <= self.entry*(1-tp) or fast > slow))
            if hit:
                return SimulatedDecision('EXIT', 'SELL' if quantity > 0 else 'BUY', abs(quantity), identity)
        elif self.cooldown:
            self.cooldown -= 1
        elif self.index < self.count - 3:
            tolerance = decimal(self.p['pullback_tolerance_bps']) / 10000
            if low <= slow*(1+tolerance) and high >= slow*(1-tolerance) and fast != slow:
                return SimulatedDecision('ENTER', 'BUY' if fast > slow else 'SELL', self.notional/close, identity)
        return SimulatedDecision.no_action(identity)
