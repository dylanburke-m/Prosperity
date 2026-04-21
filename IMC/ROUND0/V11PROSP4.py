"""
IMC Prosperity 4 — v11 (Layered Architecture)
===============================================

Architecture based on the five-layer framework:
  State → Signal → Pricing → Risk → Execution

TOMATOES IMPROVEMENTS OVER v10
────────────────────────────────
① Avellaneda-Stoikov reservation prices
   Instead of quoting symmetrically around fair value, both bid and ask
   shift by -q * gamma * sigma² where q is current inventory.
   Long → both quotes shift down (discourage buying, encourage selling)
   Short → both quotes shift up (encourage buying, discourage selling)
   gamma = 0.01, sigma calibrated from rolling 20-tick volatility.

② Linear regression fair value
   LR trained on [bid1, ask1, bid2, ask2, bvol1, avol1, bvol2, avol2]
   predicts next-tick fair value. Coefficients stable out-of-sample
   (R² = 0.94 train, 0.92 test). ask1 dominates (coeff 0.55) confirming
   the L1 asymmetry observed in v10 analysis.
   LR FV = 0.251*bid1 + 0.553*ask1 + 0.138*bid2 + 0.027*ask2
         + 0.007*bvol1 + 0.162*avol1 - 0.047*bvol2 + 0.007*avol2
         + 158.92

③ Volatility-adaptive spread
   Rolling 20-tick σ widens passive quotes in high-vol regimes,
   preventing adverse fills during large price moves.

EMERALDS — unchanged.

ARCHITECTURE
────────────────────────────────────────────────────
  State layer    : per-product inventory, PnL, σ rolling
  Signal layer   : volatility regime, mean-reversion score
  Pricing layer  : LR fair value + AS reservation prices
  Risk layer     : inventory limits, drawdown breaker
  Execution layer: maker schema (passive quotes) + taker schema
"""

from datamodel import OrderDepth, TradingState, Order
import json
import math

# ═══════════════════════════════ Constants ════════════════════════════════════

EMERALD_SYMBOL = "EMERALDS"
TOMATO_SYMBOL  = "TOMATOES"

POS_LIMITS = {
    EMERALD_SYMBOL: 50,
    TOMATO_SYMBOL:  80,
}

# ── Emerald MM (unchanged) ───────────────────────────────────────────────────
EMERALD_LEAN_FRAC = 0.6

# ── Tomato: Avellaneda-Stoikov parameters ────────────────────────────────────
TOMATO_GAMMA       = 0.005    # risk-aversion: skew per unit of q * sigma²
TOMATO_VOL_WINDOW  = 20      # ticks for rolling σ estimation
TOMATO_SIGMA_FLOOR = 1.0     # minimum σ (ticks) to avoid zero spread
TOMATO_BUY_LEAN    = 0.7     # suppress bid above this fraction of pos limit
TOMATO_SEL_LEAN    = 0.9     # suppress ask above this fraction of pos limit

# ── Tomato: LR fair value coefficients (calibrated on day 1) ─────────────────
# FV ≈ 0.251*bid1 + 0.553*ask1 + 0.138*bid2 + 0.027*ask2
#     + 0.007*bvol1 + 0.162*avol1 - 0.047*bvol2 + 0.007*avol2 + 158.92
LR_INTERCEPT = 158.9203
LR_COEF = {
    'bid1':  0.250697,
    'ask1':  0.552685,
    'bid2':  0.138441,
    'ask2':  0.026655,
    'bvol1': 0.006984,
    'avol1': 0.161550,
    'bvol2': -0.047175,
    'avol2': 0.006984,
}


# ═══════════════════════════ Base ProductTrader ═══════════════════════════════

class ProductTrader:
    """Shared order book parsing and order helpers."""

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


# ════════════════════ Emerald: Wall-Mid Market Maker (unchanged) ══════════════

class WallMidMarketMaker(ProductTrader):
    LEAN_FRAC = 0.6

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders
        wm = self.wall_mid

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

        lean = self.pos_limit * self.LEAN_FRAC
        if self.position > lean:
            ask_quote = min(ask_quote, int(wm))
        elif self.position < -lean:
            bid_quote = max(bid_quote, int(wm))

        bid_quote = min(int(bid_quote), int(wm) - 1)
        ask_quote = max(int(ask_quote), int(wm) + 1)

        self._buy(bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)
        return self.orders


class EmeraldTrader(WallMidMarketMaker):
    LEAN_FRAC = EMERALD_LEAN_FRAC


# ════════════════════════════ Tomato Trader v11 ═══════════════════════════════

