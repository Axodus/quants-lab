from __future__ import annotations
from decimal import Decimal
from typing import Optional
from .state import OrderBookState

def calculate_queue_imbalance(state: OrderBookState, levels: int = 1) -> Optional[Decimal]:
    if state.validity_state != "VALID":
        return None
    
    bid_depth = sum(qty for _, qty in state.bid_levels[:levels])
    ask_depth = sum(qty for _, qty in state.ask_levels[:levels])
    total_depth = bid_depth + ask_depth
    
    if total_depth == 0:
        return Decimal("0")
    
    return bid_depth / total_depth

def calculate_weighted_book_pressure(state: OrderBookState, levels: int = 5) -> tuple[Decimal, Decimal, Decimal]:
    if state.validity_state != "VALID":
        return Decimal("0"), Decimal("0"), Decimal("0")
        
    bid_pressure = Decimal("0")
    ask_pressure = Decimal("0")
    
    for i, (_, qty) in enumerate(state.bid_levels[:levels]):
        # simple distance weighting: 1 / (i + 1)
        bid_pressure += qty / Decimal(i + 1)
        
    for i, (_, qty) in enumerate(state.ask_levels[:levels]):
        ask_pressure += qty / Decimal(i + 1)
        
    net_pressure = bid_pressure - ask_pressure
    return bid_pressure, ask_pressure, net_pressure
