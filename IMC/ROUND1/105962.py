"""
IMC Prosperity 4 - Round 1 FINAL Algorithm (v5)

After exhaustive analysis (15 different approaches covering FFT, regression,
autocorrelation, microstructure, order book reconstruction, simulation, and
mathematical modeling), here is the definitive picture:

=== MARKET STRUCTURE (CONFIRMED) ===

INTARIAN_PEPPER_ROOT (IPR):
  Formula: price = 0.001000 × timestamp + 11999.94
  Residual noise: std=2.46, ZERO autocorrelation (unpredictable)
  Market makers: quote at ±6.5 from fair (spread ≈ 13)
  Volume: ~215 units/day across ~32 trades
  OPTIMAL: Hold 80 units. Never sell. Every unit held earns 0.001/timestamp.

ASH_COATED_OSMIUM (ACO):
  Fair value: 10000 (constant, mean-reverting)
  Sine component: A=2.2, period≈82,000 timestamps, R²=20% (real but modest)
  Market makers: quote at ±8 from fair (spread ≈ 16)
  Volume: ~386 units/day across ~67 trades
  Half-life of deviations: ~67 timestamps (very fast reversion)
  OPTIMAL: Quote 1 tick inside market makers. Captures 91% of max spread.

=== THREE-DAY P&L PROJECTION ===

Day 0 (actual): IPR=7,286, ACO=2,658, Total=9,944
Day 1 estimate: IPR≈7,400, ACO≈2,750, Total≈10,150  
Day 2 estimate: IPR≈7,400, ACO≈2,750, Total≈10,150
Cumulative algo: ≈30,244

=== PATH TO 200k ===

The algorithmic ceiling is ~32k over 3 days (price rises only ~300 over 3 days).
The 200k target REQUIRES the manual auction:
  DRYLAND_FLAX: bid price=30, quantity=as large as possible
  EMBER_MUSHROOM: bid price=19, quantity=as large as possible

Even at clearing price=20 with 10,000 units of FLAX: profit = 10 × 10,000 = 100,000

=== V5 IMPROVEMENTS OVER V4 ===

1. ACO aggressive take threshold lowered: 5 → 3 ticks (captures rare dips/spikes)
2. IPR: if position < 80 at any point (e.g. start of new day), sweep book instantly
3. Code simplified and documented for clarity
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List

IPR   = "INTARIAN_PEPPER_ROOT"
ACO   = "ASH_COATED_OSMIUM"
LIMIT = 80
ACO_FAIR = 10000


def clip(qty: int, pos: int, side: str) -> int:
    room = (LIMIT - pos) if side == "buy" else (LIMIT + pos)
    return max(0, min(qty, room))

def bb(od: OrderDepth):
    return max(od.buy_orders) if od.buy_orders else None

def ba(od: OrderDepth):
    return min(od.sell_orders) if od.sell_orders else None


# ──────────────────────────────────────────────────────────────────────────────
# IPR: Pure directional long. Never sell. Sweep book immediately if below 80.
# ──────────────────────────────────────────────────────────────────────────────

def trade_ipr(state: TradingState, orders: List[Order]):
    od = state.order_depths.get(IPR)
    if od is None:
        return

    pos = state.position.get(IPR, 0)
    if pos >= LIMIT:
        return  # Already maxed — never sell.

    best_ask = ba(od)
    if best_ask is None:
        return

    # Sweep ALL asks up to +20 ticks above best ask.
    # Every tick below 80 costs 0.001 per unit — paying a small premium is worthwhile.
    ceiling = best_ask + 20
    for price in sorted(od.sell_orders.keys()):
        if price > ceiling:
            break
        qty = clip(-od.sell_orders[price], pos, "buy")
        if qty <= 0:
            break
        orders.append(Order(IPR, price, qty))
        pos += qty
        if pos >= LIMIT:
            return

    # Still need more? Post a resting bid 1 tick above best market bid.
    market_bid = bb(od)
    if market_bid is not None:
        qty = clip(LIMIT - pos, pos, "buy")
        if qty > 0:
            orders.append(Order(IPR, market_bid + 1, qty))


# ──────────────────────────────────────────────────────────────────────────────
# ACO: Market make 1 tick inside market makers. Captures 91% of full spread.
#      Inventory skew keeps position from drifting far from flat.
# ──────────────────────────────────────────────────────────────────────────────

def trade_aco(state: TradingState, orders: List[Order]):
    od = state.order_depths.get(ACO)
    if od is None:
        return

    pos      = state.position.get(ACO, 0)
    fair     = ACO_FAIR
    mkt_bid  = bb(od)
    mkt_ask  = ba(od)

    # ── Aggressive take: fire when market is clearly mispriced (threshold=3) ──
    # Market ask < fair - 3 = 9997 → someone selling at a significant discount → BUY
    if mkt_ask is not None and mkt_ask < fair - 3:
        qty = clip(min(-od.sell_orders[mkt_ask], LIMIT), pos, "buy")
        if qty > 0:
            orders.append(Order(ACO, mkt_ask, qty))
            pos += qty

    # Market bid > fair + 3 = 10003 → someone buying at a premium → SELL
    if mkt_bid is not None and mkt_bid > fair + 3:
        qty = clip(min(od.buy_orders[mkt_bid], LIMIT), pos, "sell")
        if qty > 0:
            orders.append(Order(ACO, mkt_bid, -qty))
            pos -= qty

    # ── Passive quotes: 1 tick inside market makers ────────────────────────────
    our_bid = min(mkt_bid + 1, fair - 1) if mkt_bid else fair - 7
    our_ask = max(mkt_ask - 1, fair + 1) if mkt_ask else fair + 7

    # Inventory skew: nudge quotes to encourage position flattening.
    # Helps avoid getting stuck at position extremes.
    if   pos >  50: our_bid -= 2; our_ask -= 2
    elif pos >  25: our_bid -= 1; our_ask -= 1
    elif pos < -50: our_bid += 2; our_ask += 2
    elif pos < -25: our_bid += 1; our_ask += 1

    if our_bid >= our_ask:
        our_ask = our_bid + 1

    bq = clip(LIMIT, pos, "buy")
    aq = clip(LIMIT, pos, "sell")
    if bq > 0: orders.append(Order(ACO, our_bid,  bq))
    if aq > 0: orders.append(Order(ACO, our_ask, -aq))


# ──────────────────────────────────────────────────────────────────────────────
# Main Trader
# ──────────────────────────────────────────────────────────────────────────────

class Trader:
    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}

        ipr_orders: List[Order] = []
        aco_orders: List[Order] = []

        if IPR in state.order_depths:
            trade_ipr(state, ipr_orders)
            result[IPR] = ipr_orders

        if ACO in state.order_depths:
            trade_aco(state, aco_orders)
            result[ACO] = aco_orders

        return result, 0, ""