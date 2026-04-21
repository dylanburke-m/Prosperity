"""
IMC Prosperity 4 — v7 (L2 Wall Volume Imbalance Skew)
======================================================

CHANGELOG FROM v6
──────────────────
v6 scored 2,756 PnL. The entire gap to #1 (5,235) is in TOMATOES.
Emeralds is already at ceiling — unchanged.

NEW SIGNAL: L2 wall volume imbalance on Tomatoes
  l2_imb = bid_volume_2 - ask_volume_2

  | Condition    | Freq  | Avg fwd 10-tick return | Action              |
  |l2_imb == 0   | 92.8% | ~0.00                  | No skew. Pure MM.   |
  |l2_imb > 0    |  3.5% | +0.55 ticks            | Skew quotes UP      |
  |l2_imb < 0    |  3.5% | -0.70 ticks            | Skew quotes DOWN    |

v7 APPROACH: When the L2 signal is active, shift the safety-check
reference price by SKEW_TICKS.  This allows quotes to move in the
signal direction through the safety gate, enabling both the aggressive
side (tighter quote toward predicted direction) and the defensive side
(wider quote away from predicted direction) to take effect.

Without this, the safety check (bid <= wm-1, ask >= wm+1) clamps the
aggressive side back, resulting in zero net effect from the skew.

LEAN_FRAC = 0.8 for both products (the value that yielded 2,756).
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

EMERALD_LEAN_FRAC = 0.8
TOMATO_LEAN_FRAC  = 0.95

TOMATO_SKEW_TICKS = 2     # shift safety-check mid by 2 ticks on signal


# ═══════════════════════════ Base ProductTrader ═══════════════════════════════

class ProductTrader:
    """
    Shared infrastructure: order book parsing, position limits, order helpers.
    """

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

        # Bids highest→lowest, asks lowest→highest (all volumes positive)
        self.bids = dict(sorted(
            {p: abs(v) for p, v in od.buy_orders.items()}.items(),
            reverse=True,
        ))
        self.asks = dict(sorted(
            {p: abs(v) for p, v in od.sell_orders.items()}.items(),
        ))

        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None

        # Outermost quotes = the deep-liquidity market-maker walls
        self.bid_wall  = min(self.bids) if self.bids else None
        self.ask_wall  = max(self.asks) if self.asks else None

        # Wall mid = best real-time estimate of true price
        self.wall_mid  = (
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


# ════════════════════ Shared Wall-Mid Market Maker ════════════════════════════

class WallMidMarketMaker(ProductTrader):
    """
    Core MM strategy (unchanged from v6):
      ① TAKING — sweep book for positive-edge fills vs wall_mid
      ② MAKING — overbid/underask the best resting competition
      ③ INVENTORY LEAN — tighten flattening side when position extreme
      ④ SAFETY — clamp quotes to maintain positive edge vs wall_mid
    """

    LEAN_FRAC = 0.8

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        wm = self.wall_mid

        # ── ① TAKING ─────────────────────────────────────────────────────────
        for ask_px, ask_vol in self.asks.items():
            if ask_px <= wm - 1:
                self._buy(ask_px, ask_vol)
            elif ask_px <= wm and self.position < 0:
                vol = min(ask_vol, -self.position)
                self._buy(ask_px, vol)

        for bid_px, bid_vol in self.bids.items():
            if bid_px >= wm + 1:
                self._sell(bid_px, bid_vol)
            elif bid_px >= wm and self.position > 0:
                vol = min(bid_vol, self.position)
                self._sell(bid_px, vol)

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

        # ── ③ INVENTORY LEAN ─────────────────────────────────────────────────
        lean_threshold = self.pos_limit * self.LEAN_FRAC
        if self.position > lean_threshold:
            ask_quote = min(ask_quote, int(wm))
        elif self.position < -lean_threshold:
            bid_quote = max(bid_quote, int(wm))

        # ── ④ SAFETY ─────────────────────────────────────────────────────────
        bid_quote = min(int(bid_quote), int(wm) - 1)
        ask_quote = max(int(ask_quote), int(wm) + 1)

        self._buy (bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)

        return self.orders


# ══════════════════════════ Product-specific traders ═════════════════════════

class EmeraldTrader(WallMidMarketMaker):
    """Emerald: static fair value at 10,000. Already at PnL ceiling. No changes."""
    LEAN_FRAC = EMERALD_LEAN_FRAC


class TomatoTrader(WallMidMarketMaker):
    """
    Tomato v7: L2 Wall Volume Imbalance Skew
    ─────────────────────────────────────────
    When L2 market makers quote unequal volume on bid vs ask walls,
    shift the safety-check reference price so quotes can move in the
    predicted direction.

    Pipeline: TAKING (vs real wm) → MAKING → L2 SIGNAL → LEAN → SAFETY (vs shifted mid)
    """
    LEAN_FRAC  = TOMATO_LEAN_FRAC
    SKEW_TICKS = TOMATO_SKEW_TICKS

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        wm = self.wall_mid

        # ── ① TAKING — use real wm (positive edge on taker fills) ────────────
        for ask_px, ask_vol in self.asks.items():
            if ask_px <= wm - 1:
                self._buy(ask_px, ask_vol)
            elif ask_px <= wm and self.position < 0:
                vol = min(ask_vol, -self.position)
                self._buy(ask_px, vol)

        for bid_px, bid_vol in self.bids.items():
            if bid_px >= wm + 1:
                self._sell(bid_px, bid_vol)
            elif bid_px >= wm and self.position > 0:
                vol = min(bid_vol, self.position)
                self._sell(bid_px, vol)

        # ── ② MAKING: Overbid / Underask (unchanged) ─────────────────────────
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

        # ── ③ L2 IMBALANCE SIGNAL ────────────────────────────────────────────
        bid_vol_2 = self.bids.get(self.bid_wall, 0)
        ask_vol_2 = self.asks.get(self.ask_wall, 0)
        l2_imb = bid_vol_2 - ask_vol_2

        # Shift the safety-check reference price in the signal direction
        if l2_imb > 0:
            # Expect price UP → allow quotes to shift upward
            safety_mid = wm + self.SKEW_TICKS
        elif l2_imb < 0:
            # Expect price DOWN → allow quotes to shift downward
            safety_mid = wm - self.SKEW_TICKS
        else:
            # No signal → standard MM
            safety_mid = wm

        # ── ④ INVENTORY LEAN (use real wm for lean reference) ─────────────────
        lean_threshold = self.pos_limit * self.LEAN_FRAC
        if self.position > lean_threshold:
            ask_quote = min(ask_quote, int(wm))
        elif self.position < -lean_threshold:
            bid_quote = max(bid_quote, int(wm))

        # ── ⑤ SAFETY CHECK — uses signal-adjusted mid ────────────────────────
        # The key v7 change: safety gate shifts with the predicted direction.
        # l2_imb > 0 (UP):   bid allowed up to wm+1, ask floored at wm+3
        # l2_imb < 0 (DOWN): bid capped at wm-3,     ask allowed down to wm-1
        # l2_imb == 0:        standard wm-1 / wm+1 (no change)
        bid_quote = min(int(bid_quote), int(safety_mid) - 1)
        ask_quote = max(int(ask_quote), int(safety_mid) + 1)

        self._buy (bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)

        return self.orders


# ════════════════════════════════ Main Trader ═════════════════════════════════

class Trader:
    """
    Top-level Trader. Interface: run(state) → (result, conversions, traderData)
    """

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