"""
IMC Prosperity 4 — v4: Overbid/Underask with Queue Priority
============================================================

Root cause of 960 ceiling
──────────────────────────
Previous versions clamped tomato quotes to ≤ best_bid / ≥ best_ask.
This means we TIED with existing bots but never BEAT them on price.
In Prosperity, price priority is strict — best price fills first.
By posting at best_bid+1 / best_ask-1 we become the top of each queue
and intercept the flow before anyone else.

Ground truth from trades CSV (2 days, round 0)
───────────────────────────────────────────────
Product   │ Total lots │ Trades at bid │ Trades at ask │ Theoretical edge
TOMATOES  │ 2 853      │ 75%           │ 25%           │ ~14 900
EMERALDS  │ 2 189      │ 51%           │ 49%           │ ~14 700

Emerald trades cluster at exactly 9992 (bid_1) and 10008 (ask_1).
→ Post at 9993 / 10007 to always have price priority.

Tomato trades scatter across ~100-tick range.
→ Post at best_bid+1 / best_ask-1 every tick for queue priority.
→ EMA-30 lean: suppress the buy side when price > EMA (flow is sellers),
   suppress the sell side when price < EMA (flow is buyers).
   This prevents accumulating inventory against the trend.

Strategy: TAKE first (any mispriced level), then MAKE with remainder.
"""

from datamodel import OrderDepth, TradingState, Order
import json

# ════════════════════════════ Constants ══════════════════════════════════════

EMERALD_SYMBOL = "EMERALDS"
TOMATO_SYMBOL  = "TOMATOES"

POS_LIMITS = {
    EMERALD_SYMBOL: 50,
    TOMATO_SYMBOL:  350,
}

# ── Emerald ───────────────────────────────────────────────────────────────────
EMERALD_FV         = 10_000
EMERALD_LEAN_FRAC  = 0.5      # skew quotes when |pos| > 50% of limit

# ── Tomato ────────────────────────────────────────────────────────────────────
TOMATO_EMA_WINDOW  = 30       # short window — tracks the wander closely
TOMATO_LEAN_THR    = 5        # start leaning quotes at this deviation
TOMATO_SUPPRESS    = 15       # suppress one full side at this deviation
TOMATO_GUARD_FRAC  = 0.8      # emergency unwind threshold
TOMATO_GUARD_TGT   = 0.4      # unwind back to this fraction of limit


# ════════════════════════ Base ProductTrader ═════════════════════════════════

class ProductTrader:

    def __init__(self, symbol, state, new_td, last_td):
        self.symbol   = symbol
        self.state    = state
        self.new_td   = new_td
        self.last_td  = last_td
        self.orders   = []

        self.pos_limit = POS_LIMITS.get(symbol, 50)
        self.position  = state.position.get(symbol, 0)
        self.buy_cap   = self.pos_limit - self.position
        self.sell_cap  = self.pos_limit + self.position

        od = state.order_depths.get(symbol, OrderDepth())
        self.bids = dict(sorted(
            {p: abs(v) for p, v in od.buy_orders.items()}.items(), reverse=True
        ))
        self.asks = dict(sorted(
            {p: abs(v) for p, v in od.sell_orders.items()}.items()
        ))

        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None
        self.bid_wall = min(self.bids) if self.bids else None
        self.ask_wall = max(self.asks) if self.asks else None
        self.mid = (
            (self.best_bid + self.best_ask) / 2
            if self.best_bid is not None and self.best_ask is not None else None
        )

    def _buy(self, price, volume):
        vol = min(int(abs(volume)), self.buy_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), vol))
            self.buy_cap -= vol
        return vol

    def _sell(self, price, volume):
        vol = min(int(abs(volume)), self.sell_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), -vol))
            self.sell_cap -= vol
        return vol

    def _ema(self, key, window, value):
        alpha = 2.0 / (window + 1)
        prev  = self.last_td.get(key, value)
        new   = alpha * value + (1.0 - alpha) * prev
        self.new_td[key] = new
        return new

    def get_orders(self):
        return self.orders


# ════════════════════════ Emerald Market Maker ═══════════════════════════════

