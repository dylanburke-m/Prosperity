"""
IMC Prosperity 4 — v9 (Tomato Inventory Fix)
=============================================

CHANGELOG FROM v6 (last working MM build, 2701 PnL)
─────────────────────────────────────────────────────
Root cause of underperformance diagnosed from trade log analysis:

  FILL ASYMMETRY
  ─────────────
  Background trades on Tomatoes are heavily buy-skewed:
    • 2,031 units of buy fills (sellers hitting our bid)
    •   734 units of sell fills (buyers lifting our ask)
  This 2.7:1 ratio causes us to accumulate a long inventory that quickly
  hits the position limit of 80, blocking a further 1,224 units of
  profitable buy fills.

  Simulated PnL with no position limit = 7,890
  Actual PnL with position limit       = 2,701
  Gap = 5,189 — entirely from blocked fills

THREE FIXES IMPLEMENTED
────────────────────────
① ACTIVE FLATTENING
  When position exceeds FLATTEN_THRESH × POS_CAP, stop waiting for passive
  fills to rebalance — aggressively take the best available bid/ask to
  unload inventory immediately. Accepts the full bid1/ask1 spread cost but
  frees capacity for future fills worth more than the cost.

② ASYMMETRIC LEAN THRESHOLDS
  Since fills are buy-heavy, suppress the buy side much earlier:
    BUY_LEAN_FRAC  = 0.3  (stop bidding at 30% of cap = 18 units long)
    SELL_LEAN_FRAC = 0.7  (keep asking until 70% of cap = 42 units short)
  This prevents position runup before the flatten trigger fires.

③ REDUCED POSITION CAP
  Hard cap at 60 units (vs full limit of 80).
  Counterintuitively increases PnL — we never get fully blocked and stay
  nimble on both sides. Backtested PnL:
    v6 (cap=80, symmetric lean=0.6):              ~2,701
    v9 (cap=60, asymmetric lean, active flatten):  ~8,196

Emeralds: completely unchanged — already at its PnL ceiling.
"""

from datamodel import OrderDepth, TradingState, Order
import json

# ═══════════════════════════════ Constants ════════════════════════════════════

EMERALD_SYMBOL = "EMERALDS"
TOMATO_SYMBOL  = "TOMATOES"

POS_LIMITS = {
    EMERALD_SYMBOL: 50,
    TOMATO_SYMBOL:  80,   # exchange hard limit — our soft cap is lower
}

EMERALD_LEAN_FRAC = 0.6

# Tomato v9 parameters (optimised via parameter sweep)
TOMATO_POS_CAP       = 60    # soft position cap — stay well inside the hard limit
TOMATO_BUY_LEAN_FRAC = 0.3   # suppress bids when position > 30% of cap (= 18)
TOMATO_SEL_LEAN_FRAC = 0.7   # suppress asks when position < -70% of cap (= -42)
TOMATO_FLATTEN_THRESH= 0.7   # actively flatten when |position| > 70% of cap (= 42)


# ═══════════════════════════ Base ProductTrader ═══════════════════════════════

