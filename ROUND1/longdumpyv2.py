"""
IMC Prosperity 4 - Round 1 FINAL Algorithm (v5) + Long Dump

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
  END-OF-DAY: Dump all long positions in the final DUMP_WINDOW timestamps.

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

=== LONG DUMP ===

In the final DUMP_WINDOW timestamps of each day (default: last 5,000, i.e.
timestamp > 994,900), all long positions are liquidated aggressively:
  - IPR: sell entire position at best available bid; if no bid, post at bid_wall - 1
  - ACO: any long position is closed at best bid; normal market-making is suppressed
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List

IPR      = "INTARIAN_PEPPER_ROOT"
ACO      = "ASH_COATED_OSMIUM"
LIMIT    = 80

# ACO fair-value bias: market makers cluster bids ~9990-9995, asks ~10008-10013,
# giving a wall mid of ~10001. Using 10001 instead of 10000 reduces adverse
# selection from quoting symmetrically around a stale fair value.
ACO_FAIR_BASE = 10001

# Timestamps run 0 – 999,900 each day (step = 100).
# DUMP_WINDOW controls how many timestamps before the end we start liquidating.
DAY_END     = 999_900
DUMP_WINDOW = 5_000           # start dumping at timestamp > DAY_END - DUMP_WINDOW


def is_dump_phase(timestamp: int) -> bool:
    """Return True during the end-of-day liquidation window."""
    return timestamp > DAY_END - DUMP_WINDOW


def clip(qty: int, pos: int, side: str) -> int:
    room = (LIMIT - pos) if side == "buy" else (LIMIT + pos)
    return max(0, min(qty, room))

def bb(od: OrderDepth):
    return max(od.buy_orders) if od.buy_orders else None

def ba(od: OrderDepth):
    return min(od.sell_orders) if od.sell_orders else None


# ──────────────────────────────────────────────────────────────────────────────
# IPR: Pure directional long during the day.
#      In the dump window: aggressively sell entire long position.
# ──────────────────────────────────────────────────────────────────────────────

def trade_ipr(state: TradingState, orders: List[Order], dumping: bool):
    od = state.order_depths.get(IPR)
    if od is None:
        return

    pos = state.position.get(IPR, 0)

    # ── END-OF-DAY DUMP ────────────────────────────────────────────────────────
    if dumping:
        if pos <= 0:
            return  # Nothing to dump.

        # Hit every available bid to exit as fast as possible.
        remaining = pos
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if remaining <= 0:
                break
            qty = min(od.buy_orders[price], remaining)
            orders.append(Order(IPR, price, -qty))
            remaining -= qty

        # If bids were insufficient, post a resting ask just inside the bid wall
        # to maximise fill probability before the session closes.
        if remaining > 0:
            best_bid = bb(od)
            post_price = (best_bid - 1) if best_bid is not None else ACO_FAIR
            orders.append(Order(IPR, post_price, -remaining))
        return

    # ── NORMAL ACCUMULATION PHASE ──────────────────────────────────────────────
    if pos >= LIMIT:
        return  # Already maxed — never sell during normal phase.

    best_ask = ba(od)
    if best_ask is None:
        return

    # Sweep ALL asks up to +20 ticks above best ask.
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
# ACO: Market make during the day.
#      In the dump window: close any long position; suppress new long quotes.
# ──────────────────────────────────────────────────────────────────────────────

def trade_aco(state: TradingState, orders: List[Order], dumping: bool):
    od = state.order_depths.get(ACO)
    if od is None:
        return

    pos     = state.position.get(ACO, 0)
    mkt_bid = bb(od)
    mkt_ask = ba(od)

    # FIX #2: Use a dynamic fair value derived from the wall mid (outermost
    # bid/ask levels), which empirically centres around 10001-10002 rather than
    # the hardcoded 10000. Fall back to ACO_FAIR_BASE when levels are absent.
    wall_bid = min(od.buy_orders.keys())  if od.buy_orders  else None
    wall_ask = max(od.sell_orders.keys()) if od.sell_orders else None
    if wall_bid is not None and wall_ask is not None:
        fair = (wall_bid + wall_ask) / 2
    else:
        fair = ACO_FAIR_BASE

    # ── END-OF-DAY DUMP ────────────────────────────────────────────────────────
    if dumping:
        if pos > 0:
            # Hit best bid to close long.
            if mkt_bid is not None:
                qty = min(pos, od.buy_orders[mkt_bid])
                orders.append(Order(ACO, mkt_bid, -qty))
                pos -= qty
            # Post a resting ask just below fair for any remainder.
            if pos > 0:
                orders.append(Order(ACO, int(fair) - 1, -pos))
        # Allow short positions to be closed but don't open new longs.
        elif pos < 0:
            if mkt_ask is not None and mkt_ask <= fair + 3:
                qty = clip(min(-od.sell_orders[mkt_ask], LIMIT), pos, "buy")
                if qty > 0:
                    orders.append(Order(ACO, mkt_ask, qty))
        return

    # ── NORMAL MARKET-MAKING PHASE ─────────────────────────────────────────────

    # FIX #3: Aggressive take — threshold relative to dynamic fair value.
    # Only fires on genuine mispricings (~1-3% of ticks).
    if mkt_ask is not None and mkt_ask < fair - 3:
        qty = clip(min(-od.sell_orders[mkt_ask], LIMIT), pos, "buy")
        if qty > 0:
            orders.append(Order(ACO, mkt_ask, qty))
            pos += qty

    if mkt_bid is not None and mkt_bid > fair + 3:
        qty = clip(min(od.buy_orders[mkt_bid], LIMIT), pos, "sell")
        if qty > 0:
            orders.append(Order(ACO, mkt_bid, -qty))
            pos -= qty

    # Passive quotes: 1 tick inside best bid/ask, bounded by fair ± 1.
    our_bid = min(mkt_bid + 1, int(fair) - 1) if mkt_bid else int(fair) - 7
    our_ask = max(mkt_ask - 1, int(fair) + 1) if mkt_ask else int(fair) + 7

    # Inventory skew: nudge quotes to encourage position flattening.
    if   pos >  50: our_bid -= 2; our_ask -= 2
    elif pos >  25: our_bid -= 1; our_ask -= 1
    elif pos < -50: our_bid += 2; our_ask += 2
    elif pos < -25: our_bid += 1; our_ask += 1

    if our_bid >= our_ask:
        our_ask = our_bid + 1

    # FIX #1 (CRITICAL): pos is not updated between aggressive takes and passive
    # quote sizing. Previously both bq and aq were clipped against the original
    # pos, allowing combined outstanding orders of up to 2×LIMIT (peak long=160).
    # Now we compute remaining buy/sell room after accounting for any aggressive
    # takes already submitted this tick.
    buy_room  = LIMIT - pos          # headroom left for long exposure
    sell_room = LIMIT + pos          # headroom left for short exposure

    bq = max(0, min(LIMIT, buy_room))
    aq = max(0, min(LIMIT, sell_room))

    if bq > 0: orders.append(Order(ACO, our_bid,  bq))
    if aq > 0: orders.append(Order(ACO, our_ask, -aq))


# ──────────────────────────────────────────────────────────────────────────────
# Main Trader
# ──────────────────────────────────────────────────────────────────────────────

class Trader:
    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}
        dumping = is_dump_phase(state.timestamp)

        ipr_orders: List[Order] = []
        aco_orders: List[Order] = []

        if IPR in state.order_depths:
            trade_ipr(state, ipr_orders, dumping)
            result[IPR] = ipr_orders

        if ACO in state.order_depths:
            trade_aco(state, aco_orders, dumping)
            result[ACO] = aco_orders

        return result, 0, ""