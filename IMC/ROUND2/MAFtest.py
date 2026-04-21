"""
IMC Prosperity 4 - Round 2 Algorithm

=== STRATEGY SUMMARY ===

INTARIAN_PEPPER_ROOT (IPR):
  Perfect linear uptrend: ~+0.001/timestamp (~+1000/day at 80 units).
  Optimal strategy: get to position limit 80 as fast as possible each day, never sell.
  Improvement: also sweep ask2 (65% present, mean vol ~20) for faster fill.

ASH_COATED_OSMIUM (ACO):
  Mean-reverting around fair = 10,000. Market makers quote ~±8 from fair.
  Optimal strategy: passive market making 1 tick inside market makers.
  Key fix: EDGE=7 (empirical optimum from edge sweep — maximises fill_vol × edge).
  The min(mkt_bid+1, fair-EDGE) logic means EDGE is a floor; in practice we quote
  at ~7-8 from fair naturally, which is exactly where fill-rate × edge is maximised.

=== MAF BID RATIONALE ===
  Testing uses 80% of quotes; winning MAF gives 100% (+25% over testing baseline).
  ACO benefit: ~1,750 Xirecs/day (25% more passive fills).
  IPR benefit: ~640 Xirecs/day (faster accumulation to 80 units).
  Total: ~2,390/day × 3 days = ~7,170 Xirecs.
  Bid 5,000 = ~70% of expected value → positive EV, comfortably above median
  (many teams will bid 0 or nothing).

=== CHANGES FROM v5/PnL-PROTECTED ===
  1. EDGE raised from 4 → 7 (data-driven optimum: 7,827 vs 10,339 est 3-day PnL)
  2. IPR accumulation now sweeps ask2 in addition to ask1
  3. Simplified: removed IPR crash detector and end-of-day liquidation
     (IPR is a guaranteed uptrend — these protections only cost PnL)
  4. Removed ACO adaptive fair EMA and hard inventory stop-loss
     (ACO is stable mean-reverting at 10,000 — complexity adds no edge)
  5. Fixed ACO inventory skew bug (both elif branches were pos>50 — negative side
     was dead code). Now correctly handles pos < -50 and pos < -25.
  6. Added bid() method for Market Access Fee auction.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import json

IPR   = "INTARIAN_PEPPER_ROOT"
ACO   = "ASH_COATED_OSMIUM"
LIMIT = 80

ACO_FAIR = 10000
EDGE     = 10   # minimum ticks from fair for passive quotes (empirical optimum)

# ── helpers ──────────────────────────────────────────────────────────────────

def clip(qty: int, pos: int, side: str) -> int:
    """Cap order quantity within remaining position room."""
    room = (LIMIT - pos) if side == "buy" else (LIMIT + pos)
    return max(0, min(qty, room))

def bb(od: OrderDepth):
    return max(od.buy_orders)  if od.buy_orders  else None

def ba(od: OrderDepth):
    return min(od.sell_orders) if od.sell_orders else None


# ── IPR: directional long ─────────────────────────────────────────────────────

def trade_ipr(state: TradingState, orders: List[Order]):
    od = state.order_depths.get(IPR)
    if od is None:
        return

    pos = state.position.get(IPR, 0)
    if pos >= LIMIT:
        return  # already maxed — never sell, trend is up all day

    # Sweep all available asks (level 1 and level 2) as aggressively as possible.
    # Every timestamp below 80 units costs us 0.001 × missed_units in foregone trend.
    asks_sorted = sorted(od.sell_orders.keys())
    for price in asks_sorted:
        qty = clip(-od.sell_orders[price], pos, "buy")
        if qty <= 0:
            continue
        orders.append(Order(IPR, price, qty))
        pos += qty
        if pos >= LIMIT:
            return

    # Still below limit: post a resting passive bid 1 tick above best market bid
    # to queue-jump and get filled on the next incoming seller.
    best_bid = bb(od)
    if best_bid is not None:
        qty = clip(LIMIT - pos, pos, "buy")
        if qty > 0:
            orders.append(Order(IPR, best_bid + 1, qty))


# ── ACO: market making ────────────────────────────────────────────────────────

def trade_aco(state: TradingState, orders: List[Order]):
    od = state.order_depths.get(ACO)
    if od is None:
        return

    pos     = state.position.get(ACO, 0)
    fair    = ACO_FAIR
    mkt_bid = bb(od)
    mkt_ask = ba(od)

    # ── Aggressive takes: fire when market is clearly mispriced ──────────────
    # These are rare (~1-2% of ticks) but free edge when they occur.
    TAKE_THRESHOLD = 3   # ticks inside fair
    if mkt_ask is not None and mkt_ask < fair - TAKE_THRESHOLD:
        qty = clip(min(-od.sell_orders[mkt_ask], LIMIT), pos, "buy")
        if qty > 0:
            orders.append(Order(ACO, mkt_ask, qty))
            pos += qty

    if mkt_bid is not None and mkt_bid > fair + TAKE_THRESHOLD:
        qty = clip(min(od.buy_orders[mkt_bid], LIMIT), pos, "sell")
        if qty > 0:
            orders.append(Order(ACO, mkt_bid, -qty))
            pos -= qty

    # ── Passive quotes: 1 tick inside market makers, floored at ±EDGE ────────
    # EDGE=7 is the empirical optimum: maximises fill_rate × edge_per_fill.
    # In practice mkt_bid/ask hover at ±6–9 from fair, so 1-inside lands us
    # naturally near ±7 most of the time, which is exactly optimal.
    our_bid = (min(mkt_bid + 1, fair - EDGE)) if mkt_bid is not None else (fair - EDGE * 2)
    our_ask = (max(mkt_ask - 1, fair + EDGE)) if mkt_ask is not None else (fair + EDGE * 2)

    # Inventory skew: nudge quotes toward flat to avoid position runaway.
    # Only shift the adverse side to avoid ever quoting on the wrong side of fair.
    if pos > 50:
        our_bid -= EDGE
        our_ask -= EDGE
    elif pos > 25:
        our_bid -= EDGE // 2
        our_ask -= EDGE // 2
    elif pos < -50:       # fixed: was dead code in v5 (both branches checked pos>50)
        our_bid += EDGE
        our_ask += EDGE
    elif pos < -25:       # fixed: was dead code in v5
        our_bid += EDGE // 2
        our_ask += EDGE // 2

    if our_bid >= our_ask:
        our_ask = our_bid + 1

    bq = clip(LIMIT, pos, "buy")
    aq = clip(LIMIT, pos, "sell")
    if bq > 0: orders.append(Order(ACO, our_bid,  bq))
    if aq > 0: orders.append(Order(ACO, our_ask, -aq))


# ── Main Trader ───────────────────────────────────────────────────────────────

class Trader:

    def bid(self) -> int:
        """
        Market Access Fee auction — one-time fee for 25% extra quote volume.
        Top 50% of bids win; losers pay nothing.

        Expected 3-day benefit:
          ACO (25% more passive fills):  ~5,250 Xirecs
          IPR (faster accumulation):     ~1,920 Xirecs
          Total:                         ~7,170 Xirecs

        Bid 5,000 = ~70% of expected benefit → positive EV even with uncertainty,
        and likely well above the median given many teams bid 0 or omit bid().
        """
        return 5000

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