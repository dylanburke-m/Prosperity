"""
IMC Prosperity 4 - Round 1 FINAL Algorithm (v5 + Drawdown Protection)

=== DRAWDOWN RISKS IDENTIFIED (from historical data analysis) ===

1. IPR INTRADAY CRASH:
   IPR rises linearly at 0.001/timestamp within a day.
   If the price crashes intraday (e.g. a regime change in Prosperity 4),
   holding 80 units long exposes us to 80 × crash_magnitude SeaShell losses.
   Protection: Track a rolling EMA of price. If price drops sharply below
   EMA (CRASH_THRESHOLD ticks), start unwinding the long position aggressively.

2. ACO FAIR VALUE DRIFT:
   The market maker assumes ACO fair value = 10000 forever.
   If the true fair value shifts (e.g. a structural break), market-making
   accumulates inventory on the wrong side.
   Historical data shows ACO stayed within ±50 of 10000, but Prosperity 4
   may introduce new dynamics.
   Protection: Track a slow EMA of mid price. If mid persistently diverges
   from 10000 beyond DRIFT_THRESHOLD, shift the fair value estimate and
   add an inventory emergency-exit trigger.

3. ACO INVENTORY BLOWUP:
   The current skew logic has a bug: both elif branches check pos > 70,
   so the negative-side skew (pos < -70) is dead code.
   If we accumulate a large short position while price rises, losses mount.
   Protection: Fix the bug + add hard stop — if |pos| >= INVENTORY_STOP,
   immediately cross the spread to flatten rather than waiting for passive fills.

4. ACO PASSIVE QUOTE LOGIC BUG:
   `our_bid = min(mkt_bid + 1, fair - EDGE)` can fail when mkt_bid is None
   (the code doesn't guard this). Fix: None-guard on both passive quote lines.

=== ALL PROTECTION MECHANISMS ===

  A. IPR: EMA-based crash detector → unwind long if price drops > CRASH_THRESHOLD below EMA
  B. IPR: End-of-day liquidation window → sell all at ts >= LIQUIDATION_TS to avoid
          holding into a potential next-day reset (conservative: only if price below day-start)
  C. ACO: Fix dead-code inventory skew bug (was: elif pos > 70 twice → now correctly pos < -70)
  D. ACO: Adaptive fair value via slow EMA — tracks if 10000 assumption has drifted
  E. ACO: Hard inventory stop-loss — if |pos| >= INVENTORY_STOP, cross spread to flatten fast
  F. ACO: None-guard on passive quote lines to prevent crashes

=== UNCHANGED FROM v5 ===
  - IPR directional long accumulation logic
  - ACO aggressive take at ±3 ticks
  - EDGE = 4 for passive quoting
  - Position limits (LIMIT = 80)
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import json
import math

IPR      = "INTARIAN_PEPPER_ROOT"
ACO      = "ASH_COATED_OSMIUM"
LIMIT    = 80

# ── ACO fair value ──────────────────────────────────────────────────────────
ACO_FAIR_STATIC = 10000           # baseline assumption

# ── ACO inventory hard stop ─────────────────────────────────────────────────
INVENTORY_STOP   = 70             # if |pos| >= this, flatten aggressively
INVENTORY_RESUME = 50             # re-enable normal quoting once |pos| drops here

# ── ACO adaptive fair value (slow EMA) ─────────────────────────────────────
ACO_EMA_ALPHA    = 0.002          # ~500-tick half-life — slow enough to ignore noise
DRIFT_THRESHOLD  = 15             # if EMA deviates >15 from static fair, shift fair

# ── IPR crash detection (fast EMA) ─────────────────────────────────────────
IPR_EMA_ALPHA    = 0.05           # ~20-tick half-life — fast enough to catch crashes
CRASH_THRESHOLD  = 20             # if price drops > 20 below EMA, unwind begins
CRASH_UNWIND_QTY = 20             # units to sell per tick during unwind

# ── IPR end-of-day protective liquidation ───────────────────────────────────
LIQUIDATION_TS   = 980_000        # start selling at this timestamp (~last 2% of day)
                                  # Only triggers if price is below day-start price
                                  # (i.e. the day's directional bet has gone wrong)


# ── Helpers ─────────────────────────────────────────────────────────────────

def clip(qty: int, pos: int, side: str) -> int:
    room = (LIMIT - pos) if side == "buy" else (LIMIT + pos)
    return max(0, min(qty, room))

def bb(od: OrderDepth):
    return max(od.buy_orders)  if od.buy_orders  else None

def ba(od: OrderDepth):
    return min(od.sell_orders) if od.sell_orders else None

def mid_price(od: OrderDepth):
    b, a = bb(od), ba(od)
    if b and a: return (b + a) / 2
    if b: return float(b)
    if a: return float(a)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# IPR — Directional long with crash protection
# ──────────────────────────────────────────────────────────────────────────────

def trade_ipr(state: TradingState, orders: List[Order], td_in: dict, td_out: dict):
    od = state.order_depths.get(IPR)
    if od is None:
        return

    pos      = state.position.get(IPR, 0)
    ts       = state.timestamp
    cur_mid  = mid_price(od)

    # ── Update fast EMA for crash detection ─────────────────────────────────
    # Seed EMA with first observed price so early ticks don't trigger false alarms
    prev_ema = td_in.get("ipr_ema", cur_mid)
    if prev_ema is None:
        prev_ema = cur_mid
    ipr_ema  = None
    if cur_mid is not None:
        ipr_ema = IPR_EMA_ALPHA * cur_mid + (1 - IPR_EMA_ALPHA) * prev_ema
    else:
        ipr_ema = prev_ema
    td_out["ipr_ema"] = ipr_ema

    # ── Track day-start price (reset when ts is near 0) ─────────────────────
    day_start_price = td_in.get("ipr_day_start")
    if ts <= 200 or day_start_price is None:
        # First ticks of new day: record starting price
        day_start_price = cur_mid if cur_mid else day_start_price
    td_out["ipr_day_start"] = day_start_price

    # ── PROTECTION A: Crash detector ─────────────────────────────────────────
    # If price has dropped sharply below the fast EMA, start unwinding the long.
    in_crash = False
    if cur_mid is not None and ipr_ema is not None and pos > 0:
        deviation = ipr_ema - cur_mid      # positive = price below EMA
        if deviation >= CRASH_THRESHOLD:
            in_crash = True
            # Sell aggressively — cross the spread to exit fast
            best_bid = bb(od)
            if best_bid is not None:
                unwind_qty = min(CRASH_UNWIND_QTY, pos)
                orders.append(Order(IPR, best_bid, -unwind_qty))
            # Do NOT add any buy orders this tick
            td_out["ipr_ema"] = ipr_ema   # persist updated EMA
            return

    # ── PROTECTION B: End-of-day protective liquidation ──────────────────────
    # If we're near the end of the day AND price is below where the day started,
    # the directional bet has failed — exit cleanly rather than holding into reset.
    if ts >= LIQUIDATION_TS and pos > 0 and day_start_price is not None and cur_mid is not None:
        if cur_mid < day_start_price:
            best_bid = bb(od)
            if best_bid is not None:
                # Sell full position at best bid (accept crossing spread to exit)
                orders.append(Order(IPR, best_bid, -pos))
            return

    # ── Normal accumulation (unchanged from v5) ──────────────────────────────
    if pos >= LIMIT:
        return  # Already maxed.

    best_ask = ba(od)
    if best_ask is None:
        return

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

    market_bid = bb(od)
    if market_bid is not None:
        qty = clip(LIMIT - pos, pos, "buy")
        if qty > 0:
            orders.append(Order(IPR, market_bid + 1, qty))


# ──────────────────────────────────────────────────────────────────────────────
# ACO — Market making with fair-value adaptation and inventory stop-loss
# ──────────────────────────────────────────────────────────────────────────────

def trade_aco(state: TradingState, orders: List[Order], td_in: dict, td_out: dict):
    od = state.order_depths.get(ACO)
    if od is None:
        return

    pos     = state.position.get(ACO, 0)
    mkt_bid = bb(od)
    mkt_ask = ba(od)
    cur_mid = mid_price(od)

    # ── PROTECTION D: Adaptive fair value via slow EMA ───────────────────────
    # If ACO's true fair value has genuinely shifted, we should update our estimate
    # rather than blindly quoting around the static 10000.
    prev_aco_ema = td_in.get("aco_ema", ACO_FAIR_STATIC)
    aco_ema      = prev_aco_ema
    if cur_mid is not None:
        aco_ema = ACO_EMA_ALPHA * cur_mid + (1 - ACO_EMA_ALPHA) * prev_aco_ema
    td_out["aco_ema"] = aco_ema

    # Blend: mostly trust static fair (10000) but nudge toward EMA if it drifts far
    drift = aco_ema - ACO_FAIR_STATIC
    if abs(drift) > DRIFT_THRESHOLD:
        # EMA has persistently moved — shift fair value toward EMA
        fair = ACO_FAIR_STATIC + (drift - math.copysign(DRIFT_THRESHOLD, drift))
    else:
        fair = ACO_FAIR_STATIC

    # ── PROTECTION E: Hard inventory stop-loss ────────────────────────────────
    # If position is extreme, stop quoting and cross spread to flatten.
    if pos >= INVENTORY_STOP:
        # Dangerously long: sell aggressively at best bid
        if mkt_bid is not None:
            qty = clip(pos - INVENTORY_RESUME, pos, "sell")
            if qty > 0:
                orders.append(Order(ACO, mkt_bid, -qty))
        return   # no new passive quotes while unwinding

    if pos <= -INVENTORY_STOP:
        # Dangerously short: buy aggressively at best ask
        if mkt_ask is not None:
            qty = clip(abs(pos) - INVENTORY_RESUME, pos, "buy")
            if qty > 0:
                orders.append(Order(ACO, mkt_ask, qty))
        return   # no new passive quotes while unwinding

    # ── Aggressive take (unchanged from v5, but uses adaptive fair) ──────────
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

    # ── Passive quotes: 1 tick inside market makers ───────────────────────────
    EDGE = 4

    # PROTECTION F: None-guard (v5 crashed if mkt_bid/mkt_ask was None)
    our_bid = (min(mkt_bid + 1, int(fair) - EDGE)) if mkt_bid is not None else (int(fair) - EDGE * 2)
    our_ask = (max(mkt_ask - 1, int(fair) + EDGE)) if mkt_ask is not None else (int(fair) + EDGE * 2)

    # PROTECTION C: Fixed inventory skew (v5 had a bug — both branches checked pos > 70,
    # so negative-side skew was dead code and shorts were never pushed back toward flat)
    if   pos >  50: our_bid -= EDGE;     our_ask -= EDGE
    elif pos >  25: our_bid -= EDGE // 2; our_ask -= EDGE // 2
    elif pos < -50: our_bid += EDGE;     our_ask += EDGE      # ← was unreachable in v5
    elif pos < -25: our_bid += EDGE // 2; our_ask += EDGE // 2  # ← was unreachable in v5

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

        # Load persisted state from previous tick
        try:
            td_in = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td_in = {}
        td_out = {}

        ipr_orders: List[Order] = []
        aco_orders: List[Order] = []

        if IPR in state.order_depths:
            trade_ipr(state, ipr_orders, td_in, td_out)
            result[IPR] = ipr_orders

        if ACO in state.order_depths:
            trade_aco(state, aco_orders, td_in, td_out)
            result[ACO] = aco_orders

        try:
            trader_data_out = json.dumps(td_out)
        except Exception:
            trader_data_out = ""

        return result, 0, trader_data_out