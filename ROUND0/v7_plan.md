# IMC Prosperity 4 — v7 Implementation Plan

You are implementing the next version of a trading bot for the IMC Prosperity 4 competition tutorial round. The tutorial round trades two products: EMERALDS (static fair value) and TOMATOES (moving fair value). The current code scores 2,756 PnL. The leaderboard #1 scores 5,235. The entire gap is in TOMATOES. Emeralds is already at ceiling — do not change the Emerald strategy.

## Current code summary

The current code (`p4t3db.py`, also named `17736.py`) uses a `WallMidMarketMaker` base class for both products. It does three things per tick: (1) TAKING — sweep the order book for positive-edge fills relative to wall_mid, (2) MAKING — overbid the best bid below wall_mid by 1 tick, underask the best ask above wall_mid by 1 tick, (3) INVENTORY LEAN — when `|position| > lean_frac * pos_limit`, tighten the flattening-side quote toward wall_mid. The lean_frac is 0.8 for both products. Position limits are 80 for both. The hard safety check `bid_quote = min(int(bid_quote), int(wm) - 1)` and `ask_quote = max(int(ask_quote), int(wm) + 1)` ensures positive edge.

## The signal: L2 wall volume imbalance

Analysis of 40,000 price bars across 2 days found a predictive signal in the Tomato order book. The designated market makers who set the L2 bid/ask walls (the outermost quotes, `bid_price_2` and `ask_price_2`) usually quote **equal volume** on both sides. But ~7.2% of the time, the volumes diverge, and this divergence predicts future price direction with high reliability.

### Signal definition

```
l2_imb = bid_volume_2 - ask_volume_2
```

### Signal statistics (consistent across both days)

| Condition | Frequency | Avg fwd 10-tick return | Interpretation |
|---|---|---|---|
| `l2_imb == 0` | 92.8% of ticks | ~0.00 | No signal. Pure MM. |
| `l2_imb > 0` | ~3.5% of ticks | **+0.55 ticks** | Price going UP. Bid wall has more volume. |
| `l2_imb < 0` | ~3.5% of ticks | **−0.70 ticks** | Price going DOWN. Ask wall has more volume. |

When the signal is active, the average absolute L2 imbalance is ~12.5 units. The signal persists for an average of 7 consecutive ticks before returning to zero. The correlation between `l2_imb` and `fwd_ret_10` is ~0.13 (both days). The spread between positive-imbalance and negative-imbalance forward returns is 1.2 ticks over 10 steps.

The theoretical PnL from holding 1 unit in the signal direction is ~440 ticks/day. At 50 units of position, this is ~22,000 SeaShells/day. We cannot capture all of this through passive MM, but even 5-10% capture adds 1,100-2,200 daily Tomato PnL.

### Why this signal exists

The L2 market makers know the true floating-point price. When the true price is closer to one wall than the other, they quote more volume on the nearer side (less adverse selection risk there). This asymmetry leaks their private price estimate into the order book. Prosperity 3 data confirmed a similar microstructure: the wall_mid was the best estimate of the true price, and the walls were placed symmetrically by informed market makers.

### Other signals investigated and rejected

- **Taker flow** (cumulative buy vs sell trade volume): Correlation with forward returns is <0.02. Not predictive.
- **L1 imbalance** (`bid_volume_1 - ask_volume_1`): Correlation ~0.07. Weakly predictive but dominated by L2 signal.
- **L1 spread width**: No predictive power.
- **L3 presence**: Weak negative correlation (~-0.10 with fwd_ret_5). Signals recent volatility, not direction. Not worth adding complexity.
- **Mean reversion (lag-1 autocorrelation = -0.21)**: Too weak per-tick to trade actively. The MM already captures this passively.

## What to change (Tomatoes only)

### Change 1: Add L2 imbalance tracking to traderData

Store the current L2 imbalance in `new_td` so it persists across ticks. Also store `last_wm` for return calculation.

```python
l2_imb = 0
if self.bid_wall is not None and self.ask_wall is not None:
    bid_vol_2 = self.bids.get(self.bid_wall, 0)
    ask_vol_2 = self.asks.get(self.ask_wall, 0)
    l2_imb = bid_vol_2 - ask_vol_2
```

### Change 2: Quote skewing when signal is active

When `l2_imb != 0`, shift both the bid and ask quotes in the predicted direction. This biases fill flow toward building a position aligned with the signal, without crossing the spread.

**When `l2_imb > 0` (expect price UP):**
- `bid_quote += SKEW_TICKS` — tighter bid, more buy fills
- `ask_quote += SKEW_TICKS` — wider ask, fewer sell fills but better price when selling

**When `l2_imb < 0` (expect price DOWN):**
- `bid_quote -= SKEW_TICKS` — wider bid, fewer buy fills
- `ask_quote -= SKEW_TICKS` — tighter ask, more sell fills

**When `l2_imb == 0`:**
- No skew. Standard MM quotes.

The skewing must happen AFTER the overbid/underask logic but BEFORE the safety check. The safety check (`bid_quote <= int(wm) - 1`, `ask_quote >= int(wm) + 1`) remains unchanged and will clamp any overly aggressive skewed quotes.

### Change 3: Tune SKEW_TICKS

Start with `SKEW_TICKS = 2`. This shifts quotes by 2 ticks in the signal direction. The safety check prevents crossing wall_mid, so the worst case is quoting at `wm - 1` (bid) or `wm + 1` (ask), which still has positive edge.

