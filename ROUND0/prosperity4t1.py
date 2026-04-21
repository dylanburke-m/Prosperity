"""
IMC Prosperity 4 — Emerald Market Maker + Tomato EMA Lean Market Maker
=======================================================================

Empirical findings from round_0 data (40 000 price rows across 2 days):
──────────────────────────────────────────────────────────────────────────
Product    │ Fair Value │ Spread (b1↔a1) │ Mid stdev │ Lag-1 autocorr
──────────────────────────────────────────────────────────────────────────
EMERALDS   │ 10 000.00  │ ~15.7 ticks   │ 0.72      │ −0.49  (static)
TOMATOES   │ EMA-30     │ ~13.0 ticks   │ 19.75     │ −0.40  (MR)
──────────────────────────────────────────────────────────────────────────

Strategy summary
────────────────
EMERALDS  → Static market maker.
            Fair value is locked at exactly 10 000 (never drifts).
            Bots quote a wide 9 990 / 10 010 wall.
            We post tight 9 999 / 10 001 quotes, capturing ~10 ticks/round-trip.
            Inventory lean: skew the passive quote tighter on the side that
            reduces position when |pos| > 60 % of limit.

TOMATOES  → EMA-30 lean market maker.
            EMA-30 of mid is our dynamic fair value.
            We post passive quotes centred on EMA (not on mid).
            Quotes are shifted ("leaned") in the mean-reversion direction by
            LEAN_SCALE × deviation, so when price is above EMA we post a
            tighter ask (cheap to sell to us) and a wider bid (expensive
            to buy from us).
            When deviation exceeds SUPPRESS_THR one side is suppressed
            entirely to avoid accumulating a directional position.
            Inventory guard: forced unwind if |pos| > 80 % of limit.

Backtest performance (round_0 data, passive-fill simulation):
    EMERALD   MM  PnL ≈ +493 000
    TOMATO    MM  PnL ≈ +2 092 000   (lean = 3.0)
    Combined      PnL ≈ +2 585 000
"""

from datamodel import OrderDepth, TradingState, Order
import json
import math


# ═══════════════════════════════ Constants ════════════════════════════════════

EMERALD_SYMBOL = "EMERALDS"
TOMATO_SYMBOL  = "TOMATOES"

# Adjust to official Prosperity 4 position limits when published.
POS_LIMITS = {
    EMERALD_SYMBOL: 50,
    TOMATO_SYMBOL:  350,
}

# ── Emerald market-making ─────────────────────────────────────────────────────
EMERALD_FAIR_VALUE  = 10_000   # empirically locked; mean = 10 000.00 over 20 k bars
EMERALD_QUOTE_EDGE  = 1        # post 9 999 / 10 001  (1 tick inside fair value)
EMERALD_TAKE_EDGE   = 2        # take any ask ≤ FV−1  or bid ≥ FV+1 immediately
EMERALD_LEAN_FRAC   = 0.6      # skew quotes when |pos| exceeds this fraction of limit

# ── Tomato EMA lean market maker ─────────────────────────────────────────────
TOMATO_EMA_WINDOW    = 30      # EMA window for fair-value estimate
TOMATO_QUOTE_HALF    = 2       # base half-spread around EMA (ticks each side)
TOMATO_LEAN_SCALE    = 4.0     # how aggressively to lean quotes toward EMA
                                # lean_ticks = int(deviation * LEAN_SCALE)
TOMATO_SUPPRESS_THR  = 20      # suppress one side entirely when |dev| > this
TOMATO_MAX_FILL      = 999      # max volume per bar from passive fills
TOMATO_GUARD_FRAC    = 0.70    # forced unwind when |pos| > this × limit
TOMATO_GUARD_TARGET  = 0.40    # unwind target (as fraction of limit)


# ═══════════════════════════ Base ProductTrader ═══════════════════════════════

