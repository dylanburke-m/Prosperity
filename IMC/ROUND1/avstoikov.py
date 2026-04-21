from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import math

ACO       = "ASH_COATED_OSMIUM"
LIMIT     = 80
ACO_FAIR  = 10_000

# ── Avellaneda-Stoikov parameters ─────────────────────────────────────────────
GAMMA       = 0.15    # risk-aversion: higher → quotes skew more aggressively with inventory
SIGMA_FLOOR = 0.5     # minimum assumed volatility (prevents division-by-zero / flat quotes)
VOL_WINDOW  = 15      # rolling window for σ estimation from wall_mid history

# ── Edge / take parameters ────────────────────────────────────────────────────
TAKE_EDGE   = 5       # only aggress if market is ≥5 ticks mispriced vs fair
MIN_EDGE    = 2       # passive quotes must be at least this many ticks from fair value
                      # (prevents near-zero recovery trades)

# ── Lean thresholds (fraction of LIMIT) ──────────────────────────────────────
BUY_LEAN    = 0.60    # above 60% long → stop posting bids
SEL_LEAN    = 0.60    # above 60% short → stop posting asks


def bb(od: OrderDepth):
    return max(od.buy_orders) if od.buy_orders else None

def ba(od: OrderDepth):
    return min(od.sell_orders) if od.sell_orders else None

def clip(qty: int, pos: int, side: str) -> int:
    room = (LIMIT - pos) if side == "buy" else (LIMIT + pos)
    return max(0, min(abs(qty), room))


def compute_sigma(wm_history: list) -> float:
    """Rolling σ from wall_mid tick-to-tick differences."""
    if len(wm_history) < 3:
        return SIGMA_FLOOR
    diffs = [wm_history[i] - wm_history[i-1] for i in range(1, len(wm_history))]
    n    = len(diffs)
    mean = sum(diffs) / n
    var  = sum((d - mean) ** 2 for d in diffs) / max(n - 1, 1)
    return max(math.sqrt(var), SIGMA_FLOOR)


def wall_mid(od: OrderDepth):
    """Average of the outermost bid/ask walls — stable fair value proxy."""
    bid_wall = min(od.buy_orders)  if od.buy_orders  else None
    ask_wall = max(od.sell_orders) if od.sell_orders else None
    if bid_wall is not None and ask_wall is not None:
        return (bid_wall + ask_wall) / 2
    return None


def trade_aco(state: TradingState, orders: list, last_td: dict, new_td: dict):
    od = state.order_depths.get(ACO)
    if od is None:
        return

    pos     = state.position.get(ACO, 0)
    fair    = ACO_FAIR
    mkt_bid = bb(od)
    mkt_ask = ba(od)
    wm      = wall_mid(od)

    # ── 1. Rolling volatility ─────────────────────────────────────────────────
    wm_hist = last_td.get('aco_wm_hist', [])
    if wm is not None:
        wm_hist.append(wm)
        if len(wm_hist) > VOL_WINDOW + 1:
            wm_hist = wm_hist[-(VOL_WINDOW + 1):]
    new_td['aco_wm_hist'] = wm_hist

    sigma = compute_sigma(wm_hist)

    # ── 2. Reservation price (Avellaneda-Stoikov) ────────────────────────────
    # Long inventory → r shifts down  (lean toward selling)
    # Short inventory → r shifts up   (lean toward buying)
    reservation = fair - pos * GAMMA * (sigma ** 2)

    # ── 3. Aggressive take — only when market is clearly mispriced ───────────
    if mkt_ask is not None and mkt_ask < fair - TAKE_EDGE:
        qty = clip(-od.sell_orders[mkt_ask], pos, "buy")
        if qty > 0:
            orders.append(Order(ACO, mkt_ask, qty))
            pos += qty

    if mkt_bid is not None and mkt_bid > fair + TAKE_EDGE:
        qty = clip(od.buy_orders[mkt_bid], pos, "sell")
        if qty > 0:
            orders.append(Order(ACO, mkt_bid, -qty))
            pos -= qty

    # ── 4. Passive quotes anchored to reservation price ──────────────────────
    bid_wall_px = min(od.buy_orders)  if od.buy_orders  else None
    ask_wall_px = max(od.sell_orders) if od.sell_orders else None

    # Start 1 tick inside the walls
    bid_quote = int(bid_wall_px) + 1 if bid_wall_px else int(fair) - 7
    ask_quote = int(ask_wall_px) - 1 if ask_wall_px else int(fair) + 7

    # Overbid the best resting bid that is still BELOW reservation
    for bp, bv in sorted(od.buy_orders.items(), reverse=True):
        if bp < reservation:
            bid_quote = max(bid_quote, int(bp) + 1 if bv > 1 else int(bp))
            break

    # Underask the best resting ask that is still ABOVE reservation
    for ap, av in sorted(od.sell_orders.items()):
        if ap > reservation:
            ask_quote = min(ask_quote, int(ap) - 1 if av > 1 else int(ap))
            break

    # ── 5. Enforce minimum edge from fair value (recovery floor) ─────────────
    # This is the key fix: never post closer than MIN_EDGE ticks to fair.
    # Prevents near-zero recovery fills that drag down your average.
    bid_quote = min(bid_quote, int(fair) - MIN_EDGE)
    ask_quote = max(ask_quote, int(fair) + MIN_EDGE)

    # ── 6. Lean thresholds — stop quoting one side when too skewed ───────────
    buy_lean_lvl =  LIMIT * BUY_LEAN   #  +48
    sel_lean_lvl = -LIMIT * SEL_LEAN   #  -48

    # Safety: always keep quotes on the correct side of fair
    bid_quote = min(bid_quote, int(fair) - 1)
    ask_quote = max(ask_quote, int(fair) + 1)

    if bid_quote >= ask_quote:
        ask_quote = bid_quote + 1

    # Post with full remaining capacity, but stop one side when leaning hard
    bq = clip(LIMIT, pos, "buy")
    aq = clip(LIMIT, pos, "sell")

    if pos <= buy_lean_lvl and bq > 0:
        orders.append(Order(ACO, bid_quote, bq))
    if pos >= sel_lean_lvl and aq > 0:
        orders.append(Order(ACO, ask_quote, -aq))


# ── Trader wrapper (drop-in replacement) ─────────────────────────────────────
class Trader:
    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        import json

        last_td = {}
        try:
            if state.traderData:
                last_td = json.loads(state.traderData)
        except Exception:
            pass

        new_td  = {}
        result: Dict[str, List[Order]] = {}

        aco_orders: List[Order] = []
        if ACO in state.order_depths:
            trade_aco(state, aco_orders, last_td, new_td)
            result[ACO] = aco_orders

        return result, 0, json.dumps(new_td)