"""
IMC Prosperity 4 — v10 (Aggressive Asymmetric Lean)
=====================================================

CHANGELOG FROM v9
──────────────────
v9 scored 1,833 — worse than v6's 2,701. Post-mortem:

  WHAT HURT IN v9
  ───────────────
  ① Active flattening fired in the real backtester and bled money by
    aggressively selling at bid1 (paying the full ~13 tick spread).
    Our local simulation never triggered it because simulation
    over-counts fills — background trades in the CSV are other bots
    trading with each other, not with us.

  ② BUY_LEAN_FRAC=0.3 was too restrictive. Suppressing bids at just
    18 units long killed fill rate in the real backtester where fills
    are already scarce.

  ③ Soft cap of 60 left 20 units of capacity permanently unused.

v10 PHILOSOPHY: MAXIMUM AGGRESSION
────────────────────────────────────
This is a competition. Risk is acceptable. We want PnL.

  REMOVED: Active flattening — too costly, too unpredictable
  REMOVED: Soft position cap — use the full 80 unit limit
  KEPT:    Asymmetric lean — but loosened significantly
             BUY_LEAN_FRAC = 0.7  (was 0.3 in v9, 0.6 in v6)
             SEL_LEAN_FRAC = 0.9  (was 0.7 in v9)

  The asymmetry is buy-heavy (2.7:1 buy:sell fill ratio observed),
  so we let longs run longer before leaning (0.7 × 80 = 56 units)
  while keeping a very high sell threshold (0.9 × 80 = 72 units).
  This maximises passive fill capture on both sides without blocking.

  QUOTE AGGRESSION: unchanged from v6 — overbid/underask by 1 tick
  vs the best resting competition. This is already optimal.

Emeralds: completely unchanged — already at PnL ceiling.
"""

from datamodel import OrderDepth, TradingState, Order
import json

# ═══════════════════════════════ Constants ════════════════════════════════════

EMERALD_SYMBOL = "EMERALDS"
TOMATO_SYMBOL  = "TOMATOES"

POS_LIMITS = {
    EMERALD_SYMBOL: 50,
    TOMATO_SYMBOL:  80,
}

EMERALD_LEAN_FRAC = 0.6

# Tomato v10 — aggressive, full position limit, asymmetric lean only
TOMATO_BUY_LEAN_FRAC = 0.2   # lean ask toward wm when pos > 56 (was 0.6 in v6)
TOMATO_SEL_LEAN_FRAC = 0.9   # lean bid toward wm when pos < -72


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
        self.l1_mid = (
            (self.best_bid + self.best_ask) / 2
            if self.best_bid is not None and self.best_ask is not None
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
    Tomato v10: Aggressive asymmetric lean. Full position limit. No flattening.

    The only change vs v6 is the lean thresholds:
      v6:  symmetric 0.6 / 0.6
      v10: asymmetric 0.7 / 0.9

    BUY_LEAN_FRAC=0.7 — tolerate up to 56 units long before leaning the ask
    SEL_LEAN_FRAC=0.9 — tolerate up to 72 units short before leaning the bid

    The higher buy threshold vs v6 lets us absorb more of the buy-heavy
    flow before interfering with quoting. The very high sell threshold
    ensures we almost never suppress our ask — since sells are scarce,
    we want to capture every one.
    """

    BUY_LEAN_FRAC = TOMATO_BUY_LEAN_FRAC
    SEL_LEAN_FRAC = TOMATO_SEL_LEAN_FRAC

    def get_orders(self):
        if self.l1_mid is None:
            return self.orders

        wm = self.l1_mid

        # ── ① TAKING ─────────────────────────────────────────────────────────
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

        # ── ② MAKING: Overbid / Underask ─────────────────────────────────────
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

        # ── ③ ASYMMETRIC LEAN ─────────────────────────────────────────────────
        buy_lean_threshold = self.pos_limit * self.BUY_LEAN_FRAC
        sel_lean_threshold = self.pos_limit * self.SEL_LEAN_FRAC

        if self.position > buy_lean_threshold:
            # Long — push ask toward wm to encourage sells and flatten
            ask_quote = min(ask_quote, int(wm))
        elif self.position < -sel_lean_threshold:
            # Short — push bid toward wm to encourage buys and flatten
            bid_quote = max(bid_quote, int(wm))

        # ── ④ SAFETY ─────────────────────────────────────────────────────────
        bid_quote = min(int(bid_quote), int(wm) - 1)
        ask_quote = max(int(ask_quote), int(wm) + 1)

        # ── ⑤ POST full capacity ──────────────────────────────────────────────
        self._buy (bid_quote, self.buy_cap)
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