class EmeraldTrader(ProductTrader):
    """
    EMERALDS strategy
    ─────────────────
    FV = 10 000 (never moves — stdev 0.72 over 20 k bars).
    Bot walls: bid_1 = 9992, ask_1 = 10008 (confirmed from trades CSV).

    Step 1 — SWEEP TAKE
      Buy every ask strictly below FV (9992 → free money vs 10000).
      Sell every bid strictly above FV (10008 → free money vs 10000).
      Iterate all levels, not just level 1.

    Step 2 — OVERBID / UNDERASK (queue priority)
      We want to be the BEST price on each side so we fill before the bots.
      • Overbid:  post at best_bid+1 (e.g. 9993) — beats bot's 9992
      • Underask: post at best_ask-1 (e.g. 10007) — beats bot's 10008
      Still capped at FV-1 / FV+1 to keep positive edge vs fair value.

    Step 3 — INVENTORY LEAN
      If long, tighten ask toward FV to exit faster.
      If short, tighten bid toward FV to exit faster.
    """

    FV = EMERALD_FV

    def get_orders(self):
        fv = self.FV

        # ── 1. SWEEP TAKE ──────────────────────────────────────────────────
        for ask_px, ask_vol in self.asks.items():
            if ask_px < fv:
                self._buy(ask_px, ask_vol)
            else:
                break   # sorted ascending — stop at first price ≥ FV

        for bid_px, bid_vol in self.bids.items():
            if bid_px > fv:
                self._sell(bid_px, bid_vol)
            else:
                break   # sorted descending — stop at first price ≤ FV

        # ── 2. OVERBID / UNDERASK ─────────────────────────────────────────
        # Default: just inside the walls
        bid_q = (self.bid_wall + 1) if self.bid_wall else (fv - 9)
        ask_q = (self.ask_wall - 1) if self.ask_wall else (fv + 9)

        # Overbid: beat the best resting bid below FV by 1 tick
        for bp, bv in self.bids.items():
            if bp < fv:
                bid_q = max(bid_q, bp + 1)   # beat them by 1 for queue priority
                break

        # Underask: beat the best resting ask above FV by 1 tick
        for ap, av in self.asks.items():
            if ap > fv:
                ask_q = min(ask_q, ap - 1)   # beat them by 1 for queue priority
                break

        # ── 3. INVENTORY LEAN ────────────────────────────────────────────
        lean = self.pos_limit * EMERALD_LEAN_FRAC
        if self.position > lean:
            ask_q = min(ask_q, fv)       # sell at FV to flatten long
        elif self.position < -lean:
            bid_q = max(bid_q, fv)       # buy at FV to flatten short

        # Hard clamps: never cross FV
        bid_q = min(int(bid_q), fv - 1)
        ask_q = max(int(ask_q), fv + 1)

        self._buy (bid_q, self.buy_cap)
        self._sell(ask_q, self.sell_cap)

        return self.orders


# ══════════════════════════ Tomato Trader ════════════════════════════════════

