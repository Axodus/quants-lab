"""Rank-wise multi-level OFI; sum signed bid contribution minus ask contribution."""
from decimal import Decimal as D


def level_flow(previous, current, bid):
    if previous is None:
        return current[1] if current else D(0)
    if current is None:
        return -previous[1]
    p, q = previous
    price, qty = current
    improves = price > p if bid else price < p
    worsens = price < p if bid else price > p
    return qty if improves else (-q if worsens else qty-q)


def ofi(previous, current, depth):
    result = D(0)
    for side, sign in (('bid_levels', 1), ('ask_levels', -1)):
        old, new = getattr(previous, side), getattr(current, side)
        for i in range(depth):
            result += sign * level_flow(old[i] if i < len(old) else None, new[i] if i < len(new) else None, sign == 1)
    return result
