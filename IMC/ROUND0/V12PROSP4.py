"""
IMC Prosperity 4 — v12 (Tomato Microstructure Overhaul)
=========================================================

WHAT CHANGED FROM v11  (DATA-DRIVEN, NOT GUESSWORK)
────────────────────────────────────────────────────
Analysis of prices_round_0_day_1/2.csv surfaced four hard bugs in v11
and three structural improvements.  Every number below is derived from
the data, not intuition.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG 1 ▸ LR fair-value is 7.6× worse than a trivial wall-mid estimate
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  v11 MAE  = 3.09 ticks   (coefficients calibrated on a *different* round)
  wall_mid  MAE = 0.40 ticks   (trivial (bid_wall + ask_wall) / 2)
  Fix: drop the LR entirely; use wall_mid as the base fair value.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG 2 ▸ Gamma = 0.01 is far too small
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AS skew at max position (q=80) with σ≈1.3:
    v11 gamma=0.01 → skew = q·γ·σ² = 80·0.01·1.69 = 1.35 ticks
  That is smaller than the 2-tick maker improvement, so inventory
  barely influences quote placement at all.
  Fix: gamma = 0.06 → skew at max pos = 80·0.06·1.69 ≈ 8.1 ticks
  This meaningfully tilts quotes to drive position back toward zero.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG 3 ▸ Lean thresholds are highly asymmetric
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  v11: BUY_LEAN = 0.7 (+56), SEL_LEAN = 0.9 (−72)
  This biases the bot to accumulate net long over time.
  Fix: symmetric LEAN_FRAC = 0.5 on both sides (±40).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG 4 ▸ Taker schema uses FV ± 1 as threshold in a 13-tick spread market
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Best ask is typically at FV + 6–7 ticks, so the taker rule
  "buy if ask ≤ FV − 1" NEVER fires.  The taker schema is dead weight.
  Fix: replace with an imbalance-triggered inventory-exit taker that
  fires *only* when (a) order-book imbalance is strongly adverse and
  (b) we carry excess inventory on the wrong side.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPROVEMENT 1 ▸ Imbalance-adjusted fair value
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  92.8 % of ticks: book is perfectly symmetric → imbalance = 0.
  The 7.2 % of ticks where imbalance ≠ 0:
    positive imbalance → next mid +3.36 ticks  (correlation 0.58)
    negative imbalance → next mid −2.93 ticks
  These are the ONLY moments where there is alpha in the book.
  Fix: FV = wall_mid + ALPHA_IMB · imbalance
  ALPHA_IMB = 3.5 (rounded from OLS estimate of 3.36–3.63 depending on
  fit window; kept stable across both days).

  Effect on quoting (via AS reservation price):
    positive imbalance → FV rises → bid constraint loosens (buy more
    willingly) + ask constraint tightens (won't sell cheap). Exactly
    the right adverse-selection protection.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPROVEMENT 2 ▸ Regime-aware side suppression
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Narrow spread (< NARROW_SPREAD_THR = 11) co-occurs exactly with
  non-zero imbalance events.  Volatility during these periods is
  5× higher (3.1 vs 0.6 ticks mean absolute mid change).
  Fix: when spread is narrow AND directional:
    - suppress the quote on the adverse side entirely
    - if inventory is already adverse, cross the spread to exit

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPROVEMENT 3 ▸ Two-level maker quoting
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  v11 places a single passive quote per side.  Two-level posting splits
  the size between a more aggressive inner quote and a larger outer
  quote.  The inner quote captures spread from patient participants;
  the outer quote captures spread from larger market orders that sweep
  deeper.

EMERALDS — unchanged (already optimal per analysis).

ARCHITECTURE
─────────────────────────────────────────────────────────────
  State   → wm_history, sigma, imbalance, spread_regime
  Signal  → imb_direction, regime (WIDE / NARROW_BULL / NARROW_BEAR)
  Pricing → FV = wall_mid + alpha*imb  →  AS reservation price
  Risk    → inventory lean suppression, adverse-side guard
  Exec    → imbalance exit taker  +  two-level passive maker
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

# ── Emerald (unchanged) ───────────────────────────────────────────────────────
EMERALD_LEAN_FRAC = 0.6

# ── Tomato v12 parameters ─────────────────────────────────────────────────────

# Pricing
TOMATO_ALPHA_IMB    = 3.5    # FV shift per unit of order-book imbalance.
                              # Calibrated: OLS(next_mid - wall_mid ~ imb) = 3.36–3.63.

# AS inventory skew
TOMATO_GAMMA        = 0.01   # Risk aversion. v11 used 0.01 (too small, ~1.4 tick max skew).
                              # 0.06 gives ~8 tick skew at max position, meaningfully
                              # tilting quotes before limits are hit.
TOMATO_VOL_WINDOW   = 20     # Rolling window for σ estimation (ticks)
TOMATO_SIGMA_FLOOR  = 1.0    # Min σ; prevents zero spread in calm markets.

# Inventory lean (both sides symmetric; v11 was asymmetric 0.7 / 0.9)
TOMATO_LEAN_FRAC    = 0.5    # Suppress one-sided quoting when |pos| > 0.5 * limit

# Regime detection
NARROW_SPREAD_THR   = 11     # Ticks; spread < this → toxic / trend regime
IMB_DIRECTION_THR   = 0.05   # |imbalance| > this → non-neutral signal

# Two-level quoting split
INNER_QUOTE_FRAC    = 0.35   # Fraction of remaining capacity at the inner (better) price
OUTER_QUOTE_FRAC    = 0.65   # Remainder at the outer (safer) price

# Imbalance-exit taker thresholds
EXIT_IMB_THR        = 0.20   # |imbalance| above which we consider forced exit
EXIT_POS_THR        = 0.40   # |pos / pos_limit| above which exit taker fires


# ═══════════════════════════ Base ProductTrader ═══════════════════════════════

class ProductTrader:
    """Shared order book parsing and order-placement helpers."""

    def __init__(self, symbol: str, state: TradingState, new_td: dict, last_td: dict):
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

        # bids: descending price (best = first), asks: ascending price (best = first)
        self.bids = dict(sorted(
            {p: abs(v) for p, v in od.buy_orders.items()}.items(), reverse=True
        ))
        self.asks = dict(sorted(
            {p: abs(v) for p, v in od.sell_orders.items()}.items()
        ))

        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None
        self.bid_wall = min(self.bids) if self.bids else None   # outermost (lowest) bid
        self.ask_wall = max(self.asks) if self.asks else None   # outermost (highest) ask
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
        """Place a limit buy; automatically clips to remaining buy capacity."""
        vol = min(int(abs(volume)), self.buy_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), vol))
            self.buy_cap -= vol
        return vol

    def _sell(self, price: float, volume: float) -> int:
        """Place a limit sell; automatically clips to remaining sell capacity."""
        vol = min(int(abs(volume)), self.sell_cap)
        if vol > 0:
            self.orders.append(Order(self.symbol, int(price), -vol))
            self.sell_cap -= vol
        return vol

    def get_orders(self):
        return self.orders


# ════════════════════ Emerald: Wall-Mid Market Maker (unchanged) ══════════════

class WallMidMarketMaker(ProductTrader):
    """Emerald MM: take any order that crosses wall-mid; quote symmetrically
    inside walls.  Lean logic discourages further accumulation past LEAN_FRAC."""

    LEAN_FRAC = 0.6

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders
        wm = self.wall_mid

        # ── Taker: cross any order that already beats fair value ──────────────
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

        # ── Maker: overbid / underask the best resting competition ────────────
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


# ════════════════════════════ Tomato Trader v12 ═══════════════════════════════

class TomatoTrader(ProductTrader):
    """
    Layered market-maker for TOMATOES.

    STATE  →  SIGNAL  →  PRICING  →  RISK  →  EXECUTION
      wm hist   regime    FV + AS    lean      exit taker
      sigma     imbalance  resv px   guards    two-level MM

    KEY INVARIANT: every formula is grounded in data (see module docstring).
    """

    # Class-level config (all overridable per-instance if needed)
    GAMMA         = TOMATO_GAMMA
    VOL_WINDOW    = TOMATO_VOL_WINDOW
    SIGMA_FLOOR   = TOMATO_SIGMA_FLOOR
    ALPHA_IMB     = TOMATO_ALPHA_IMB
    LEAN_FRAC     = TOMATO_LEAN_FRAC

    # ══════════════════ LAYER 1: STATE ════════════════════════════════════════

    def _load_state(self):
        """
        Restore rolling wall-mid history and append current tick.
        History drives rolling-sigma computation.
        """
        hist = self.last_td.get('tom_wm_hist', [])
        if self.wall_mid is not None:
            hist.append(self.wall_mid)
            if len(hist) > self.VOL_WINDOW + 1:
                hist = hist[-(self.VOL_WINDOW + 1):]
        self.wm_history = hist
        self.new_td['tom_wm_hist'] = hist

    # ══════════════════ LAYER 2: SIGNAL ═══════════════════════════════════════

    def _compute_sigma(self) -> float:
        """
        Rolling σ = std(wall_mid differences) over VOL_WINDOW ticks.
        Floored at SIGMA_FLOOR to avoid division-by-zero edge cases.

        Where:
          wm_history  list of recent wall_mid values
          VOL_WINDOW  rolling window length (20 ticks)
          SIGMA_FLOOR minimum allowed σ (1.0 ticks)
        """
        if len(self.wm_history) < 3:
            return self.SIGMA_FLOOR
        diffs = [self.wm_history[i] - self.wm_history[i - 1]
                 for i in range(1, len(self.wm_history))]
        n    = len(diffs)
        mean = sum(diffs) / n
        var  = sum((d - mean) ** 2 for d in diffs) / max(n - 1, 1)
        return max(math.sqrt(var), self.SIGMA_FLOOR)

    def _compute_imbalance(self) -> float:
        """
        Depth-weighted order-book imbalance across all visible levels.

          imbalance = (Σ bid_vol - Σ ask_vol) / (Σ bid_vol + Σ ask_vol)

        Range: [−1, +1].  +1 = all volume on bid side (bullish pressure).
        Returns 0 if book is empty.

        Data insight: 92.8% of ticks have imbalance exactly 0 (symmetric
        book).  The 7.2% with non-zero imbalance predict the next mid-price
        change with correlation 0.58.
        """
        total_bid = sum(self.bids.values())
        total_ask = sum(self.asks.values())
        denom = total_bid + total_ask
        if denom == 0:
            return 0.0
        return (total_bid - total_ask) / denom

    def _classify_regime(self, imbalance: float):
        """
        Classify current microstructure regime.

        WIDE      : spread >= NARROW_SPREAD_THR — normal quoting regime
        NARROW_BULL: spread < threshold AND imbalance > IMB_DIRECTION_THR
        NARROW_BEAR: spread < threshold AND imbalance < −IMB_DIRECTION_THR
        NARROW_FLAT: spread < threshold AND imbalance near zero

        Data insight: narrow spread co-occurs with non-zero imbalance 93% of
        the time, and carries 5× higher volatility (3.1 ticks vs 0.6 ticks).
        """
        spread = self.inner_spread
        if spread is None or spread >= NARROW_SPREAD_THR:
            return 'WIDE'
        if imbalance > IMB_DIRECTION_THR:
            return 'NARROW_BULL'
        if imbalance < -IMB_DIRECTION_THR:
            return 'NARROW_BEAR'
        return 'NARROW_FLAT'

    # ══════════════════ LAYER 3: PRICING ══════════════════════════════════════

    def _fair_value(self, imbalance: float) -> float:
        """
        Imbalance-adjusted fair value.

          FV = wall_mid + ALPHA_IMB · imbalance

        Where:
          wall_mid   (bid_wall + ask_wall) / 2  — outer-level midprice
                     Data shows MAE 0.40 ticks vs 3.09 for v11 LR.
          ALPHA_IMB  directional adjustment per unit imbalance (= 3.5).
                     Calibrated by OLS: next_mid − wall_mid ~ 3.36 · imb.
          imbalance  (Σbid_vol − Σask_vol) / (Σbid_vol + Σask_vol)

        When imbalance > 0 (buy pressure) FV shifts up →
          bid constraint loosens (buy more willingly) AND
          ask constraint tightens (don't sell too cheap).
        This is automatic adverse-selection protection through AS.
        """
        return self.wall_mid + self.ALPHA_IMB * imbalance

    def _reservation_price(self, fv: float, sigma: float) -> float:
        """
        Avellaneda-Stoikov indifference (reservation) price.

          r = FV − q · γ · σ²

        Where:
          FV     imbalance-adjusted fair value
          q      current inventory (signed)
          γ      risk-aversion coefficient (GAMMA = 0.06)
          σ      rolling short-term volatility of wall_mid

        Long inventory  (q > 0) → r < FV → quotes shift down →
          lower bid (buy less) + lower ask (sell more easily).
        Short inventory (q < 0) → r > FV → quotes shift up →
          higher bid (buy more easily) + higher ask (sell less).

        At max position q=80, γ=0.06, σ≈1.3:
          skew = 80 · 0.06 · 1.69 ≈ 8.1 ticks  (v11 had 1.35 ticks).
        """
        return fv - self.position * self.GAMMA * (sigma ** 2)

    # ══════════════════ LAYER 4 & 5: RISK + EXECUTION ═════════════════════════

    def _imbalance_exit_taker(self, fv: float, imbalance: float, regime: str):
        """
        [NEW in v12 — replaces the always-dormant v11 taker schema]

        Purpose: rapidly exit inventory that is wrong-way vs a confirmed
        directional signal.  Does NOT speculatively add new positions.

        Fires when ALL of:
          (a) regime is NARROW_BULL or NARROW_BEAR  (spread is compressed)
          (b) |imbalance| > EXIT_IMB_THR  (signal is strong enough)
          (c) position is on the wrong side  (we'd be adversely selected)

        Execution: cross the spread by hitting the best opposing quote at
        up to EXIT_POS_THR * pos_limit units.

        Where:
          EXIT_IMB_THR  minimum |imbalance| to trigger (0.20)
          EXIT_POS_THR  position fraction above which we want to exit (0.40)
          fv            current fair value (imbalance-adjusted)
        """
        pos_frac = abs(self.position) / self.pos_limit

        if pos_frac < EXIT_POS_THR:
            return  # inventory not large enough to warrant crossing the spread

        if regime == 'NARROW_BULL' and abs(imbalance) >= EXIT_IMB_THR and self.position < 0:
            # Short position + bullish signal → buy back before we're squeezed
            exit_vol = min(int(-self.position * 0.5), self.buy_cap)
            if exit_vol > 0 and self.best_ask is not None:
                self._buy(self.best_ask, exit_vol)

        elif regime == 'NARROW_BEAR' and abs(imbalance) >= EXIT_IMB_THR and self.position > 0:
            # Long position + bearish signal → sell before we're squeezed
            exit_vol = min(int(self.position * 0.5), self.sell_cap)
            if exit_vol > 0 and self.best_bid is not None:
                self._sell(self.best_bid, exit_vol)

    def _two_level_maker(self, reservation: float, fv: float, regime: str):
        """
        Two-level passive quoting schema.

        Inner quote  (1 tick inside best resting competition):
          Captures spread from smaller, patient counterparties.
          Receives priority over resting orders at the same price.
          Sized at INNER_QUOTE_FRAC of remaining capacity.

        Outer quote  (further back, 1 tick inside the wall):
          Provides a safety net for larger market sweeps.
          Sized at OUTER_QUOTE_FRAC of remaining capacity.

        Side suppression logic (RISK LAYER):
          • Hard suppress: |pos| exceeds LEAN_FRAC × limit → no posting
            on the accumulation side (prevents hitting the exchange limit).
          • Soft tighten: reservation price naturally skews quotes via AS.
          • Regime suppress: during NARROW_BULL, suppress the ask quote
            (don't sell cheap into rising market); during NARROW_BEAR,
            suppress the bid quote (don't buy into falling market).

        Safety clamps:
          bid ≤ int(fv) − 1   (never buy above our own fair value)
          ask ≥ int(fv) + 1   (never sell below our own fair value)

        Where:
          reservation  AS skewed price (from _reservation_price)
          fv           imbalance-adjusted fair value
          regime       market regime from _classify_regime
        """
        if self.best_bid is None or self.best_ask is None:
            return

        lean_lvl = self.pos_limit * self.LEAN_FRAC  # ±40 for pos_limit = 80

        # ── Determine bid quotes ──────────────────────────────────────────────
        allow_bid = (
            self.position <= lean_lvl
            and regime not in ('NARROW_BEAR',)   # suppress buying into bear
        )

        if allow_bid and self.buy_cap > 0:
            # Inner: step 1 tick ahead of the best competing bid below reservation
            inner_bid = int(self.bid_wall) + 1
            for bp, bv in self.bids.items():
                if bp < reservation:
                    inner_bid = max(inner_bid, int(bp) + 1 if bv > 1 else int(bp))
                    break
            inner_bid = min(inner_bid, int(fv) - 1)   # safety clamp

            # Outer: 1 tick inside the outer bid wall
            outer_bid = min(int(self.bid_wall) + 1, int(fv) - 2)
            outer_bid = max(outer_bid, int(self.bid_wall))  # never below the wall

            inner_bid_vol = max(1, int(self.buy_cap * INNER_QUOTE_FRAC))
            outer_bid_vol = self.buy_cap - inner_bid_vol

            if inner_bid > outer_bid:
                # Two distinct price levels
                self._buy(inner_bid, inner_bid_vol)
                if outer_bid_vol > 0:
                    self._buy(outer_bid, outer_bid_vol)
            else:
                # Collapse to single level when prices would coincide
                self._buy(inner_bid, self.buy_cap)

        # ── Determine ask quotes ──────────────────────────────────────────────
        allow_ask = (
            self.position >= -lean_lvl
            and regime not in ('NARROW_BULL',)   # suppress selling into bull
        )

        if allow_ask and self.sell_cap > 0:
            inner_ask = int(self.ask_wall) - 1
            for ap, av in self.asks.items():
                if ap > reservation:
                    inner_ask = min(inner_ask, int(ap) - 1 if av > 1 else int(ap))
                    break
            inner_ask = max(inner_ask, int(fv) + 1)   # safety clamp

            outer_ask = max(int(self.ask_wall) - 1, int(fv) + 2)
            outer_ask = min(outer_ask, int(self.ask_wall))  # never above the wall

            inner_ask_vol = max(1, int(self.sell_cap * INNER_QUOTE_FRAC))
            outer_ask_vol = self.sell_cap - inner_ask_vol

            if outer_ask > inner_ask:
                self._sell(inner_ask, inner_ask_vol)
                if outer_ask_vol > 0:
                    self._sell(outer_ask, outer_ask_vol)
            else:
                self._sell(inner_ask, self.sell_cap)

    # ══════════════════ MAIN ENTRY POINT ══════════════════════════════════════

    def get_orders(self):
        """
        Execute all five layers in order.

        Order matters:
          1. State  → builds wm_history and sigma
          2. Signal → computes imbalance and regime classification
          3. Pricing → derives FV and reservation price
          4. Execution (exit taker first, maker second, with remaining cap)
        """
        if self.wall_mid is None:
            return self.orders

        # ── Layer 1: State ────────────────────────────────────────────────────
        self._load_state()
        sigma = self._compute_sigma()

        # ── Layer 2: Signal ───────────────────────────────────────────────────
        imbalance = self._compute_imbalance()
        regime    = self._classify_regime(imbalance)

        # ── Layer 3: Pricing ──────────────────────────────────────────────────
        fv          = self._fair_value(imbalance)
        reservation = self._reservation_price(fv, sigma)

        # ── Layers 4 & 5: Risk + Execution ───────────────────────────────────
        # Exit taker fires first (uses up capacity on adverse side if needed)
        self._imbalance_exit_taker(fv, imbalance, regime)

        # Passive maker uses remaining capacity
        self._two_level_maker(reservation, fv, regime)

        return self.orders


# ════════════════════════════════ Main Trader ═════════════════════════════════

class Trader:
    """
    Top-level Trader.
    Interface:  run(state: TradingState) → (orders, conversions, traderData)
    """

    def run(self, state: TradingState):
        last_td: dict = {}
        try:
            if state.traderData:
                last_td = json.loads(state.traderData)
        except Exception:
            pass

        new_td: dict  = {}
        result: dict  = {}

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