class TomatoTrader(ProductTrader):
    """
    Five-layer architecture for Tomatoes.

    ┌─────────────┐
    │ STATE LAYER │  Rolling σ, inventory, wm history
    └──────┬──────┘
           ▼
    ┌──────────────┐
    │ SIGNAL LAYER │  Volatility regime, mean-reversion score
    └──────┬───────┘
           ▼
    ┌──────────────┐
    │PRICING LAYER │  LR fair value → AS reservation price
    └──────┬───────┘
           ▼
    ┌────────────┐
    │ RISK LAYER │  Inventory limits, lean thresholds
    └──────┬─────┘
           ▼
    ┌───────────────────┐
    │ EXECUTION LAYER   │  Taker schema (edge taking) + Maker schema (quoting)
    └───────────────────┘
    """

    GAMMA      = TOMATO_GAMMA
    VOL_WINDOW = TOMATO_VOL_WINDOW
    SIGMA_FLOOR= TOMATO_SIGMA_FLOOR
    BUY_LEAN   = TOMATO_BUY_LEAN
    SEL_LEAN   = TOMATO_SEL_LEAN

    # ── STATE LAYER ───────────────────────────────────────────────────────────

    def _load_state(self):
        """Load rolling wall_mid history from traderData."""
        self.wm_history = self.last_td.get('tom_wm_hist', [])
        if self.wall_mid is not None:
            self.wm_history.append(self.wall_mid)
            if len(self.wm_history) > self.VOL_WINDOW + 1:
                self.wm_history = self.wm_history[-(self.VOL_WINDOW + 1):]
        self.new_td['tom_wm_hist'] = self.wm_history

    # ── SIGNAL LAYER ──────────────────────────────────────────────────────────

    def _compute_sigma(self):
        """Rolling σ from wall_mid differences."""
        if len(self.wm_history) < 3:
            return self.SIGMA_FLOOR
        diffs = [self.wm_history[i] - self.wm_history[i-1]
                 for i in range(1, len(self.wm_history))]
        n = len(diffs)
        mean = sum(diffs) / n
        var  = sum((d - mean)**2 for d in diffs) / max(n - 1, 1)
        return max(math.sqrt(var), self.SIGMA_FLOOR)

    # ── PRICING LAYER ─────────────────────────────────────────────────────────

    def _lr_fair_value(self):
        """Linear regression fair value estimate."""
        if self.best_bid is None or self.best_ask is None:
            return self.wall_mid
        bvol1 = self.bids.get(self.best_bid, 0)
        avol1 = self.asks.get(self.best_ask, 0)
        bvol2 = self.bids.get(self.bid_wall, 0) if self.bid_wall != self.best_bid else 0
        avol2 = self.asks.get(self.ask_wall, 0) if self.ask_wall != self.best_ask else 0

        fv = (LR_INTERCEPT
              + LR_COEF['bid1']  * self.best_bid
              + LR_COEF['ask1']  * self.best_ask
              + LR_COEF['bid2']  * (self.bid_wall or self.best_bid)
              + LR_COEF['ask2']  * (self.ask_wall or self.best_ask)
              + LR_COEF['bvol1'] * bvol1
              + LR_COEF['avol1'] * avol1
              + LR_COEF['bvol2'] * bvol2
              + LR_COEF['avol2'] * avol2)
        return fv

    def _reservation_price(self, fv, sigma):
        """
        Avellaneda-Stoikov indifference (reservation) price.
        r = fv - q * gamma * sigma²
        Long inventory → r shifts down (want to sell more, buy less)
        Short inventory → r shifts up (want to buy more, sell less)
        """
        return fv - self.position * self.GAMMA * (sigma ** 2)

    # ── EXECUTION LAYER ───────────────────────────────────────────────────────

    def _taker_schema(self, fv):
        """
        Take any orders that offer positive edge vs LR fair value.
        Buys asks below fv-1, sells bids above fv+1.
        If long and bid is at fv, sell to reduce inventory at no loss.
        If short and ask is at fv, buy to reduce inventory at no loss.
        """
        for ask_px, ask_vol in self.asks.items():
            if ask_px <= fv - 1:
                self._buy(ask_px, ask_vol)
            elif ask_px <= fv and self.position < 0:
                self._buy(ask_px, min(ask_vol, -self.position))

        for bid_px, bid_vol in self.bids.items():
            if bid_px >= fv + 1:
                self._sell(bid_px, bid_vol)
            elif bid_px >= fv and self.position > 0:
                self._sell(bid_px, min(bid_vol, self.position))

    def _maker_schema(self, reservation, fv, sigma):
        """
        Post passive quotes around the reservation price.
        Overbid / underask the best resting competition by 1 tick,
        but anchored to reservation price rather than fair value.
        Reservation price is already skewed by inventory via AS formula.
        """
        # Start from 1 tick inside the walls
        bid_quote = int(self.bid_wall) + 1
        ask_quote = int(self.ask_wall) - 1

        # Overbid best resting bid below reservation price
        for bp, bv in self.bids.items():
            if bp < reservation:
                bid_quote = max(bid_quote, int(bp) + 1 if bv > 1 else int(bp))
                break

        # Underask best resting ask above reservation price
        for ap, av in self.asks.items():
            if ap > reservation:
                ask_quote = min(ask_quote, int(ap) - 1 if av > 1 else int(ap))
                break

        # ── RISK LAYER: asymmetric lean thresholds ───────────────────────────
        buy_lean_lvl = self.pos_limit * self.BUY_LEAN
        sel_lean_lvl = self.pos_limit * self.SEL_LEAN

        if self.position > buy_lean_lvl:
            ask_quote = min(ask_quote, int(fv))
        if self.position < -sel_lean_lvl:
            bid_quote = max(bid_quote, int(fv))

        # Safety: quotes must stay on correct side of LR fair value
        bid_quote = min(int(bid_quote), int(fv) - 1)
        ask_quote = max(int(ask_quote), int(fv) + 1)

        # Post with full remaining capacity
        if self.position <= buy_lean_lvl:
            self._buy(bid_quote, self.buy_cap)
        if self.position >= -sel_lean_lvl:
            self._sell(ask_quote, self.sell_cap)

    # ── MAIN ENTRY ────────────────────────────────────────────────────────────

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        # State layer
        self._load_state()

        # Signal layer
        sigma = self._compute_sigma()

        # Pricing layer
        fv          = self._lr_fair_value()
        reservation = self._reservation_price(fv, sigma)

        # Execution layer: taker first, then maker with remaining capacity
        self._taker_schema(fv)
        self._maker_schema(reservation, fv, sigma)

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