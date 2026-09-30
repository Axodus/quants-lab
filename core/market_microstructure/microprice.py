"""Top-of-book opposite-quantity weighted price, not calibrated Stoikov microprice."""
from decimal import Decimal
from .state import OrderBookState


def calculate_microprice(state: OrderBookState) -> Decimal | None:
    if state.validity_state != 'VALID' or not state.bid_levels or not state.ask_levels:
        return None
    bid, bid_qty = state.bid_levels[0]
    ask, ask_qty = state.ask_levels[0]
    if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in (bid, ask, bid_qty, ask_qty)):
        return None
    if bid >= ask:
        return None
    return (bid * ask_qty + ask * bid_qty) / (bid_qty + ask_qty)


def microprice_fields(state):
    value = calculate_microprice(state)
    displacement = value - state.mid_price if value is not None else None
    return {
        'microprice': value,
        'microprice_minus_mid': displacement,
        'microprice_displacement_bps': displacement / state.mid_price * 10000 if value is not None else None,
        'microprice_valid': value is not None,
        'microprice_provenance': 'COMPUTED_FROM_BOOK' if value is not None else 'UNAVAILABLE',
    }


def calculate_microprice_displacement(state, microprice):
    return microprice - state.mid_price if microprice is not None and state.mid_price else None
