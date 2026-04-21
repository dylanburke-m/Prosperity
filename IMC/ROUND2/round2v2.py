"""
IMC Prosperity 4 - Round 2 Algorithm (v3)

=== CHANGES FROM v2 (based on live log 347041 + summary.txt analysis) ===

Flags from summary.txt that drove these fixes:
  • Inventory Blowup: true  — position hit 121 (limit 80), ACO drifted long
  • Adverse Selection: true — fills skewed against us while already long
  • Spread Captured: -77    — losing money per fill due to one-sided accumulation
  • Peak Long: 121          — confirms ACO went 41 units over limit

Root cause: the ACO book is structurally asymmetric.
  Bids average fair-4 (9996); asks average fair+12 (10012) — a 3:1 ratio.
  EDGE=2 made us competitive on both sides, so we got filled when sellers hit
  our bid far more often than when buyers hit our ask → slow drift long.
  When pos exceeded 50, SKEW_MAJOR=5 pushed our bid only to fair-7 (≈9997),
  still inside or at the market bid → barely slowed accumulation.
  SKEW_MINOR=1 had virtually no effect.

Fix 1: ASYMMETRIC skew — only shift the side that needs to cool down.
  Previously: both bid AND ask shifted by skew (ask side collateral damage).
  Now: when long, only the bid shifts down (ask stays neutral for recovery).
       when short, only the ask shifts up (bid stays neutral for recovery).
  This stops over-accumulation without tightening the recovery-side quote.

Fix 2: Three-tier skew with larger ticks calibrated to the actual book.
  Book bid1 ranges from fair-11 to fair+1.
  To fully stop buying, need skew > 13 ticks (push bid below all market bids).
  SKEW_CRITICAL (|pos| > 70): 15 ticks → bid at fair-17, below all market bids
  SKEW_MAJOR    (|pos| > 50): 10 ticks → bid at fair-12, at/below book floor
  SKEW_MINOR    (|pos| > 25):  4 ticks → bid at fair-7,  slightly behind market

=== STRATEGY SUMMARY ===

INTARIAN_PEPPER_ROOT (IPR): unchanged — sweep all asks, post passive overbid.
  Slope 0.001677/ts; stay maxed at 80 all day; dump at end of day.

ASH_COATED_OSMIUM (ACO):
  EDGE=2 with dynamic wall_mid fair — keeps us competitive on the bid side.
  Asymmetric inventory skew — prevents position blowup without hurting recovery.
  Aggressive takes at ±TAKE_THR from fair — captures rare mispricings.
  End-of-day dump at ts >= DUMP_TS.

=== MAF BID ===
  bid() returns 5,000 Xirecs — positive EV, above expected median.
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
TAKE_THR: int        = 3        # ticks inside fair for aggressive takes
# Asymmetric inventory skew — only the accumulating side is shifted.
# Calibrated to book bid1 range of fair-11 to fair+1:
SKEW_CRITICAL: int   = 15       # |pos| > 70: push adverse quote below entire book
SKEW_MAJOR: int      = 10       # |pos| > 50: push adverse quote to book floor
SKEW_MINOR: int      = 4        # |pos| > 25: push adverse quote slightly behind
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

        EDGE=2: bid lands at min(bid1+1, fair-2) ≈ fair-3, competitive vs avg bid1=fair-4.

        Asymmetric inventory skew: only the side that is accumulating shifts.
          Long  → bid shifts DOWN  (stop buying),  ask stays neutral (keep selling to recover)
          Short → ask shifts UP    (stop selling),  bid stays neutral (keep buying to recover)
        This prevents the ask collapsing toward fair when we're already long,
        which was causing adverse-selection losses in v2.
        """
        our_bid: int = (
            min(mkt_bid + 1, fair - EDGE) if mkt_bid is not None
            else fair - EDGE * 2
        )
        our_ask: int = (
            max(mkt_ask - 1, fair + EDGE) if mkt_ask is not None
            else fair + EDGE * 2
        )

        bid_skew, ask_skew = self._inventory_skew()
        our_bid += bid_skew
        our_ask += ask_skew

        if our_bid >= our_ask:
            our_ask = our_bid + 1

        self.buy(our_bid, LIMIT)
        self.sell(our_ask, LIMIT)

    def _inventory_skew(self) -> tuple[int, int]:
        """
        Return (bid_skew, ask_skew) to apply independently to each side.

        When long: push bid DOWN aggressively to stop buying; leave ask alone.
        When short: push ask UP aggressively to stop selling; leave bid alone.

        Three tiers calibrated to the observed bid1 range of fair-11 to fair+1:
          SKEW_CRITICAL (|pos|>70): 15 ticks — adverse quote drops below entire book
          SKEW_MAJOR    (|pos|>50): 10 ticks — adverse quote at book floor
          SKEW_MINOR    (|pos|>25):  4 ticks — adverse quote slightly behind market
        """
        pos: int = self.state.position.get(self.symbol, 0)
        if pos > 70:
            return (-SKEW_CRITICAL, 0)   # very long: kill bid, keep ask
        if pos > 50:
            return (-SKEW_MAJOR, 0)      # long: suppress bid, keep ask
        if pos > 25:
            return (-SKEW_MINOR, 0)      # slightly long: nudge bid down
        if pos < -70:
            return (0, SKEW_CRITICAL)    # very short: kill ask, keep bid
        if pos < -50:
            return (0, SKEW_MAJOR)       # short: suppress ask, keep bid
        if pos < -25:
            return (0, SKEW_MINOR)       # slightly short: nudge ask up
        return (0, 0)


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