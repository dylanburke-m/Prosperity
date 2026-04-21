"""
IMC Prosperity 4 — v13  (Round 1: Pepper Root + Ash Osmium)
=============================================================

ROUND 1 PRODUCTS
────────────────
  INTARIAN_PEPPER_ROOT  — slowly drifting market (~+1000/day), wide spread
  ASH_COATED_OSMIUM     — mean-reverting around 10 000, wide spread

CALIBRATION DATA  (prices_round_1_day_{-2,-1,0}.csv)
────────────────────────────────────────────────────────────────────────

  ┌────────────────────────────┬──────────────────┬──────────────────┐
  │ Metric                     │ PEPPER ROOT      │ ASH OSMIUM       │
  ├────────────────────────────┼──────────────────┼──────────────────┤
  │ Inner spread (mean/median) │ 13.0 / 13        │ 16.2 / 16        │
  │ Outer (wall) spread        │ 17.3             │ 20.1             │
  │ best_bid distance to wall  │ ~2 ticks         │ ~2 ticks         │
  │ best_ask distance to wall  │ ~2 ticks         │ ~2 ticks         │
  │ Tick volatility σ (std Δm) │ 1.75 ticks       │ 1.93 ticks       │
  │ Wall-mid MAE vs mid_price  │ 0.87 ticks       │ 1.02 ticks       │
  │ OLS imbalance alpha        │ −0.71 (CONTRA)   │ −0.54 (CONTRA)   │
  │ Non-zero-imbalance ticks   │ 52 %             │ 53 %             │
  │ Price structure            │ +1000/day drift  │ MR to 10 000     │
  │ MR beta (at dev=10)        │ n/a              │ −2.43 ticks      │
  │ Actual trade vol/day       │ ~1 743           │ ~2 198           │
  └────────────────────────────┴──────────────────┴──────────────────┘

KEY FINDINGS vs v12 TOMATO
──────────────────────────

▸ IMBALANCE SIGNAL IS REVERSED (CRITICAL)
  v12 TOMATO had a POSITIVE alpha_imb (+3.5): heavy bid side → price UP.
  Round 1 products have a NEGATIVE alpha_imb (−0.5 to −0.7): heavy bid
  side → price DOWN.  This is a mean-reversion / quoting-side book.
  The heavy side represents PASSIVE market-maker quotes, NOT aggressive
  order flow.  Using it as a directional signal would LOSE money.
  Fix: do NOT apply imbalance as a fair-value shift.  Instead, treat it
  as a RISK FILTER: if book is imbalanced in the same direction as our
  inventory, slightly suppress that side's quote to avoid doubling down
  into an adverse move.

▸ FAIR VALUE
  Both products: wall_mid is the best FV (MAE < 1.1 ticks).
  ASH_OSMIUM: additionally anchor toward 10 000 with a gentle 15 % pull
  (captures the documented mean-reversion without overfitting).
  PEPPER_ROOT: pure wall_mid — the daily drift is an interday phenomenon
  and does not help intra-day quoting.

▸ SPREAD REGIME
  Both products have a stable WIDE regime (most ticks).
  Narrow-spread events (below NARROW_SPREAD_THR) carry higher volatility.
  In narrow-regime: tighten risk (suppress quotes if inventory is adverse).

▸ QUOTE PLACEMENT
  The best_bid / best_ask are typically 2 ticks inside the walls.
  Stepping 1 tick inside best gives the best queue priority vs spread.
  The two-level quoting from v12 is retained and adapted.

▸ INVENTORY / AS
  GAMMA = 0.06 retained from v12 (shown optimal).
  Max AS skew at pos=50, σ=1.9: 50·0.06·3.6 ≈ 10.8 ticks — meaningful.
  LEAN_FRAC = 0.5 (symmetric ±25 for pos_limit=50).

▸ EXIT TAKER
  Imbalance exit taker from v12 is REMOVED.
  The imbalance signal is contra (negative alpha), so firing an
  aggressive buy on positive imbalance would be incorrect.
  Replace with: if |pos| > HARD_EXIT_FRAC × limit → cross spread to
  exit half the excess.  Pure inventory-driven, no signal required.

ARCHITECTURE  (inherits v12 five-layer design, adapted for Round 1)
────────────────────────────────────────────────────────────────────
  Layer 1 STATE     → rolling wm_history, sigma, imbalance, regime
  Layer 2 SIGNAL    → regime, contra_imb_risk, mr_deviation (ASH only)
  Layer 3 PRICING   → FV  =  wall_mid [+ MR adj]
                      r    =  FV − pos·γ·σ²
  Layer 4 RISK      → lean suppression, hard-exit taker, spread guard
  Layer 5 EXECUTION → two-level passive quotes (inner + outer)
"""