class TomatoTrader(ProductTrader):
    """
    TOMATOES strategy
    ─────────────────
    Prices wander (stdev ≈ 20), lag-1 autocorr = -0.40 (mean reversion).
    75% of market trades hit the bid → lean toward selling into rallies.

    Step 1 — SWEEP TAKE
      Buy any ask more than 1 tick below EMA fair value.
      Sell any bid more than 1 tick above EMA fair value.
      (There are rarely mispriced orders vs EMA in normal conditions,
       but this catches sharp dislocations immediately.)

    Step 2 — OVERBID / UNDERASK (queue priority)  ← KEY FIX vs v3
      Post bid at best_bid + 1 (ABOVE existing best bid → price priority).
      Post ask at best_ask - 1 (BELOW existing best ask → price priority).
      This ensures we're first in queue on both sides every tick.

    Step 3 — EMA LEAN (suppress wrong side)
      Use EMA-30 deviation to suppress one side when price is extended:
      • dev > LEAN_THR: start shifting ask lower (more aggressive sells)
      • dev > SUPPRESS: stop buying entirely (fully suppress bid)
      • Mirror for negative deviation.
      This prevents accumulating a large position against the MR direction.

    Step 4 — INVENTORY GUARD
      Emergency market order if |pos| > 80% of limit.
    """

    def get_orders(self):

        if self.mid is None or self.best_bid is None or self.best_ask is None:
            return self.orders

        # ── EMA fair value ────────────────────────────────────────────────
        fv  = self._ema("t_ema", TOMATO_EMA_WINDOW, self.mid)
        dev = self.mid - fv     # positive = price above EMA = lean short

        # ── 1. SWEEP TAKE ─────────────────────────────────────────────────
        for ask_px, ask_vol in self.asks.items():
            if ask_px < fv - 1:
                self._buy(ask_px, ask_vol)
            else:
                break

        for bid_px, bid_vol in self.bids.items():
            if bid_px > fv + 1:
                self._sell(bid_px, bid_vol)
            else:
                break

        # ── 2. OVERBID / UNDERASK: queue priority ─────────────────────────
        # Core fix: post ABOVE best_bid and BELOW best_ask so we're first.
        # Previous versions capped at best_bid (tied with bots, no priority).
        bid_q = self.best_bid + 1    # overbid by 1 tick
        ask_q = self.best_ask - 1    # underask by 1 tick

        # ── 3. EMA LEAN ───────────────────────────────────────────────────
        # Suppress the side that would build inventory in wrong direction.
        suppress_bid = dev >  TOMATO_SUPPRESS     # far above EMA → stop buying
        suppress_ask = dev < -TOMATO_SUPPRESS     # far below EMA → stop selling

        # Partial lean: when approaching suppress threshold, widen the
        # bad side's quote away from the market to reduce fill probability.
        if dev > TOMATO_LEAN_THR:
            # Price above EMA — lean ask more aggressively, widen bid
            ask_q = max(self.best_ask - 2, int(fv))   # tighter ask
            bid_q = self.best_bid                      # at (not above) best bid — less priority
        elif dev < -TOMATO_LEAN_THR:
            bid_q = min(self.best_bid + 2, int(fv))   # tighter bid
            ask_q = self.best_ask                      # at best ask — less priority

        # Inventory lean: if already positioned, lean further to exit
        pos_frac = self.position / self.pos_limit
        if pos_frac > 0.5:      # long — be more aggressive on sells
            ask_q = min(ask_q, self.best_ask)
        elif pos_frac < -0.5:   # short — be more aggressive on buys
            bid_q = max(bid_q, self.best_bid)

        # Safety: never cross the spread
        bid_q = min(int(bid_q), self.best_ask - 1)
        ask_q = max(int(ask_q), self.best_bid + 1)

        # ── Post quotes ────────────────────────────────────────────────────
        if not suppress_bid and self.buy_cap > 0:
            self._buy(bid_q, self.buy_cap)

        if not suppress_ask and self.sell_cap > 0:
            self._sell(ask_q, self.sell_cap)

        # ── 4. INVENTORY GUARD ────────────────────────────────────────────
        guard  = int(self.pos_limit * TOMATO_GUARD_FRAC)
        target = int(self.pos_limit * TOMATO_GUARD_TGT)

        if self.position > guard and self.best_bid:
            self._sell(self.best_bid, self.position - target)
        elif self.position < -guard and self.best_ask:
            self._buy(self.best_ask, -self.position - target)

        self.new_td["t_dbg"] = {"mid": round(self.mid,2), "fv": round(fv,2),
                                 "dev": round(dev,2), "pos": self.position,
                                 "bq": bid_q, "aq": ask_q}
        return self.orders


# ═══════════════════════════════ Main Trader ═════════════════════════════════

class Trader:

    def run(self, state: TradingState):

        last_td = {}
        try:
            if state.traderData:
                last_td = json.loads(state.traderData)
        except Exception:
            pass

        new_td = {}
        result = {}

        for symbol, TraderClass in [(EMERALD_SYMBOL, EmeraldTrader),
                                     (TOMATO_SYMBOL,  TomatoTrader)]:
            if symbol not in state.order_depths:
                continue
            try:
                t = TraderClass(symbol, state, new_td, last_td)
                orders = t.get_orders()
                if orders:
                    result[symbol] = orders
            except Exception as e:
                print(f"[ERR] {symbol}: {e}")

        try:
            td_out = json.dumps(new_td)
        except Exception:
            td_out = ""

        return result, 0, td_out