class ProductTrader:
    """Shared infrastructure: order book parsing, position limits, order helpers."""

    def __init__(self, symbol, state, new_td, last_td):
        self.symbol    = symbol
        self.state     = state
        self.new_td    = new_td
        self.last_td   = last_td
        self.orders    = []

        self.pos_limit = POS_LIMITS.get(symbol, 50)
        self.position  = state.position.get(symbol, 0)
        self.buy_cap   = self.pos_limit - self.position
        self.sell_cap  = self.pos_limit + self.position

        od = state.order_depths.get(symbol, OrderDepth())

        self.bids = dict(sorted(
            {p: abs(v) for p, v in od.buy_orders.items()}.items(),
            reverse=True,
        ))
        self.asks = dict(sorted(
            {p: abs(v) for p, v in od.sell_orders.items()}.items(),
        ))

        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None
        self.bid_wall = min(self.bids) if self.bids else None
        self.ask_wall = max(self.asks) if self.asks else None
        self.wall_mid = (
            (self.bid_wall + self.ask_wall) / 2
            if self.bid_wall is not None and self.ask_wall is not None
            else None
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

    def get_orders(self):
        return self.orders


# ════════════════════ Wall-Mid Market Maker (Emeralds) ════════════════════════

class WallMidMarketMaker(ProductTrader):
    """Unchanged from v6. Emeralds only."""

    LEAN_FRAC = 0.6

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        wm = self.wall_mid

        # ① TAKING
        for ask_px, ask_vol in self.asks.items():
            if ask_px <= wm - 1:
                self._buy(ask_px, ask_vol)
            elif ask_px <= wm and self.position < 0:
                self._buy(ask_px, min(ask_vol, -self.position))

        for bid_px, bid_vol in self.bids.items():
            if bid_px >= wm + 1:
                self._sell(bid_px, bid_vol)
            elif bid_px >= wm and self.position > 0:
                self._sell(bid_px, min(bid_vol, self.position))

        # ② MAKING: Overbid / Underask
        bid_quote = int(self.bid_wall) + 1
        ask_quote = int(self.ask_wall) - 1

        for bp, bv in self.bids.items():
            if bp < wm:
                bid_quote = max(bid_quote, int(bp) + 1 if bv > 1 else int(bp))
                break

        for ap, av in self.asks.items():
            if ap > wm:
                ask_quote = min(ask_quote, int(ap) - 1 if av > 1 else int(ap))
                break

        # ③ INVENTORY LEAN
        lean_threshold = self.pos_limit * self.LEAN_FRAC
        if self.position > lean_threshold:
            ask_quote = min(ask_quote, int(wm))
        elif self.position < -lean_threshold:
            bid_quote = max(bid_quote, int(wm))

        # ④ SAFETY
        bid_quote = min(int(bid_quote), int(wm) - 1)
        ask_quote = max(int(ask_quote), int(wm) + 1)

        self._buy (bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)

        return self.orders


# ══════════════════════════ Product-specific traders ═════════════════════════

class EmeraldTrader(WallMidMarketMaker):
    """Emerald: unchanged from v6. Already at PnL ceiling."""
    LEAN_FRAC = EMERALD_LEAN_FRAC


class TomatoTrader(WallMidMarketMaker):
    """
    Tomato v9: Three inventory fixes on top of v6 wall-mid MM.

    ① ACTIVE FLATTENING
       Before placing any passive quotes, check if position has drifted
       beyond FLATTEN_THRESH. If so, aggressively take from the book to
       reduce inventory back below the threshold.

    ② ASYMMETRIC LEAN THRESHOLDS
       BUY_LEAN_FRAC  < SELL_LEAN_FRAC to counter the buy-heavy fill flow.
       Bids are suppressed much earlier than asks.

    ③ SOFT POSITION CAP
       All capacity calculations use POS_CAP (60) instead of the exchange
       limit (80), keeping us nimble and preventing fill blockage.
    """

    POS_CAP        = TOMATO_POS_CAP
    BUY_LEAN_FRAC  = TOMATO_BUY_LEAN_FRAC
    SEL_LEAN_FRAC  = TOMATO_SEL_LEAN_FRAC
    FLATTEN_THRESH = TOMATO_FLATTEN_THRESH

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        wm          = self.wall_mid
        flatten_lvl = int(self.FLATTEN_THRESH * self.POS_CAP)

        # ── ① ACTIVE FLATTENING ───────────────────────────────────────────────
        # Fires BEFORE passive quoting — unload aggressively at best available
        if self.position > flatten_lvl:
            # Too long → hit bids to reduce
            unload = self.position - int(self.FLATTEN_THRESH * self.POS_CAP * 0.7)
            for bid_px, bid_vol in self.bids.items():
                if unload <= 0:
                    break
                taken = min(bid_vol, unload)
                self._sell(bid_px, taken)
                unload -= taken
            # After flattening, skip passive quoting this tick
            return self.orders

        elif self.position < -flatten_lvl:
            # Too short → lift asks to reduce
            unload = -self.position - int(self.FLATTEN_THRESH * self.POS_CAP * 0.7)
            for ask_px, ask_vol in self.asks.items():
                if unload <= 0:
                    break
                taken = min(ask_vol, unload)
                self._buy(ask_px, taken)
                unload -= taken
            return self.orders

        # ── Override buy_cap / sell_cap with soft cap ─────────────────────────
        self.buy_cap  = min(self.buy_cap,  self.POS_CAP - self.position)
        self.sell_cap = min(self.sell_cap, self.POS_CAP + self.position)

        # ── ② TAKING (unchanged from v6) ─────────────────────────────────────
        for ask_px, ask_vol in self.asks.items():
            if ask_px <= wm - 1:
                self._buy(ask_px, ask_vol)
            elif ask_px <= wm and self.position < 0:
                self._buy(ask_px, min(ask_vol, -self.position))

        for bid_px, bid_vol in self.bids.items():
            if bid_px >= wm + 1:
                self._sell(bid_px, bid_vol)
            elif bid_px >= wm and self.position > 0:
                self._sell(bid_px, min(bid_vol, self.position))

        # ── MAKING: Overbid / Underask (unchanged from v6) ───────────────────
        bid_quote = int(self.bid_wall) + 1
        ask_quote = int(self.ask_wall) - 1

        for bp, bv in self.bids.items():
            if bp < wm:
                bid_quote = max(bid_quote, int(bp) + 1 if bv > 1 else int(bp))
                break

        for ap, av in self.asks.items():
            if ap > wm:
                ask_quote = min(ask_quote, int(ap) - 1 if av > 1 else int(ap))
                break

        # ── ② ASYMMETRIC LEAN ─────────────────────────────────────────────────
        # Buy side suppressed early (0.3) — sell side suppressed late (0.7)
        buy_lean_threshold = self.POS_CAP * self.BUY_LEAN_FRAC
        sel_lean_threshold = self.POS_CAP * self.SEL_LEAN_FRAC

        if self.position > buy_lean_threshold:
            # Long and past buy lean — push ask to wm to flatten faster
            ask_quote = min(ask_quote, int(wm))
        if self.position < -sel_lean_threshold:
            # Short and past sell lean — push bid to wm to flatten faster
            bid_quote = max(bid_quote, int(wm))

        # ── SAFETY ────────────────────────────────────────────────────────────
        bid_quote = min(int(bid_quote), int(wm) - 1)
        ask_quote = max(int(ask_quote), int(wm) + 1)

        # ── ③ POST with soft-capped capacity ──────────────────────────────────
        # Suppress the bid entirely when long beyond buy lean threshold
        if self.position <= buy_lean_threshold:
            self._buy(bid_quote, self.buy_cap)

        # Suppress the ask entirely when short beyond sell lean threshold
        if self.position >= -sel_lean_threshold:
            self._sell(ask_quote, self.sell_cap)

        return self.orders


# ════════════════════════════════ Main Trader ═════════════════════════════════

class Trader:
    """Top-level Trader. Interface: run(state) → (result, conversions, traderData)"""

    def run(self, state: TradingState):

        last_td: dict = {}
        try:
            if state.traderData:
                last_td = json.loads(state.traderData)
        except Exception:
            pass

        new_td: dict = {}
        result: dict = {}

        TRADERS = {
            EMERALD_SYMBOL: EmeraldTrader,
            TOMATO_SYMBOL:  TomatoTrader,
        }

        for symbol, TraderClass in TRADERS.items():
            if symbol not in state.order_depths:
                continue
            try:
                trader = TraderClass(symbol, state, new_td, last_td)
                orders = trader.get_orders()
                if orders:
                    result[symbol] = orders
            except Exception as err:
                print(f"[ERR] {symbol}: {err}")

        try:
            trader_data_out = json.dumps(new_td)
        except Exception:
            trader_data_out = ""

        return result, 0, trader_data_out