from datamodel import OrderDepth, TradingState, Order
import json
import math

# ═══════════════════════════════ Config ═══════════════════════════════════════

PEPPER_SYMBOL = "INTARIAN_PEPPER_ROOT"
OSMIUM_SYMBOL = "ASH_COATED_OSMIUM"

POS_LIMITS = {
    PEPPER_SYMBOL: 80,
    OSMIUM_SYMBOL: 80,
}

# ── Shared AS / risk parameters ───────────────────────────────────────────────
GAMMA       = 0.06   # Avellaneda-Stoikov risk aversion.
                     # Skew at max pos=50, σ=1.9: 50·0.06·3.6 ≈ 10.8 ticks.

VOL_WINDOW  = 20     # Rolling window for σ estimation (ticks).
SIGMA_FLOOR = 1.0    # Min σ to prevent zero-spread quoting.

LEAN_FRAC   = 0.5    # Suppress quoting when |pos| > LEAN_FRAC · limit (±25).

# Hard exit: cross spread if |pos| > this fraction of limit
HARD_EXIT_FRAC   = 0.80   # e.g. 40 out of 50 → cross to exit
HARD_EXIT_RATIO  = 0.40   # exit this fraction of the excess

# Imbalance risk filter (NOT a directional signal — alpha is NEGATIVE / contra)
# Suppress the same-side quote when imbalance reinforces existing inventory
IMB_RISK_THR     = 0.30   # |imbalance| threshold to trigger contra risk

# Two-level quote sizing
INNER_FRAC  = 0.40   # fraction of capacity at inner (better) price
OUTER_FRAC  = 0.60   # remainder at outer price

# ── Product-specific parameters ───────────────────────────────────────────────

# PEPPER ROOT
PEPPER_SIGMA    = 1.75    # Calibrated from data (std of mid changes).
PEPPER_NARROW   = 10      # Spread below this → elevated vol regime.

# ASH OSMIUM
OSMIUM_SIGMA    = 1.93    # Calibrated from data.
OSMIUM_FAIR     = 10000.0 # Fundamental anchor price.
OSMIUM_MR_PULL  = 0.15    # FV = wall_mid + 0.15*(10000 − wall_mid).
                           # Calibrated: MR beta −0.243/tick at dev 10.
OSMIUM_NARROW   = 13      # Spread below this → elevated vol regime.


# ═══════════════════════════ Base ProductTrader ═══════════════════════════════

class ProductTrader:
    """
    Shared order-book parsing and order-placement helpers.

    Provides:
      bids        dict{price: volume}, sorted descending (best first)
      asks        dict{price: volume}, sorted ascending  (best first)
      best_bid / best_ask   top-of-book prices
      bid_wall / ask_wall   outermost resting prices
      wall_mid              (bid_wall + ask_wall) / 2
      inner_spread          best_ask − best_bid
      _buy(price, vol) / _sell(price, vol)   clipped order placement
    """

    def __init__(self, symbol: str, state: TradingState, new_td: dict, last_td: dict):
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

        self.wall_mid = (
            (self.bid_wall + self.ask_wall) / 2
            if self.bid_wall is not None and self.ask_wall is not None
            else None
        )
        self.inner_spread = (
            (self.best_ask - self.best_bid)
            if self.best_bid is not None and self.best_ask is not None
            else None
        )

    # ── Order helpers ─────────────────────────────────────────────────────────

    def _buy(self, price: float, volume: float) -> int:
        """Place a clipped limit buy."""
        vol = min(int(abs(volume)), self.buy_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), vol))
            self.buy_cap -= vol
        return vol

    def _sell(self, price: float, volume: float) -> int:
        """Place a clipped limit sell."""
        vol = min(int(abs(volume)), self.sell_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), -vol))
            self.sell_cap -= vol
        return vol

    def get_orders(self):
        return self.orders


