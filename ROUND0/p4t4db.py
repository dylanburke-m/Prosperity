"""
IMC Prosperity 4 — v7 (Wall Mid MM + MR Signal + Better Inventory)
===================================================================

CHANGES FROM v6
────────────────
1. FRACTIONAL WALL_MID FIX: Use round() instead of int() for safety
   check. When wall_mid = X.5, int() truncates and wastes 1 tick of
   edge on the bid side. round() preserves correct pricing.

2. GRADUATED INVENTORY LEAN: Instead of a binary lean at 60% threshold,
   use a graduated system that tightens quotes proportionally as
   position grows. Starts gentle, gets aggressive near limits.

3. MEAN REVERSION QUOTE SKEWING (Tomatoes only):
   Lag-1 autocorrelation = -0.21. When price just moved up, bias
   quotes to favor selling (tighten ask, widen bid) and vice versa.
   This directs fill flow toward the mean-reversion direction.

4. POSITION-AWARE QUOTING: When position is large, the flattening
   side gets tighter by 1-2 ticks relative to baseline, while the
   growing side gets wider. This keeps inventory cycling near zero.

5. ACTIVE TAKING FOR FLATTENING: When position exceeds 60% of limit
   AND there's a favorable L1 quote (positive edge), take it to
   flatten. This prevents getting stuck at limits.

DATA FINDINGS (unchanged from v6)
───────────────────────────────────
EMERALDS
  100% of taker trades happen at bid1 (9992) or ask1 (10008).
  wall_mid = 10000 (constant). Our 9993/10007 quotes intercept takers.
  Strategy: unchanged from v6 (already near-optimal).

TOMATOES
  100% of taker trades happen at bid1 or ask1 (per-day merge confirms).
  wall_mid tracks true price; L1 spread ≈ 13 ticks; L2 spread ≈ 16.
  Lag-1 autocorrelation of wall_mid returns: -0.21 (mean-reverting).
  Day 1 price drift: -49 ticks. Day 2 drift: +6.5 ticks.
  Position drag at limit (80 units × 49 tick drift) = 3920 SeaShells!
  → Aggressive flattening is CRITICAL for Tomatoes.
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


# ═══════════════════════════ Base ProductTrader ═══════════════════════════════

class ProductTrader:

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


# ════════════════════ Emerald Trader (unchanged core logic) ═══════════════════

class EmeraldTrader(ProductTrader):
    """
    Emeralds: Static fair value at 10000. All takers at 9992/10008.
    Our 9993/10007 quotes are optimal: maximum edge while intercepting L1.
    
    Only change from v6: graduated lean for better inventory cycling.
    """

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        wm = self.wall_mid

        # ── ① TAKING ─────────────────────────────────────────────────────
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

        # ── ② MAKING: Overbid / Underask ─────────────────────────────────
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

        # ── ③ GRADUATED INVENTORY LEAN ────────────────────────────────────
        pos_frac = self.position / self.pos_limit  # -1 to +1
        
        if abs(pos_frac) > 0.3:
            # Lean strength: 0 at 30%, max at 100%
            lean_strength = (abs(pos_frac) - 0.3) / 0.7  # 0 to 1
            lean_ticks = int(lean_strength * 3)  # 0-3 ticks of lean
            
            if self.position > 0:  # long → tighten ask
                ask_quote = min(ask_quote, int(wm) + 1 + max(0, 2 - lean_ticks))
            else:  # short → tighten bid
                bid_quote = max(bid_quote, int(wm) - 1 - max(0, 2 - lean_ticks))

        # Safety: always maintain positive edge
        bid_quote = min(int(bid_quote), int(wm) - 1)
        ask_quote = max(int(ask_quote), int(wm) + 1)

        self._buy(bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)

        return self.orders


# ════════════════════ Tomato Trader (enhanced with MR signal) ═════════════════

class TomatoTrader(ProductTrader):
    """
    Tomatoes: Moving fair value, 100% of trades at L1.
    
    Enhancements over v6:
    ① Fractional wall_mid fix (round vs int in safety check)
    ② Mean-reversion quote skewing based on last return
    ③ Graduated position lean (starts at 20% of limit)
    ④ Active flattening via taking when position is extreme
    ⑤ EMA tracking for trend detection
    """

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        wm = self.wall_mid

        # ── TRACK EMA AND LAST RETURN ─────────────────────────────────────
        last_wm = self.last_td.get('tom_wm', wm)
        last_return = wm - last_wm
        
        # EMA(10) for longer-term trend
        old_ema = self.last_td.get('tom_ema', wm)
        alpha = 2 / (10 + 1)
        new_ema = alpha * wm + (1 - alpha) * old_ema
        ema_dev = wm - new_ema  # positive = above trend
        
        self.new_td['tom_wm'] = wm
        self.new_td['tom_ema'] = new_ema

        # ── ① TAKING: sweep favorable + active flatten ───────────────────
        # Standard taking: buy cheap, sell dear
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

        # Active flatten: when position is extreme, take at small edge to reduce
        pos_frac = abs(self.position) / self.pos_limit
        if pos_frac > 0.7:
            if self.position > 0 and self.best_bid is not None:
                # Long and extreme → sell at best_bid if edge >= 1
                if self.best_bid >= wm + 1:
                    flatten_vol = min(abs(self.position) - int(0.3 * self.pos_limit), 
                                     self.bids.get(self.best_bid, 0))
                    if flatten_vol > 0:
                        self._sell(self.best_bid, flatten_vol)
            elif self.position < 0 and self.best_ask is not None:
                # Short and extreme → buy at best_ask if edge >= 1
                if self.best_ask <= wm - 1:
                    flatten_vol = min(abs(self.position) - int(0.3 * self.pos_limit),
                                     self.asks.get(self.best_ask, 0))
                    if flatten_vol > 0:
                        self._buy(self.best_ask, flatten_vol)

        # ── ② MAKING: Overbid / Underask with MR skew ────────────────────
        bid_quote = int(self.bid_wall) + 1
        ask_quote = int(self.ask_wall) - 1

        # Standard overbid/underask
        for bp, bv in self.bids.items():
            if bp < wm:
                bid_quote = max(bid_quote, int(bp) + 1 if bv > 1 else int(bp))
                break

        for ap, av in self.asks.items():
            if ap > wm:
                ask_quote = min(ask_quote, int(ap) - 1 if av > 1 else int(ap))
                break

        # ── MEAN REVERSION SKEW ──────────────────────────────────────────
        # If price just went UP → expect DOWN → tighten ask, widen bid
        # If price just went DOWN → expect UP → tighten bid, widen ask
        # Skew magnitude: 1 tick for normal moves, 2 for large moves
        mr_skew = 0
        if abs(last_return) >= 0.5:
            mr_skew = 1  # base skew
        if abs(last_return) >= 1.5:
            mr_skew = 2  # strong skew for larger moves
        
        if last_return > 0:  # price went up → expect down
            ask_quote -= mr_skew   # tighten ask (more eager to sell)
            bid_quote -= mr_skew   # widen bid (less eager to buy)
        elif last_return < 0:  # price went down → expect up
            bid_quote += mr_skew   # tighten bid (more eager to buy)
            ask_quote += mr_skew   # widen ask (less eager to sell)

        # ── GRADUATED INVENTORY LEAN ─────────────────────────────────────
        pos_frac_signed = self.position / self.pos_limit  # -1 to +1
        
        if abs(pos_frac_signed) > 0.2:
            lean_strength = min((abs(pos_frac_signed) - 0.2) / 0.6, 1.0)  # 0→1
            lean_ticks = round(lean_strength * 4)  # 0-4 ticks
            
            if self.position > 0:  # long → tighten ask to sell faster
                ask_quote -= lean_ticks
            else:  # short → tighten bid to buy faster
                bid_quote += lean_ticks

        # ── SAFETY CHECK (fractional fix) ─────────────────────────────────
        # Use math.floor/ceil for proper handling of X.5 wall_mids
        # Bid must be BELOW wall_mid (positive edge for buying)
        # Ask must be ABOVE wall_mid (positive edge for selling)
        max_bid = math.floor(wm - 0.01)  # highest integer strictly below wm
        min_ask = math.ceil(wm + 0.01)   # lowest integer strictly above wm
        
        bid_quote = min(int(bid_quote), max_bid)
        ask_quote = max(int(ask_quote), min_ask)

        # Post passive quotes
        self._buy(bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)

        return self.orders


# ════════════════════════════════ Main Trader ═════════════════════════════════

class Trader:

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