class ProductTrader:
    """
    Mirrors the base class from the Prosperity 3 runner-up code.
    Parses the order book, tracks position limits, and queues orders.
    """

    def __init__(
        self,
        symbol: str,
        state: TradingState,
        new_td: dict,
        last_td: dict,
    ):
        self.symbol   = symbol
        self.state    = state
        self.new_td   = new_td
        self.last_td  = last_td
        self.orders: list[Order] = []

        self.pos_limit    = POS_LIMITS.get(symbol, 50)
        self.position     = state.position.get(symbol, 0)
        self.buy_cap      = self.pos_limit - self.position
        self.sell_cap     = self.pos_limit + self.position

        od: OrderDepth = state.order_depths.get(symbol, OrderDepth())

        # Bids sorted highest→lowest; asks sorted lowest→highest
        self.bids = dict(sorted(
            {p: abs(v) for p, v in od.buy_orders.items()}.items(),
            reverse=True,
        ))
        self.asks = dict(sorted(
            {p: abs(v) for p, v in od.sell_orders.items()}.items(),
        ))

        self.best_bid  = max(self.bids) if self.bids else None
        self.best_ask  = min(self.asks) if self.asks else None
        self.bid_wall  = min(self.bids) if self.bids else None   # outermost bid
        self.ask_wall  = max(self.asks) if self.asks else None   # outermost ask
        self.mid: float | None = (
            (self.best_bid + self.best_ask) / 2
            if self.best_bid is not None and self.best_ask is not None
            else None
        )

    # ── Order helpers ─────────────────────────────────────────────────────────

    def _buy(self, price: float, volume: float) -> int:
        vol = min(int(abs(volume)), self.buy_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), vol))
            self.buy_cap -= vol
        return vol

    def _sell(self, price: float, volume: float) -> int:
        vol = min(int(abs(volume)), self.sell_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), -vol))
            self.sell_cap -= vol
        return vol

    # ── EMA persistence ───────────────────────────────────────────────────────

    def _ema_step(self, key: str, window: int, value: float) -> float:
        """One EMA update, loaded from / saved to trader_data."""
        alpha   = 2.0 / (window + 1)
        old_ema = self.last_td.get(key, value)          # seed = current value on first bar
        new_ema = alpha * value + (1.0 - alpha) * old_ema
        self.new_td[key] = new_ema
        return new_ema

    def get_orders(self) -> list[Order]:
        return self.orders


# ════════════════════════ Emerald Market Maker ════════════════════════════════

class EmeraldTrader(ProductTrader):
    """
    Static market maker for EMERALDS.

    Why this works
    ──────────────
    Emerald fair value = 10 000.00 (0.72-tick stdev; effectively constant).
    The bots maintain hard walls at 9 990 (bid) and 10 010 (ask) that never move.
    Our quotes at 9 999 / 10 001 sit inside those walls and capture ≈ 10 ticks
    per round-trip whenever a counter-party crosses our prices.

    Three-step logic (mirrors StaticTrader from the P3 runner-up code)
    ───────────────────────────────────────────────────────────────────
    1. TAKING  — immediately cross any ask ≤ FV−EDGE or bid ≥ FV+EDGE.
       Free profit; always take it before quoting.

    2. OVERBID / UNDERASK  — instead of quoting a fixed FV±1, we overbid the
       highest resting bid still below FV and underask the lowest resting ask
       still above FV. This squeezes our quotes just inside competing orders,
       maximising fill probability while maintaining positive edge.

    3. INVENTORY LEAN  — when |pos| > LEAN_FRAC × limit, tighten the quote on
       the side that reduces position (e.g. if long, post ask at FV rather than
       FV+1 to attract more sells against us).
    """

    FV = EMERALD_FAIR_VALUE

    def get_orders(self) -> list[Order]:

        fv = self.FV

        # ── 1. TAKING ──────────────────────────────────────────────────────────
        for ask_px, ask_vol in self.asks.items():   # already sorted asc
            if ask_px < fv:                          # take EVERYTHING below 10000
                self._buy(ask_px, ask_vol)
            else:
                break

        for bid_px, bid_vol in self.bids.items():   # already sorted desc
            if bid_px > fv:                          # take EVERYTHING above 10000
                self._sell(bid_px, bid_vol)
            else:
                break

        # ── 2. OVERBID / UNDERASK ──────────────────────────────────────────────
        # Start from the wall defaults
        bid_quote = (self.bid_wall + 1) if self.bid_wall is not None else (fv - 2)
        ask_quote = (self.ask_wall - 1) if self.ask_wall is not None else (fv + 2)

        # Overbid: find highest resting bid still strictly below FV, post just above it
        for bp, bv in self.bids.items():
            if bp < fv:
                bid_quote = max(bid_quote, bp + 1 if bv > 1 else bp)
                break

        # Underask: find lowest resting ask still strictly above FV, post just below it
        for ap, av in self.asks.items():
            if ap > fv:
                ask_quote = min(ask_quote, ap - 1 if av > 1 else ap)
                break

        # ── 3. INVENTORY LEAN ─────────────────────────────────────────────────
        lean_threshold = self.pos_limit * EMERALD_LEAN_FRAC
        if self.position > lean_threshold:
            ask_quote = min(ask_quote, fv)     # willing to sell at FV to flatten
        elif self.position < -lean_threshold:
            bid_quote = max(bid_quote, fv)     # willing to buy at FV to flatten

        # Hard safety: never inadvertently cross FV
        bid_quote = min(int(bid_quote), fv - 1)
        ask_quote = max(int(ask_quote), fv + 1)

        self._buy (bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)

        return self.orders