# ═══════════════════════ WallMidMarketMaker (base MM) ════════════════════════

class WallMidMarketMaker(ProductTrader):
    """
    Five-layer market maker.  Subclasses override the class-level config
    attributes and/or individual layer methods for product customization.

    LAYER 1: STATE   — rolling wall_mid history → sigma
    LAYER 2: SIGNAL  — imbalance, regime, MR deviation
    LAYER 3: PRICING — fair value, AS reservation price
    LAYER 4: RISK    — hard exit taker, lean suppression
    LAYER 5: EXEC    — two-level passive quotes
    """

    # ── Product-specific config (override in subclass) ────────────────────────
    SIGMA_INIT    = 1.5     # Starting sigma before history builds up
    NARROW_THR    = 11      # Spread below this → narrow/volatile regime
    MR_ANCHOR     = None    # Mean-reversion price anchor (None = disabled)
    MR_PULL       = 0.0     # Fraction of gap to anchor to pull FV each tick

    # ══════════════════ LAYER 1: STATE ════════════════════════════════════════

    def _load_state(self) -> None:
        """
        Restore and update rolling wall_mid history.

        State key: f'{symbol}_wm_hist'  →  list[float]

        The history drives sigma computation.  We cap at VOL_WINDOW + 1
        entries to bound memory usage.
        """
        key  = f'{self.symbol}_wm_hist'
        hist = self.last_td.get(key, [])

        if self.wall_mid is not None:
            hist.append(self.wall_mid)
            if len(hist) > VOL_WINDOW + 1:
                hist = hist[-(VOL_WINDOW + 1):]

        self.wm_history = hist
        self.new_td[key] = hist

    # ══════════════════ LAYER 2: SIGNAL ═══════════════════════════════════════

    def _compute_sigma(self) -> float:
        """
        Rolling realised volatility of wall_mid changes.

          σ = std( Δwall_mid ) over VOL_WINDOW ticks

        Floored at SIGMA_FLOOR to prevent degenerate quote widths.
        Falls back to SIGMA_INIT while history is short.

        Where:
          wm_history   list of recent wall_mid prices
          VOL_WINDOW   rolling window length (20)
          SIGMA_FLOOR  minimum σ (1.0)
          SIGMA_INIT   fallback σ when history < 3 (product default)
        """
        if len(self.wm_history) < 3:
            return self.SIGMA_INIT

        diffs = [self.wm_history[i] - self.wm_history[i - 1]
                 for i in range(1, len(self.wm_history))]
        n    = len(diffs)
        mean = sum(diffs) / n
        var  = sum((d - mean) ** 2 for d in diffs) / max(n - 1, 1)
        return max(math.sqrt(var), SIGMA_FLOOR)

    def _compute_imbalance(self) -> float:
        """
        Depth-weighted order-book imbalance across all visible levels.

          imbalance = (Σ_bid_vol − Σ_ask_vol) / (Σ_bid_vol + Σ_ask_vol)

        Range [−1, +1].  +1 = all book volume on bid side.

        NOTE: In Round 1 data the OLS alpha of imbalance vs next mid
        change is NEGATIVE (−0.54 to −0.71).  The heavy side is the
        market-maker quoting side, NOT aggressive order flow.
        We use imbalance ONLY as a risk filter, NOT as a price signal.

        Where:
          bids / asks   order book dicts {price: volume}
        """
        total_bid = sum(self.bids.values())
        total_ask = sum(self.asks.values())
        denom = total_bid + total_ask
        return (total_bid - total_ask) / denom if denom > 0 else 0.0

    def _classify_regime(self, spread: float) -> str:
        """
        Classify market regime from current inner spread.

          WIDE   spread ≥ NARROW_THR  →  normal quoting regime
          NARROW spread < NARROW_THR  →  compressed / elevated vol regime

        Data: narrow-spread co-occurs with higher realised vol
        (wide spread: ~0.6 tick mean |Δmid|; narrow: ~1.1 tick).

        Where:
          spread     current best_ask − best_bid
          NARROW_THR product-specific threshold (ticks)
        """
        if spread is None or spread >= self.NARROW_THR:
            return 'WIDE'
        return 'NARROW'

    # ══════════════════ LAYER 3: PRICING ══════════════════════════════════════

    def _fair_value(self) -> float:
        """
        Product fair value.

        Base case:  FV = wall_mid    (MAE < 1.1 ticks across both products)

        Optional mean-reversion anchor (ASH OSMIUM only):
          FV = wall_mid + MR_PULL · (MR_ANCHOR − wall_mid)

        Where:
          wall_mid   (bid_wall + ask_wall) / 2
          MR_ANCHOR  fundamental anchor price (10 000 for ASH)
          MR_PULL    fractional pull toward anchor per tick (0.15 for ASH)
                     Calibrated from MR beta −0.243/tick at deviation 10.
        """
        fv = self.wall_mid
        if self.MR_ANCHOR is not None and self.MR_PULL > 0:
            fv += self.MR_PULL * (self.MR_ANCHOR - self.wall_mid)
        return fv

    def _reservation_price(self, fv: float, sigma: float) -> float:
        """
        Avellaneda-Stoikov inventory-adjusted indifference price.

          r = FV − q · γ · σ²

        Where:
          FV       fair value from _fair_value()
          q        current signed inventory (position)
          γ        risk-aversion coefficient (GAMMA = 0.06)
          σ        rolling volatility of wall_mid changes

        Interpretation:
          Long position  (q > 0): r < FV → quotes shift down →
            lower bid (buy less willingly) + lower ask (sell more willingly).
          Short position (q < 0): r > FV → quotes shift up →
            higher bid (buy more willingly) + higher ask (sell less).

        At max pos=50, σ=1.93, γ=0.06:
          skew = 50·0.06·3.72 ≈ 11.2 ticks — meaningfully tilts quotes.
        """
        return fv - self.position * GAMMA * (sigma ** 2)

    # ══════════════════ LAYER 4: RISK ═════════════════════════════════════════

    def _hard_exit_taker(self, fv: float) -> None:
        """
        Inventory-driven aggressive exit.

        Fires when |position| > HARD_EXIT_FRAC × pos_limit.
        Unlike the v12 imbalance-based exit, this uses NO imbalance signal
        (because Round 1 imbalance alpha is CONTRA, not directional).

        Action: cross the spread to exit HARD_EXIT_RATIO of the excess
        inventory.  This prevents position-limit lockout during volatile
        periods.

        Where:
          HARD_EXIT_FRAC   fraction of limit above which we act (0.80)
          HARD_EXIT_RATIO  fraction of excess to exit in one shot (0.40)
          fv               current fair value
        """
        threshold = self.pos_limit * HARD_EXIT_FRAC   # e.g. 40

        if self.position > threshold and self.best_bid is not None:
            # Too long → sell aggressively at best bid
            excess   = self.position - int(self.pos_limit * LEAN_FRAC)
            exit_vol = max(1, int(excess * HARD_EXIT_RATIO))
            self._sell(self.best_bid, exit_vol)

        elif self.position < -threshold and self.best_ask is not None:
            # Too short → buy aggressively at best ask
            excess   = -self.position - int(self.pos_limit * LEAN_FRAC)
            exit_vol = max(1, int(excess * HARD_EXIT_RATIO))
            self._buy(self.best_ask, exit_vol)

    def _lean_allowed(self, side: str, imbalance: float) -> bool:
        """
        Determine whether quoting on this side is permitted.

        Hard lean:   suppress side entirely if |pos| > LEAN_FRAC × limit
                     This prevents the algo hitting position limits through
                     one-sided accumulation.

        Imbalance risk filter (CONTRA signal):
          If book is heavily bid (imbalance > +IMB_RISK_THR) AND we already
          hold a long position → suppress bid quote.  This avoids adding
          inventory in the direction the book says mean-reverts downward.
          Symmetric for short side.

          NOTE: We are NOT using imbalance as a directional alpha signal.
          We are using it purely to avoid doubling down into an adverse
          mean-reversion.

        Where:
          side       'bid' or 'ask'
          imbalance  depth-weighted (Σbid−Σask)/(Σbid+Σask)
          LEAN_FRAC  position fraction above which we suppress (0.5)
          IMB_RISK_THR  imbalance magnitude to trigger suppression (0.30)
        """
        lean_lvl = self.pos_limit * LEAN_FRAC  # e.g. 25

        if side == 'bid':
            # Hard lean: already too long
            if self.position >= lean_lvl:
                return False
            # Contra risk: heavy bid side + net long → mean-reversion risk DOWN
            if imbalance > IMB_RISK_THR and self.position > 0:
                return False
            return True
        else:  # ask
            # Hard lean: already too short
            if self.position <= -lean_lvl:
                return False
            # Contra risk: heavy ask side + net short → mean-reversion risk UP
            if imbalance < -IMB_RISK_THR and self.position < 0:
                return False
            return True

    # ══════════════════ LAYER 5: EXECUTION ════════════════════════════════════

    def _two_level_maker(self, reservation: float, fv: float,
                         imbalance: float, regime: str) -> None:
        """
        Two-level passive quoting schema with full risk gating.

        INNER QUOTE  (1 tick inside best competition):
          Captures spread from patient passive counterparties.
          Gets queue priority over orders at the same price.
          Sized at INNER_FRAC of remaining capacity.

        OUTER QUOTE  (1 tick inside the wall):
          Safety net for larger market sweeps.
          Sized at OUTER_FRAC (remainder).

        Safety clamps:
          bid ≤ int(fv) − 1   (never pay above fair value)
          ask ≥ int(fv) + 1   (never sell below fair value)

        Additional NARROW regime guard:
          In narrow-spread regime, tighten clamps by 1 extra tick
          to reduce adverse-selection exposure during elevated vol.

        Where:
          reservation  AS-skewed indifference price from _reservation_price()
          fv           fair value from _fair_value()
          imbalance    depth-weighted book imbalance
          regime       'WIDE' or 'NARROW'
        """
        if self.best_bid is None or self.best_ask is None:
            return

        # Extra buffer in narrow (high-vol) regime
        narrow_buf = 1 if regime == 'NARROW' else 0

        # ── BID SIDE ──────────────────────────────────────────────────────────
        if self._lean_allowed('bid', imbalance) and self.buy_cap > 0:

            # Inner: step 1 tick ahead of best competing bid < reservation
            inner_bid = int(self.bid_wall) + 1
            for bp, bv in self.bids.items():
                if bp < reservation:
                    inner_bid = max(inner_bid, int(bp) + 1 if bv > 1 else int(bp))
                    break

            # Safety clamp: never buy above fair value
            fv_clamp  = int(fv) - 1 - narrow_buf
            inner_bid = min(inner_bid, fv_clamp)

            # Outer: just inside the outer wall
            outer_bid = min(int(self.bid_wall) + 1, int(fv) - 2 - narrow_buf)
            outer_bid = max(outer_bid, int(self.bid_wall))  # floor at wall

            inner_vol = max(1, int(self.buy_cap * INNER_FRAC))
            outer_vol = self.buy_cap - inner_vol

            if inner_bid > outer_bid:
                self._buy(inner_bid, inner_vol)
                if outer_vol > 0:
                    self._buy(outer_bid, outer_vol)
            else:
                self._buy(inner_bid, self.buy_cap)

        # ── ASK SIDE ──────────────────────────────────────────────────────────
        if self._lean_allowed('ask', imbalance) and self.sell_cap > 0:

            # Inner: step 1 tick ahead of best competing ask > reservation
            inner_ask = int(self.ask_wall) - 1
            for ap, av in self.asks.items():
                if ap > reservation:
                    inner_ask = min(inner_ask, int(ap) - 1 if av > 1 else int(ap))
                    break

            # Safety clamp: never sell below fair value
            fv_clamp  = int(fv) + 1 + narrow_buf
            inner_ask = max(inner_ask, fv_clamp)

            # Outer: just inside the outer wall
            outer_ask = max(int(self.ask_wall) - 1, int(fv) + 2 + narrow_buf)
            outer_ask = min(outer_ask, int(self.ask_wall))  # ceiling at wall

            inner_vol = max(1, int(self.sell_cap * INNER_FRAC))
            outer_vol = self.sell_cap - inner_vol

            if outer_ask > inner_ask:
                self._sell(inner_ask, inner_vol)
                if outer_vol > 0:
                    self._sell(outer_ask, outer_vol)
            else:
                self._sell(inner_ask, self.sell_cap)

    # ══════════════════ MAIN ENTRY POINT ══════════════════════════════════════

    def get_orders(self):
        """
        Execute all five layers in sequence.

        Order:
          1. State   → history, sigma
          2. Signal  → imbalance, regime
          3. Pricing → fair value, reservation price
          4. Risk    → hard-exit taker (uses capacity first)
          5. Exec    → two-level passive maker (uses remaining capacity)
        """
        if self.wall_mid is None:
            return self.orders

        # Layer 1
        self._load_state()
        sigma = self._compute_sigma()

        # Layer 2
        imbalance = self._compute_imbalance()
        regime    = self._classify_regime(self.inner_spread)

        # Layer 3
        fv          = self._fair_value()
        reservation = self._reservation_price(fv, sigma)

        # Layer 4: hard exit first (consumes capacity on one side)
        self._hard_exit_taker(fv)

        # Layer 5: passive maker uses remaining capacity
        self._two_level_maker(reservation, fv, imbalance, regime)

        return self.orders


