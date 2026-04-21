"""
IMC Prosperity 4 - Round 2 Algorithm (v2)

=== CHANGES FROM v1 (based on live trade log analysis) ===

1. EDGE: 7 → 2
   Round 2 market bids sit at fair-1 to fair-6 (avg -4), not fair-8 like Round 1.
   With EDGE=7 our bid floor (9993) was behind the market best bid 94% of ticks
   → zero passive bid fills. With EDGE=2 we're competitive 57% of ticks.
   Simulated PnL improvement: ~74k vs ~6k per testing day.

2. DYNAMIC FAIR VALUE via wall_mid instead of static 10,000
   ACO mid averaged 10,004 in Round 2. Quoting around the actual mid price
   removes systematic directional bias and keeps both sides competitive
   as the true fair drifts intraday.
   Wall mid = (deepest_bid + deepest_ask) / 2, rounded to nearest int.
   Falls back to static ACO_FAIR_STATIC if either wall is absent.

3. INVENTORY SKEW scaled to new EDGE
   Skew ticks were hardcoded to EDGE (7). Now uses SKEW_MAJOR / SKEW_MINOR
   constants sized appropriately for the tighter EDGE=2 quotes.

4. IPR: no change to strategy — sweep all asks, post passive bid.
   Slope confirmed at 0.001677/ts in Round 2 (+65% vs Round 1), making
   fast accumulation even more important.

=== STRATEGY SUMMARY ===

INTARIAN_PEPPER_ROOT (IPR):
  Linear uptrend slope 0.001677/ts (~134k Xirecs/day at 80 units).
  Optimal: reach position limit 80 as fast as possible, never sell mid-day.
  End-of-day: flatten full position at best bid at ts >= DUMP_TS.

ASH_COATED_OSMIUM (ACO):
  Mean-reverting around a drifting fair value (use wall_mid, not static 10k).
  Round 2 bids cluster at fair-1 to fair-6 — much tighter than Round 1.
  Optimal: quote 1 tick inside market makers, floored at ±EDGE=2 from fair.
  End-of-day: flatten full position at best bid/ask at ts >= DUMP_TS.

=== MAF BID ===
  bid() returns 5,000 Xirecs.
  Estimated 3-day benefit with corrected EDGE=2: ~2.2M Xirecs vs ~190k before.
  MAF gives +25% volume → ~550k extra. Well worth the 5k fee.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional
import json


# ── Constants ─────────────────────────────────────────────────────────────────

IPR: str = "INTARIAN_PEPPER_ROOT"
ACO: str = "ASH_COATED_OSMIUM"

LIMIT: int           = 80
ACO_FAIR_STATIC: int = 10_000   # fallback if wall_mid unavailable
EDGE: int            = 2        # min ticks from fair for passive ACO quotes
                                 # (Round 2 bids avg -4 from fair; 2 keeps us competitive)
TAKE_THR: int        = 3        # ticks inside fair for aggressive takes
SKEW_MAJOR: int      = 5        # tick shift when |pos| > 50
SKEW_MINOR: int      = 1        # tick shift when |pos| > 25
DUMP_TS: int         = 999_800  # timestamp at which to flatten all positions


# ── Base product trader ───────────────────────────────────────────────────────

class ProductTrader:
    """Holds per-product state and order-book helpers for one symbol."""

    def __init__(self, symbol: str, state: TradingState) -> None:
        self.symbol: str        = symbol
        self.state: TradingState = state
        self.od: OrderDepth     = state.order_depths.get(symbol, OrderDepth())
        self.pos: int           = state.position.get(symbol, 0)
        self.orders: List[Order] = []

    # ── order-book helpers ────────────────────────────────────────────────────

    def best_bid(self) -> Optional[int]:
        return max(self.od.buy_orders) if self.od.buy_orders else None

    def best_ask(self) -> Optional[int]:
        return min(self.od.sell_orders) if self.od.sell_orders else None

    def wall_mid(self) -> Optional[int]:
        """
        Deepest bid / deepest ask midpoint — a more stable fair-value proxy
        than best bid/ask, which can be distorted by aggressive takers.
        Returns None if either side of the book is empty.
        """
        if not self.od.buy_orders or not self.od.sell_orders:
            return None
        deep_bid: int = min(self.od.buy_orders)
        deep_ask: int = max(self.od.sell_orders)
        return (deep_bid + deep_ask) // 2

    # ── order helpers ─────────────────────────────────────────────────────────

    def _clip(self, qty: int, side: str) -> int:
        """Cap qty within remaining position room."""
        room: int = (LIMIT - self.pos) if side == "buy" else (LIMIT + self.pos)
        return max(0, min(qty, room))

    def buy(self, price: int, qty: int) -> None:
        clipped: int = self._clip(qty, "buy")
        if clipped > 0:
            self.orders.append(Order(self.symbol, price, clipped))
            self.pos += clipped

    def sell(self, price: int, qty: int) -> None:
        clipped: int = self._clip(qty, "sell")
        if clipped > 0:
            self.orders.append(Order(self.symbol, price, -clipped))
            self.pos -= clipped

    # ── end-of-day dump ───────────────────────────────────────────────────────

    def dump(self) -> None:
        """
        Flatten the full position at best available price.
        Longs are sold at best bid; shorts are covered at best ask.
        Uses the position at the START of this tick (before any intraday orders)
        so we don't accidentally double-count.
        """
        initial_pos: int = self.state.position.get(self.symbol, 0)

        if initial_pos > 0:
            bid: Optional[int] = self.best_bid()
            if bid is not None:
                self.sell(bid, initial_pos)

        elif initial_pos < 0:
            ask: Optional[int] = self.best_ask()
            if ask is not None:
                self.buy(ask, -initial_pos)

    # ── subclass interface ────────────────────────────────────────────────────

    def trade(self) -> None:
        raise NotImplementedError

    def get_orders(self) -> List[Order]:
        return self.orders


# ── IPR trader ────────────────────────────────────────────────────────────────

class IPRTrader(ProductTrader):
    """
    Directional long on INTARIAN_PEPPER_ROOT.
    Sweeps all ask levels to reach the position limit as fast as possible.
    Posts a passive overbid for any remaining room.
    Dumps full position at end of day.
    """

    def trade(self) -> None:
        if self.state.timestamp >= DUMP_TS:
            self.dump()
            return

        if self.pos >= LIMIT:
            return  # already maxed — never sell mid-day

        # Sweep every ask level in the book (level 1 and level 2 where present).
        # Every tick below 80 units costs ~0.001677 × missed_units in foregone trend.
        for price in sorted(self.od.sell_orders.keys()):
            vol: int = -self.od.sell_orders[price]
            self.buy(price, vol)
            if self.pos >= LIMIT:
                return

        # Still below limit: overbid best market bid by 1 tick to queue-jump
        # and get filled on the next incoming seller.
        bid: Optional[int] = self.best_bid()
        if bid is not None:
            self.buy(bid + 1, LIMIT - self.pos)


# ── ACO trader ────────────────────────────────────────────────────────────────

class ACOTrader(ProductTrader):
    """
    Passive market maker on ASH_COATED_OSMIUM.

    Fair value: wall_mid (deepest bid / ask midpoint), falling back to
    ACO_FAIR_STATIC if the book is one-sided. This adapts to intraday drift
    rather than assuming 10,000 forever.

    Quoting: 1 tick inside market best bid/ask, floored at ±EDGE from fair.
    EDGE=2 keeps us competitive in Round 2's tighter bid-side market structure
    (bids cluster at fair-1 to fair-6, avg -4).

    Aggressive takes: fire when market quote is inside TAKE_THR of fair.

    Inventory skew: both quotes shift by SKEW_MAJOR/SKEW_MINOR ticks when
    position is extreme, nudging us back toward flat.

    End-of-day: flatten full position.
    """

    def _fair(self, mkt_bid: Optional[int], mkt_ask: Optional[int]) -> int:
        """
        Dynamic fair value: wall_mid if available, else static fallback.
        Wall mid uses the DEEPEST quotes on each side (most stable price).
        """
        wm: Optional[int] = self.wall_mid()
        return wm if wm is not None else ACO_FAIR_STATIC

    def trade(self) -> None:
        if self.state.timestamp >= DUMP_TS:
            self.dump()
            return

        mkt_bid: Optional[int] = self.best_bid()
        mkt_ask: Optional[int] = self.best_ask()
        fair: int = self._fair(mkt_bid, mkt_ask)

        self._aggressive_takes(mkt_bid, mkt_ask, fair)
        self._passive_quotes(mkt_bid, mkt_ask, fair)

    def _aggressive_takes(
        self,
        mkt_bid: Optional[int],
        mkt_ask: Optional[int],
        fair: int,
    ) -> None:
        """Buy cheap asks / sell expensive bids when clearly mispriced vs fair."""
        if mkt_ask is not None and mkt_ask < fair - TAKE_THR:
            vol: int = -self.od.sell_orders[mkt_ask]
            self.buy(mkt_ask, vol)

        if mkt_bid is not None and mkt_bid > fair + TAKE_THR:
            vol = self.od.buy_orders[mkt_bid]
            self.sell(mkt_bid, vol)

    def _passive_quotes(
        self,
        mkt_bid: Optional[int],
        mkt_ask: Optional[int],
        fair: int,
    ) -> None:
        """
        Post resting bid and ask 1 tick inside market makers, floored at ±EDGE.
        EDGE=2: in Round 2 bid side averages fair-4, so bid+1 = fair-3,
        min(fair-3, fair-2) = fair-3 → we ARE competitive (1 inside market).
        """
        our_bid: int = (
            min(mkt_bid + 1, fair - EDGE) if mkt_bid is not None
            else fair - EDGE * 2
        )
        our_ask: int = (
            max(mkt_ask - 1, fair + EDGE) if mkt_ask is not None
            else fair + EDGE * 2
        )

        skew: int = self._inventory_skew()
        our_bid += skew
        our_ask += skew

        if our_bid >= our_ask:
            our_ask = our_bid + 1

        self.buy(our_bid, LIMIT)
        self.sell(our_ask, LIMIT)

    def _inventory_skew(self) -> int:
        """
        Shift both quotes toward flat when position is extreme.
        Negative skew (push quotes down) when long; positive when short.
        """
        pos: int = self.state.position.get(self.symbol, 0)
        if pos > 50:
            return -SKEW_MAJOR
        if pos > 25:
            return -SKEW_MINOR
        if pos < -50:
            return SKEW_MAJOR
        if pos < -25:
            return SKEW_MINOR
        return 0


# ── Main Trader ───────────────────────────────────────────────────────────────

class Trader:

    def bid(self) -> int:
        """
        Market Access Fee auction.
        Top 50% of bids win 25% extra quote volume; losers pay nothing.
        With corrected EDGE=2, extra volume is worth ~550k Xirecs over 3 days.
        Bid 5,000 is positive EV and comfortably above the expected median.
        """
        return 5_000

    def run(self, state: TradingState) -> tuple[Dict[str, List[Order]], int, str]:
        result: Dict[str, List[Order]] = {}

        traders: Dict[str, ProductTrader] = {}

        if IPR in state.order_depths:
            traders[IPR] = IPRTrader(IPR, state)

        if ACO in state.order_depths:
            traders[ACO] = ACOTrader(ACO, state)

        for symbol, trader in traders.items():
            trader.trade()
            result[symbol] = trader.get_orders()

        return result, 0, ""