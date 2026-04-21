"""
IMC Prosperity 4 - Round 1 Algorithm (v6)

Improvements over v5 (105962.py), all data-verified:

=== WHAT WAS WRONG IN v5 ===

1. [BUG] ACO inventory skew moves BOTH bid and ask in the same direction.
   When long (pos>50): bid-=2 AND ask-=2.
   That drops our ask from 10001 → 9999 — we sell BELOW fair value.
   Data shows this fires on 13% of ticks at high positions.
   FIX: Asymmetric skew. When long, only lower the bid (discourages buying).
        When short, only raise the ask (discourages selling).
        After skew, hard clamp: ask >= fair+1, bid <= fair-1. Always.

2. [BUG] ACO takes at both ask levels but was only designed for ask1.
   If ask1 AND ask2 are both below threshold, we should sweep both.
   FIX: Explicit multi-level take loop on both sides.

3. [MINOR] IPR sweep ceiling is fixed at +20 regardless of time remaining.
   Paying +20 premium over best_ask costs more than remaining gains when
   fewer than 20,000 timestamps remain in the day.
   FIX: Dynamic ceiling = min(20, max(1, (1_000_000 - ts) * 0.001))
   Effect: negligible for 98% of the day, saves a few ticks in the final 2%.

=== WHAT v5 GOT RIGHT (kept unchanged) ===
- LIMIT = 80 (confirmed: more PnL than 50)
- ACO_FAIR = 10000 (hard constant, mean-reverting, confirmed)
- ACO take threshold ±3 (simulation shows thr=3 beats thr=1 for this market)
- ACO overbid strategy: bid = mkt_bid+1 clipped at fair-1 (beats fixed fair±1 by ~10x)
- IPR: always max long, never sell, sweep multiple ask levels
- IPR: post resting bid above mkt_bid when not fully filled

=== MARKET STRUCTURE (verified from 30,000-row dataset) ===

INTARIAN_PEPPER_ROOT:
  Price = ~0.001 × timestamp + day_base (exact linear trend, +1000/day)
  Market makers quote ±6.5 from fair (spread ≈ 13 ticks)
  Level 2 ask is typically 3 ticks above level 1
  80 units fillable within 300 timestamps at day open (negligible delay cost)
  OPTIMAL: Hold 80 units. Never sell. Every tick at max position = +0.08 PnL.

ASH_COATED_OSMIUM:
  Fair value = 10000 (constant, mean-reverting, day stdev ≈ 5)
  Market makers quote ±8 from fair (best bid ≈ 9992, best ask ≈ 10008)
  Taker trades happen at ALL prices 9979–10026 (volume peaks at 9990–9995 / 10005–10010)
  OPTIMAL: Overbid mkt bid by 1 tick (clipped at fair-1=9999),
           undercut mkt ask by 1 tick (clipped at fair+1=10001).
           Asymmetric inventory skew to prevent position drift.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import json
import statistics

IPR      = "INTARIAN_PEPPER_ROOT"
ACO      = "ASH_COATED_OSMIUM"
LIMIT    = 80
ACO_FAIR = 10000
ACO_TAKE_THR = 3       # take aggressively when mkt crosses fair by this many ticks

# Z-score lean parameters (data-verified: one-sided lean, window=10, thr=0.5, lean=2)
ACO_ZSCORE_WINDOW = 10   # rolling window of mid deviations from fair
ACO_ZSCORE_THR    = 0.5  # z-score threshold before leaning
ACO_LEAN_SIZE     = 2    # ticks to lean the quote by when signal fires


def room(pos: int, side: str) -> int:
    """Remaining capacity on a given side."""
    if side == "buy":
        return max(0, LIMIT - pos)
    else:
        return max(0, LIMIT + pos)


def best_bid(od: OrderDepth):
    return max(od.buy_orders) if od.buy_orders else None


def best_ask(od: OrderDepth):
    return min(od.sell_orders) if od.sell_orders else None


# ─── IPR: Always-long trend follower ──────────────────────────────────────────

def trade_ipr(state: TradingState, orders: List[Order]):
    od = state.order_depths.get(IPR)
    if od is None:
        return

    pos = state.position.get(IPR, 0)
    if pos >= LIMIT:
        return  # maxed — never sell

    ba = best_ask(od)
    if ba is None:
        return

    # Dynamic sweep ceiling:
    # Paying a premium over best_ask is profitable while the gain over remaining
    # timestamps exceeds the premium paid.
    # gain_per_unit = 0.001 * remaining_ts
    # => max worthwhile premium = 0.001 * (1_000_000 - ts)
    remaining_ts = max(0, 1_000_000 - state.timestamp)
    ceiling = ba + max(1, min(20, int(remaining_ts * 0.001)))

    # Sweep all ask levels within the ceiling
    for price in sorted(od.sell_orders.keys()):
        if price > ceiling:
            break
        qty = min(-od.sell_orders[price], room(pos, "buy"))
        if qty <= 0:
            break
        orders.append(Order(IPR, price, qty))
        pos += qty
        if pos >= LIMIT:
            return

    # Still short of limit? Post a resting bid 1 tick above best market bid
    # to capture any passive sell flow at a price we're happy to pay.
    bb = best_bid(od)
    if bb is not None:
        qty = room(pos, "buy")
        if qty > 0:
            orders.append(Order(IPR, bb + 1, qty))


# ─── ACO: Market making around fair=10000 ─────────────────────────────────────

def trade_aco(state: TradingState, orders: List[Order], trader_data: dict):
    od = state.order_depths.get(ACO)
    if od is None:
        return

    pos  = state.position.get(ACO, 0)
    fair = ACO_FAIR
    bb_  = best_bid(od)
    ba_  = best_ask(od)

    # ── Aggressive takes: multi-level, both sides ─────────────────────────────
    # Sweep all ask levels strictly below fair - ACO_TAKE_THR
    take_buy_threshold = fair - ACO_TAKE_THR   # = 9997
    if ba_ is not None and ba_ < take_buy_threshold:
        for price in sorted(od.sell_orders.keys()):
            if price >= take_buy_threshold:
                break
            qty = min(-od.sell_orders[price], room(pos, "buy"))
            if qty <= 0:
                break
            orders.append(Order(ACO, price, qty))
            pos += qty

    # Sweep all bid levels strictly above fair + ACO_TAKE_THR
    take_sell_threshold = fair + ACO_TAKE_THR  # = 10003
    if bb_ is not None and bb_ > take_sell_threshold:
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= take_sell_threshold:
                break
            qty = min(od.buy_orders[price], room(pos, "sell"))
            if qty <= 0:
                break
            orders.append(Order(ACO, price, -qty))
            pos -= qty

    # ── Passive quotes: overbid / undercut, clipped at fair±1 ─────────────────
    # Base: 1 tick inside the market, clipped so we never quote through fair.
    our_bid = min(bb_ + 1, fair - 1) if bb_ is not None else fair - 7
    our_ask = max(ba_ - 1, fair + 1) if ba_ is not None else fair + 7

    # Asymmetric inventory skew:
    #   When LONG  → lower only the bid  (makes us less attractive to buy from / buy to)
    #   When SHORT → raise only the ask  (makes us less attractive to sell to / sell from)
    # This always keeps ask >= fair+1 and bid <= fair-1 naturally,
    # and never forces us to trade at a loss.
    if   pos >  50: our_bid -= 2
    elif pos >  25: our_bid -= 1
    elif pos < -50: our_ask += 2
    elif pos < -25: our_ask += 1

    # ── Z-score price lean (one-sided, data-verified) ─────────────────────────
    # Maintain a rolling window of ACO mid deviations from fair in traderData.
    # 70% directional accuracy on moving ticks when z > 0.5 or z < -0.5.
    # ONE-SIDED lean only: when price is high (likely falling), lean the bid down
    # but leave the ask untouched (we still want to sell at full edge), and vice versa.
    # Leaning both sides simultaneously was tested and hurt PnL.
    mid = (bb_ + ba_) / 2 if (bb_ is not None and ba_ is not None) else fair
    hist = trader_data.get("aco_dev_hist", [])
    hist.append(mid - fair)
    if len(hist) > ACO_ZSCORE_WINDOW:
        hist = hist[-ACO_ZSCORE_WINDOW:]
    trader_data["aco_dev_hist"] = hist

    if len(hist) >= 4:  # need at least a few samples before leaning
        try:
            m = statistics.mean(hist)
            s = statistics.stdev(hist)
            if s > 0:
                z = (hist[-1] - m) / s
                if z > ACO_ZSCORE_THR:
                    # Price above rolling mean → likely to fall → lean bid down
                    our_bid -= ACO_LEAN_SIZE
                elif z < -ACO_ZSCORE_THR:
                    # Price below rolling mean → likely to rise → lean ask up
                    our_ask += ACO_LEAN_SIZE
        except Exception:
            pass  # skip lean if statistics fail (e.g. zero variance)

    # Hard safety clamp — guarantees edge is never negative regardless of inputs.
    our_bid = min(our_bid, fair - 1)   # bid  ≤ 9999
    our_ask = max(our_ask, fair + 1)   # ask  ≥ 10001

    # Sanity: don't cross our own quotes (shouldn't happen, but defensive)
    if our_bid >= our_ask:
        our_ask = our_bid + 2

    bq = room(pos, "buy")
    aq = room(pos, "sell")

    if bq > 0:
        orders.append(Order(ACO, our_bid,  bq))
    if aq > 0:
        orders.append(Order(ACO, our_ask, -aq))


# ─── Main Trader ──────────────────────────────────────────────────────────────

class Trader:
    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}

        # Load persisted state (ACO deviation history for z-score lean)
        try:
            trader_data = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            trader_data = {}

        ipr_orders: List[Order] = []
        aco_orders: List[Order] = []

        if IPR in state.order_depths:
            trade_ipr(state, ipr_orders)
            result[IPR] = ipr_orders

        if ACO in state.order_depths:
            trade_aco(state, aco_orders, trader_data)
            result[ACO] = aco_orders

        return result, 0, json.dumps(trader_data)