# ════════════════════════════ Product Subclasses ══════════════════════════════

class PepperRootTrader(WallMidMarketMaker):
    """
    INTARIAN_PEPPER_ROOT market maker.

    Fair value:  pure wall_mid
      — wall_mid MAE = 0.87 ticks (best available estimator)
      — Daily drift (+1000/day) is an inter-day phenomenon; no intra-day
        trend signal is statistically reliable in the tick data.

    Imbalance: CONTRA (OLS alpha −0.71).
      Used only as a risk filter, not as an FV shift.

    Parameters calibrated from prices_round_1_day_{-2,-1,0}.csv:
      σ = 1.75 ticks, inner spread median = 13, outer = 17
      NARROW_THR = 10 (bottom 10th percentile of spread distribution)
    """
    SIGMA_INIT = PEPPER_SIGMA     # 1.75 ticks
    NARROW_THR = PEPPER_NARROW    # 10 ticks
    MR_ANCHOR  = None             # No fundamental anchor
    MR_PULL    = 0.0


class OsmiumTrader(WallMidMarketMaker):
    """
    ASH_COATED_OSMIUM market maker.

    Fair value:  wall_mid + MR pull toward 10 000
      FV = wall_mid + 0.15 · (10 000 − wall_mid)
      Calibrated from: MR beta −0.243 ticks per tick at deviation 10.
      All-day mean = 10 000.2, std = 4.9.  Anchor is robust.

    Imbalance: CONTRA (OLS alpha −0.54).
      Used only as a risk filter, not as an FV shift.

    Parameters calibrated from prices_round_1_day_{-2,-1,0}.csv:
      σ = 1.93 ticks, inner spread median = 16, outer = 20
      NARROW_THR = 13 (below typical median of 16)
    """
    SIGMA_INIT = OSMIUM_SIGMA     # 1.93 ticks
    NARROW_THR = OSMIUM_NARROW    # 13 ticks
    MR_ANCHOR  = OSMIUM_FAIR      # 10 000
    MR_PULL    = OSMIUM_MR_PULL   # 0.15


# ════════════════════════════════ Main Trader ═════════════════════════════════

class Trader:
    """
    Top-level competition interface.

    run(state) → (orders: dict, conversions: int, traderData: str)
    """

    PRODUCT_MAP = {
        PEPPER_SYMBOL: PepperRootTrader,
        OSMIUM_SYMBOL: OsmiumTrader,
    }

    def run(self, state: TradingState):
        # ── Deserialise persistent state ──────────────────────────────────────
        last_td: dict = {}
        try:
            if state.traderData:
                last_td = json.loads(state.traderData)
        except Exception:
            pass

        new_td: dict = {}
        result: dict = {}

        # ── Run each product trader ───────────────────────────────────────────
        for symbol, TraderClass in self.PRODUCT_MAP.items():
            if symbol not in state.order_depths:
                continue
            try:
                trader = TraderClass(symbol, state, new_td, last_td)
                orders = trader.get_orders()
                if orders:
                    result[symbol] = orders
            except Exception as err:
                print(f"[ERR] {symbol}: {err}")

        # ── Serialise state ───────────────────────────────────────────────────
        try:
            trader_data_out = json.dumps(new_td)
        except Exception:
            trader_data_out = ""

        return result, 0, trader_data_out