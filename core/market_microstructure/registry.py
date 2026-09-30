"""Versioned catalog: units, normalization and source requirements."""
from dataclasses import dataclass
from .models import SCHEMA_VERSION, HORIZONS

@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    description: str
    units: str
    normalization: str
    sources: tuple
    horizons: tuple = HORIZONS
    version: str = SCHEMA_VERSION


def catalog():
    result={}
    def add(names, description, units, normalization, sources):
        for name in names:
            result[name]=FeatureDefinition(name,description,units,normalization,tuple(sources))
    add(['available_bid_levels','available_ask_levels'],'available capped view depth; fewer levels mean partial top-N coverage','count','none',('L2',))
    for n in (1,5,10):
        add([f'queue_imbalance_L{n}',f'signed_queue_imbalance_L{n}'],f'top-{n} visible depth ratio','ratio','bid/(bid+ask); signed=(bid-ask)/(sum)',('L2',))
        add([f'OFI_L{n}',f'normalized_OFI_L{n}'],f'rank-wise top-{n} signed quantity transitions','base quantity','raw or divided by current top-N total depth',('L2',))
        add([f'bid_depth_L{n}',f'ask_depth_L{n}'],f'top-{n} displayed depth','base quantity','sum',('L2',))
    add(['microprice','mid','microprice_minus_mid','microprice_displacement_bps'],'top-level weighted-mid proxy','quote price/bps','(bid*askQty+ask*bidQty)/(sum)',('L2',))
    add(['microprice_valid','microprice_provenance'],'explicit weighted-book price availability','boolean/enum','COMPUTED_FROM_BOOK or UNAVAILABLE; no fallback',('L2',))
    add(['weighted_bid_depth','weighted_ask_depth','weighted_book_pressure','normalized_weighted_pressure'],'rank-weighted displayed depth','quantity/ratio','weights = 1/(level rank); configurable',('L2',))
    add(['buy_aggressive_volume','sell_aggressive_volume','signed_aggressive_volume','aggression_imbalance','unknown_aggressive_volume'],'tape aggressor quantities','base quantity/ratio','signed = buy-sell',('TRADES',))
    add(['buy_trade_count','sell_trade_count','average_buy_trade_size','average_sell_trade_size','trade_rate','volume_rate','signed_volume_rate','trade_velocity','volume_velocity','signed_volume_velocity','trade_acceleration','signed_volume_acceleration'],'tape rates and first difference','count/quantity per second','window seconds; acceleration per second squared',('TRADES',))
    for side in ('bid','ask'):
        add([side+'_add_rate',side+'_cancel_rate','net_'+side+'_liquidity_change',side+'_cancel_to_add_ratio',side+'_consumed_volume_proxy',side+'_replenished_volume',side+'_replenishment_ratio',side+'_replenishment_count'],'visible L2 quantity changes, cancel is removal proxy','quantity/count/ratio','per horizon; removal cause unknown',('L2',))
    add(['buy_sweep_score','sell_sweep_score'],'monotone multi-price trade cluster proxy','quantity*levels','non-overlapping cluster; not proven single market order',('TRADES',))
    add(['buy_absorption_score','sell_absorption_score'],'aggression times visible opposite-side restoration divided by tick response','base quantity','not hidden-liquidity proof',('L2','TRADES'))
    add(['mid_change','price_change','microprice_change','price_response_per_signed_volume','price_response_per_aggressive_volume','mid_return'],'horizon price response','quote price/ratio','anchor at/before horizon start',('L2','TRADES'))
    add(['spread_absolute','spread_bps','spread_change','spread_percentile','total_depth','depth_imbalance','depth_change','depth_percentile','realized_micro_volatility','trade_rate_percentile'],'book/regime context','price/bps/quantity/ratio','causal rolling context',('L2','TRADES'))
    return result