If the website backtester shows improvement, test `SKEW_TICKS = 1` and `SKEW_TICKS = 3` to find the optimum. Do not grid-search locally — the local backtester does not correctly model fill probabilities for passive orders.

### Change 4 (optional, higher risk): Active position building on strong signal

When `abs(l2_imb) > 10` (strong signal) AND `abs(position) < 0.6 * pos_limit` (room to build), consider adding aggressive taking in the signal direction:

- If `l2_imb > 10` and `position < 48`: buy at best_ask (take the ask)
- If `l2_imb < -10` and `position > -48`: sell at best_bid (take the bid)

This costs the half-spread (~6 ticks) per trade but captures the expected 0.55-0.70 tick move per step over the signal's ~7-tick duration. Net expected PnL per aggressive take: `7 * 0.6 - 6 = -1.8 ticks` — slightly negative in expectation for a single trade, but it builds the position that earns from the multi-tick trend. Only implement this if passive skewing alone does not close the PnL gap.

## What NOT to change

- **Emerald strategy**: Already at ceiling. Do not modify EmeraldTrader or its LEAN_FRAC.
- **Wall_mid calculation**: `(bid_wall + ask_wall) / 2` is correct. Do not add EMAs, rolling averages, or other fair value estimators.
- **Overbid/underask logic**: The core making logic is correct and matches the P3 runner-up's proven approach.
- **Taking logic**: Sweeping favorable L1 quotes is correct.
- **Safety check**: The `bid_quote <= int(wm) - 1` and `ask_quote >= int(wm) + 1` check must remain. It prevents crossing wall_mid.
- **Position limits**: Keep at 80 for both products.
- **LEAN_FRAC for both products**: Keep at 0.8. This was the value that yielded 2,756.

## Implementation structure

The cleanest implementation is to override `get_orders()` in `TomatoTrader` instead of using the shared `WallMidMarketMaker` base. This keeps Emeralds completely untouched.

```python
class TomatoTrader(WallMidMarketMaker):
    LEAN_FRAC = TOMATO_LEAN_FRAC
    SKEW_TICKS = 2

    def get_orders(self):
        if self.wall_mid is None:
            return self.orders

        wm = self.wall_mid

        # ① TAKING — identical to base class
        # [copy the taking logic from WallMidMarketMaker unchanged]

        # ② MAKING — identical to base class  
        # [copy the overbid/underask logic from WallMidMarketMaker unchanged]

        # ③ L2 IMBALANCE SKEW — NEW
        bid_vol_2 = self.bids.get(self.bid_wall, 0)
        ask_vol_2 = self.asks.get(self.ask_wall, 0)
        l2_imb = bid_vol_2 - ask_vol_2

        if l2_imb > 0:
            bid_quote += self.SKEW_TICKS
            ask_quote += self.SKEW_TICKS
        elif l2_imb < 0:
            bid_quote -= self.SKEW_TICKS
            ask_quote -= self.SKEW_TICKS

        # ④ INVENTORY LEAN — identical to base class
        # [copy the lean logic from WallMidMarketMaker unchanged]

        # ⑤ SAFETY CHECK — identical to base class
        bid_quote = min(int(bid_quote), int(wm) - 1)
        ask_quote = max(int(ask_quote), int(wm) + 1)

        # Post passive quotes
        self._buy(bid_quote, self.buy_cap)
        self._sell(ask_quote, self.sell_cap)

        return self.orders
```

The order of operations matters: TAKING → MAKING (overbid/underask) → L2 SKEW → INVENTORY LEAN → SAFETY CHECK. The skew must come after making (so it shifts the already-optimized quote) and before lean (so lean can override the skew when position is extreme). The safety check is always last.

## Risk analysis

The leaderboard top 5 all have max drawdowns of 687-720. This implies they hold ~60-85 units of directional exposure through ~8-10 tick adverse moves. Our quote skewing approach builds directional position passively through biased fills, which means position accumulates gradually (not all at once). The max position from skewing alone is bounded by the fill rate — at ~4% taker frequency and ~3.5 avg quantity, we fill roughly 14 units per 100 ticks. Over the signal's 7-tick average duration, that's ~1 fill of ~3.5 units per signal event. This is conservative. The drawdown from being wrong on a signal is approximately `3.5 units * 1.2 tick adverse move = 4.2 SeaShells per bad signal`. With ~350 signal events per day, even if 40% are wrong, the loss is `140 * 4.2 = 588 SeaShells`, well within budget.

## Testing protocol

1. Implement the code change as described above with SKEW_TICKS = 2.
2. Submit to the Prosperity website backtester. Record total PnL, per-product PnL, max drawdown, and final positions.
3. If PnL improves over 2,756, test SKEW_TICKS = 1 and SKEW_TICKS = 3 on the website backtester.
4. If passive skewing alone gets us above ~4,000, stop. If not, implement Change 4 (active taking on strong signal) and test.
5. Do NOT optimize for local backtester results. The README from P3 winners explicitly warns: "never optimize purely for website score" and "the backtester could not accurately model potential fill probabilities after inserting liquidity."

## Summary of the gap

| Component | Current PnL | Target PnL | Source of improvement |
|---|---|---|---|
| Emeralds | ~1,800 | ~1,800 | None needed (at ceiling) |
| Tomatoes (baseline MM) | ~950 | ~950 | Unchanged |
| Tomatoes (L2 skew) | 0 | +1,500 to +2,500 | Quote skewing on L2 imbalance signal |
| **Total** | **~2,750** | **~4,250 to ~5,250** | |

The target range of 4,250-5,250 would place us in the top 5 of the current leaderboard.