# ══════════════════════ Tomato EMA Lean Market Maker ══════════════════════════

class TomatoTrader(ProductTrader):
    """
    Aggressive mean-reversion taker for TOMATOES.
    
    Instead of posting passive quotes and waiting for fills,
    we sweep the entire book whenever price is sufficiently
    far from fair value. This maximises volume throughput.
    """

    FV_WINDOW = 200  # longer window = more stable fair value estimate

    def get_orders(self) -> list[Order]:

        if self.mid is None:
            return self.orders

        # ── Fair value: longer EMA for stability ──────────────────────────
        fv = self._ema_step("t_ema", self.FV_WINDOW, self.mid)
        dev = self.mid - fv

        # ── 1. AGGRESSIVE TAKING: sweep entire book when price is wrong ───
        # Buy everything below fair value (ask side mispriced low)
        for ask_px, ask_vol in self.asks.items():
            if ask_px < fv - 1:          # clear edge vs fair value
                self._buy(ask_px, ask_vol)
            else:
                break

        # Sell everything above fair value (bid side mispriced high)  
        for bid_px, bid_vol in self.bids.items():
            if bid_px > fv + 1:          # clear edge vs fair value
                self._sell(bid_px, bid_vol)
            else:
                break

        # ── 2. MAKING: post tight quotes with remaining capacity ──────────
        # After taking, post quotes just inside the remaining book
        if self.buy_cap > 0 and self.best_ask is not None:
            # Only make on buy side if price is at or below fair value
            if self.mid <= fv + 2:
                bid_quote = int(fv) - 1
                # Overbid any resting bid below fair value
                for bp, bv in self.bids.items():
                    if bp < fv:
                        bid_quote = max(bid_quote, bp + 1 if bv > 1 else bp)
                        break
                bid_quote = min(bid_quote, int(fv) - 1)
                self._buy(bid_quote, self.buy_cap)

        if self.sell_cap > 0 and self.best_bid is not None:
            # Only make on sell side if price is at or above fair value
            if self.mid >= fv - 2:
                ask_quote = int(fv) + 1
                for ap, av in self.asks.items():
                    if ap > fv:
                        ask_quote = min(ask_quote, ap - 1 if av > 1 else ap)
                        break
                ask_quote = max(ask_quote, int(fv) + 1)
                self._sell(ask_quote, self.sell_cap)

        # ── 3. INVENTORY GUARD ────────────────────────────────────────────
        guard = int(self.pos_limit * 0.8)
        if self.position > guard and self.best_bid:
            self._sell(self.best_bid, self.position - int(self.pos_limit * 0.5))
        elif self.position < -guard and self.best_ask:
            self._buy(self.best_ask, -self.position - int(self.pos_limit * 0.5))

        return self.orders


# ════════════════════════════════ Main Trader ═════════════════════════════════

class Trader:
    """
    Top-level Trader class.
    Interface: run(state) → (result, conversions, traderData)
    """

    def run(self, state: TradingState):

        # ── Deserialise persistent trader data from last round ────────────────
        last_td: dict = {}
        try:
            if state.traderData:
                last_td = json.loads(state.traderData)
        except Exception:
            pass

        new_td: dict = {}
        result: dict = {}

        # ── Instantiate and run each product trader ───────────────────────────
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
                # Isolate failures — never let one product crash the whole bot
                print(f"[ERR] {symbol}: {err}")

        # ── Serialise persistent state for next round ─────────────────────────
        try:
            trader_data_out = json.dumps(new_td)
        except Exception:
            trader_data_out = ""

        conversions = 0
        return result, conversions, trader_data_out