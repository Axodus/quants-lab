"""Decimal authority for fee-aware closed-trade and equity metrics."""
from decimal import Decimal
from statistics import median

from .models import OptimizationError, canonical, decimal

D = Decimal


def economics(simulation, duration_days):
    fills = simulation.fills
    quantity = D(0)
    entry = D(0)
    entry_fee = D(0)
    trades = []
    fees = D(0)
    for fill in fills:
        qty, price, fee = (decimal(fill[k]) for k in ('quantity', 'price', 'fee'))
        fees += fee
        signed = qty if fill['side'] == 'BUY' else -qty
        if not quantity:
            quantity, entry, entry_fee = signed, price, fee
        elif signed * quantity < 0 and abs(signed) == abs(quantity):
            gross = (price-entry) * quantity
            trades.append({'gross': canonical(gross), 'fees': canonical(entry_fee+fee), 'net': canonical(gross-entry_fee-fee),
                           'exit_time': fill['eventTime']})
            quantity = D(0)
        else:
            raise OptimizationError('unsupported overlapping/reversing trial position')
    if quantity or 'pending_orders_at_end' in simulation.limitations:
        raise OptimizationError('trial terminal position/order is not flat')
    net = sum((decimal(t['net']) for t in trades), D(0))
    gross = sum((decimal(t['gross']) for t in trades), D(0))
    if abs(net - decimal(simulation.metrics['netPnl'])) > D('1e-22'):
        raise OptimizationError('simulation and closed-trade accounting mismatch')
    wins = sum((max(decimal(t['net']), D(0)) for t in trades), D(0))
    losses = -sum((min(decimal(t['net']), D(0)) for t in trades), D(0))
    initial = decimal(simulation.metrics['initialEquity'])
    eq = [initial] + [decimal(p['equity']) for p in simulation.equity_series]
    peak, dd = initial, D(0)
    for value in eq:
        peak = max(peak, value)
        dd = max(dd, (peak-value)/peak)
    returns = [(b-a)/a for a,b in zip(eq,eq[1:]) if a]
    mean = sum(returns, D(0))/len(returns) if returns else D(0)
    variance = sum(((v-mean)**2 for v in returns), D(0))/len(returns) if returns else D(0)
    metrics = {'net_pnl': net, 'gross_pnl': gross, 'fees': fees, 'trade_count': len(trades),
               'net_expectancy': net/len(trades) if trades else D(0), 'trades_per_day': D(len(trades))/duration_days,
               'profit_factor': wins/losses if losses else None,
               'max_drawdown': dd, 'max_drawdown_bps': dd*10000,
               'sharpe': mean/variance.sqrt() if variance else None,
               'return_over_drawdown': (net/initial)/dd if dd else None,
               'slippage': sum((decimal(f['slippageCost']) for f in fills), D(0))}
    return canonical(metrics), trades


OBJECTIVE_KEYS = {'NET_EXPECTANCY':'net_expectancy', 'NET_PNL':'net_pnl', 'PROFIT_FACTOR':'profit_factor',
                  'SHARPE':'sharpe', 'MAX_DRAWDOWN':'max_drawdown', 'RETURN_OVER_DRAWDOWN':'return_over_drawdown'}


def objective_value(objective, metrics):
    raw = metrics[OBJECTIVE_KEYS[objective.name]]
    if raw is None:
        raise OptimizationError('objective undefined: insufficient trades/variance/losses')
    return decimal(raw)


def distribution(values):
    values = [decimal(v) for v in values]
    if not values:
        return {'count': 0}
    mean = sum(values,D(0))/len(values)
    return canonical({'count':len(values), 'positive_ratio':D(sum(v>0 for v in values))/len(values),
                      'median':median(values), 'worst':min(values), 'dispersion':sum(((v-mean)**2 for v in values),D(0))/len(values)})


def aggregate(evaluations, objective):
    # Every symbol/window has equal weight; raw PnL cannot dominate by notional.
    metrics = {}
    for key in evaluations[0]['metrics']:
        values = [e['metrics'][key] for e in evaluations]
        metrics[key] = canonical(median([decimal(v) for v in values])) if all(v is not None for v in values) else None
    score = objective_value(objective, metrics)
    by_symbol, by_window = {}, {}
    for evaluation in evaluations:
        by_symbol.setdefault(evaluation['symbol'], []).append(decimal(evaluation['metrics']['net_expectancy']))
        by_window.setdefault(evaluation['window_ref']['name'], []).append(objective_value(objective, evaluation['metrics']))
    cross = distribution([canonical(median(v)) for v in by_symbol.values()])
    cross['positive_symbol_ratio'] = cross['positive_ratio']
    cross['median_net_expectancy'] = cross['median']
    cross['worst_symbol_expectancy'] = cross['worst']
    cross['drawdown_distribution'] = distribution([e['metrics']['max_drawdown'] for e in evaluations])
    cross['trade_frequency_distribution'] = distribution([e['metrics']['trades_per_day'] for e in evaluations])
    temporal = distribution([canonical(median(v)) for v in by_window.values()])
    window_net = {}
    for e in evaluations:
        window_net.setdefault(e['window_ref']['name'], []).append(decimal(e['metrics']['net_pnl']))
    temporal['positive_window_ratio'] = canonical(D(sum(median(v)>0 for v in window_net.values()))/len(window_net))
    temporal['worst_window'] = min(by_window, key=lambda k: median(by_window[k])) if objective.direction == 'MAXIMIZE' else max(by_window, key=lambda k: median(by_window[k]))
    return metrics, canonical(score), cross, temporal
