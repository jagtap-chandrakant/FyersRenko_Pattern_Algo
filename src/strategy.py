# src/strategy.py
"""
Renko Pattern-Based Strategy Module

Implements pattern-based trading strategy with:
- INCREMENTAL Renko brick construction (no rebuild)
- Adaptive brick size (percentage-based, recalculated per brick)
- 8 pattern detection (4 bullish + 4 bearish)
- 2-brick trailing stop exit

Author: Chandrakant Jagtap
Version: 7.1.0 (Edge-Based Brick Sizing - FIXED)
"""

import logging
import math
from collections import deque
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Any

from src.config import PATTERNS, STRATEGY, FILTERS

# =============================================================================
# RENKO INDICATOR CALCULATOR
# =============================================================================


class RenkoIndicators:
    """
    Calculates RSI and MA on Renko brick closes incrementally.
    Matches backtest: Wilder's smoothing RSI, Simple MA.
    """

    def __init__(self, rsi_period: int = 14, ma_period: int = 40) -> None:
        self._rsi_period = rsi_period
        self._ma_period = ma_period

        # RSI state
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._rsi_count: int = 0
        self._initial_gains: List[float] = []
        self._initial_losses: List[float] = []
        self._last_close: Optional[float] = None

        # MA state
        self._ma_closes: deque = deque(maxlen=ma_period)

        # Current values
        self._current_rsi: float = float("nan")
        self._current_ma: float = float("nan")
        self._current_disparity: float = float("nan")

        logging.info(
            "RenkoIndicators initialized: RSI(%d), MA(%d)", rsi_period, ma_period
        )

    def on_new_brick(self, close_price: float) -> None:
        """Update indicators with new brick close price."""
        # --- MA ---
        self._ma_closes.append(close_price)
        if len(self._ma_closes) >= self._ma_period:
            self._current_ma = sum(self._ma_closes) / len(self._ma_closes)
            if self._current_ma > 0:
                self._current_disparity = (
                    (close_price - self._current_ma) / self._current_ma * 100
                )
            else:
                self._current_disparity = float("nan")
        else:
            self._current_ma = float("nan")
            self._current_disparity = float("nan")

        # --- RSI ---
        if self._last_close is not None:
            change = close_price - self._last_close
            gain = max(0.0, change)
            loss = max(0.0, -change)

            self._rsi_count += 1

            if self._rsi_count <= self._rsi_period:
                self._initial_gains.append(gain)
                self._initial_losses.append(loss)

                if self._rsi_count == self._rsi_period:
                    self._avg_gain = sum(self._initial_gains) / self._rsi_period
                    self._avg_loss = sum(self._initial_losses) / self._rsi_period
                    self._current_rsi = self._calc_rsi()
                    self._initial_gains.clear()
                    self._initial_losses.clear()
            else:
                self._avg_gain = (
                    self._avg_gain * (self._rsi_period - 1) + gain
                ) / self._rsi_period
                self._avg_loss = (
                    self._avg_loss * (self._rsi_period - 1) + loss
                ) / self._rsi_period
                self._current_rsi = self._calc_rsi()

        self._last_close = close_price

    def _calc_rsi(self) -> float:
        if self._avg_gain is None or self._avg_loss is None:
            return float("nan")
        if self._avg_loss == 0:
            return 100.0 if self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 2)

    def initialize_from_bricks(self, brick_closes: List[float]) -> None:
        """Initialize from existing brick closes (called during warmup)."""
        self._avg_gain = None
        self._avg_loss = None
        self._rsi_count = 0
        self._initial_gains.clear()
        self._initial_losses.clear()
        self._last_close = None
        self._ma_closes.clear()
        self._current_rsi = float("nan")
        self._current_ma = float("nan")
        self._current_disparity = float("nan")

        for close_price in brick_closes:
            self.on_new_brick(close_price)

        logging.info(
            "Indicators from %d bricks: RSI=%.2f, MA=%.2f, Disp=%.4f",
            len(brick_closes),
            self._current_rsi if not math.isnan(self._current_rsi) else 0,
            self._current_ma if not math.isnan(self._current_ma) else 0,
            self._current_disparity if not math.isnan(self._current_disparity) else 0,
        )

    @property
    def rsi(self) -> float:
        return self._current_rsi

    @property
    def ma(self) -> float:
        return self._current_ma

    @property
    def disparity(self) -> float:
        return self._current_disparity

    @property
    def is_ready(self) -> bool:
        return not math.isnan(self._current_rsi) and not math.isnan(self._current_ma)


class RenkoStrategy:
    """
    Pattern-based Renko trading strategy with INCREMENTAL brick building.

    Bricks are built once during warmup, then incrementally as new bars arrive.
    Thresholds are derived from the last brick — no full rebuild needed.

    Entry: 8 patterns (Three-Back, Two-Back, One-Back, Zigzag)
    Exit: 2-brick trailing stop from peak/trough

    CRITICAL FIX (v7.1.0):
    - Brick size calculated from EDGE (brick_high for RED, brick_low for GREEN)
    - Edge-to-edge brick connection (no gaps)
    - Matches broker's Renko calculation exactly
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialize strategy with configuration."""
        self.config = config

        # Renko parameters
        self._brick_pct = config["renko_brick_pct"]
        self._exit_bricks = config["exit_trailing_bricks"]
        self._entry_start = dtime.fromisoformat(config["entry_start_time"])

        # Position state
        self.position = 0  # 0=flat, 1=long, -1=short
        self.entry_info: Optional[Dict[str, Any]] = None

        # Trailing stop tracking
        self.peak_price: Optional[float] = None
        self.trough_price: Optional[float] = None

        # Renko state (incremental — NO full rebuild)
        self.renko_bricks: List[Dict[str, Any]] = []
        self._brick_high: float = 0.0
        self._brick_low: float = 0.0
        self._brick_close: float = 0.0
        self._brick_size: float = 0.0
        self._trend: int = 0  # 1=GREEN, -1=RED, 0=initial
        self._consecutive: int = 0
        self._acc_vol: float = 0.0
        self._initialized: bool = False

        # Thresholds (derived from last brick — always up to date)
        self._green_cont_threshold: float = 0.0
        self._red_cont_threshold: float = 0.0
        self._green_reversal_threshold: float = 0.0
        self._red_reversal_threshold: float = 0.0

        # Tracking
        self.last_processed_time: Optional[datetime] = None
        self._new_brick_indices: List[int] = []

        # Pattern logging
        self.detected_patterns: List[Dict[str, Any]] = []

        # v7.4.0: Indicators and Filters
        # These MUST be created here before initialize_from_warmup() is called
        rsi_period = FILTERS.get("f2_rsi_alignment", {}).get("rsi_period", 14)
        ma_period = FILTERS.get("f1_ma_alignment", {}).get("ma_period", 40)
        self.indicators = RenkoIndicators(rsi_period=rsi_period, ma_period=ma_period)
        self.filters = FilterEngine(FILTERS)
        self.filtered_signals: List[Dict[str, Any]] = []

        logging.info(
            "RenkoStrategy v7.4.0: brick=%.4f%%, exit=%d bricks, filters=%s",
            self._brick_pct * 100,
            self._exit_bricks,
            "ENABLED" if FILTERS.get("enabled", True) else "DISABLED",
        )

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def initialize_from_warmup(self, bars: List[Dict[str, Any]]) -> None:
        """
        Build initial Renko bricks from warmup data.
        Called ONCE at startup. After this, use on_1min_bar() for incremental updates.
        """
        if not bars or len(bars) < 2:
            logging.warning("Not enough warmup bars: %d", len(bars) if bars else 0)
            return

        # Reset state
        self.renko_bricks.clear()
        self._new_brick_indices.clear()
        self._initialized = False

        # Initialize from first bar
        first_close = round(bars[0]["close"], 2)
        self._brick_high = first_close
        self._brick_low = first_close
        self._brick_close = first_close
        self._brick_size = round(first_close * self._brick_pct, 2)
        self._trend = 0
        self._consecutive = 0
        self._acc_vol = 0.0

        if self._brick_size <= 0:
            logging.error("Invalid brick size from first price %.2f", first_close)
            return

        # Update thresholds
        self._update_thresholds()

        # Process all warmup bars (skip first — it's the reference point)
        for i in range(1, len(bars)):
            self._process_close(
                round(bars[i]["close"], 2),
                bars[i]["volume"],
                bars[i]["timestamp"],
            )

        self._initialized = True
        self.last_processed_time = bars[-1]["timestamp"]

        # Clear new brick indices from warmup (not "new" for signal purposes)
        self._new_brick_indices.clear()

        # v7.4.0: Initialize indicators from warmup bricks
        if self.renko_bricks:
            brick_closes = [b["close"] for b in self.renko_bricks]
            self.indicators.initialize_from_bricks(brick_closes)

        # Log summary
        if self.renko_bricks:
            green = sum(1 for b in self.renko_bricks if b["type"] == 1)
            red = sum(1 for b in self.renko_bricks if b["type"] == -1)
            logging.info(
                "Warmup complete: %d bricks (%d GREEN, %d RED) | "
                "Size: %.2f → %.2f (adaptive)",
                len(self.renko_bricks),
                green,
                red,
                self.renko_bricks[0]["size"],
                self.renko_bricks[-1]["size"],
            )

            if len(self.renko_bricks) >= 10:
                visual = "".join(
                    "🟢" if b["type"] == 1 else "🔴" for b in self.renko_bricks[-10:]
                )
                logging.info("Last 10 bricks: %s", visual)
        else:
            logging.info("Warmup complete: 0 bricks generated")

    def on_1min_bar(
        self,
        timestamp: Any,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Process new 1-minute bar INCREMENTALLY.
        Does NOT rebuild all bricks. Only checks if new bricks form.
        """
        timestamp = self._ensure_datetime(timestamp)

        # Validate
        if not self._validate_bar(open_price, high, low, close, volume):
            return None

        # Skip duplicates
        if self.last_processed_time and timestamp <= self.last_processed_time:
            return None

        self.last_processed_time = timestamp

        if not self._initialized:
            logging.warning(
                "Strategy not initialized — call initialize_from_warmup() first"
            )
            return None

        # Clear previous bar's new brick tracking
        self._new_brick_indices.clear()

        # Process this close price incrementally
        self._process_close(round(close, 2), volume, timestamp)

        # v7.4.0: Update indicators for any new bricks formed
        for idx in self._new_brick_indices:
            self.indicators.on_new_brick(self.renko_bricks[idx]["close"])

        # Check for signals from new bricks
        if not self._new_brick_indices:
            return None

        return self._check_signals_from_new_bricks()

    def get_current_state(self) -> Optional[Dict[str, Any]]:
        """Get current strategy state for display."""
        if not self.renko_bricks:
            return None

        last_brick = self.renko_bricks[-1]
        last_10 = [b["type"] for b in self.renko_bricks[-10:]]
        last_pattern = self.detected_patterns[-1] if self.detected_patterns else None

        return {
            "nifty_price": last_brick["close"],
            "position": self.position,
            "total_bricks": len(self.renko_bricks),
            "last_10_bricks": last_10,
            "renko_direction": last_brick["type"],
            "last_pattern_name": (
                last_pattern.get("pattern_name") if last_pattern else None
            ),
            "last_pattern_type": (
                last_pattern.get("pattern_type") if last_pattern else None
            ),
            "peak_price": self.peak_price,
            "trough_price": self.trough_price,
            "brick_high": self._brick_high,
            "brick_low": self._brick_low,
            "brick_size": self._brick_size,
            "green_threshold": self._green_cont_threshold,
            "red_threshold": self._red_cont_threshold,
            # v7.4.0
            "renko_rsi": self.indicators.rsi,
            "renko_ma": self.indicators.ma,
            "renko_disparity": self.indicators.disparity,
            "indicators_ready": self.indicators.is_ready,
        }

    def has_new_bricks(self) -> bool:
        """Check if new bricks formed in last bar processing."""
        return len(self._new_brick_indices) > 0

    def get_new_brick_count(self) -> int:
        """Get number of new bricks formed in last bar."""
        return len(self._new_brick_indices)

    def acknowledge_new_bricks(self) -> None:
        """Clear new brick tracking after external processing."""
        self._new_brick_indices.clear()

    def get_filter_stats(self) -> Dict[str, Any]:
        """Get filter statistics for reporting."""
        return self.filters.get_stats()

    def log_filter_stats(self) -> None:
        """Log filter statistics."""
        self.filters.log_stats()

    def reset_daily_stats(self) -> None:
        """Reset daily statistics."""
        self.filters.reset_stats()
        self.filtered_signals.clear()

    # =========================================================================
    # INCREMENTAL RENKO ENGINE (FIXED v7.1.0)
    # =========================================================================

    def _update_thresholds(self) -> None:
        """
        Update continuation/reversal thresholds from current brick state.

        Called after every brick formation. These thresholds are used
        to determine if the next close price forms a new brick.

        Continuation: 1 brick size from edge (same direction)
        Reversal: 2 brick sizes from opposite edge (direction change)

        ✅ FIX v7.1.0: Calculate brick_size from EDGE (not close)
        - GREEN bricks: Use brick_low
        - RED bricks: Use brick_high
        - INITIAL: Use brick_close (no trend yet)
        """
        # ✅ FIX: Edge-based brick size calculation
        if self._trend == 1:  # GREEN trend
            self._brick_size = round(self._brick_low * self._brick_pct, 2)
        elif self._trend == -1:  # RED trend
            self._brick_size = round(self._brick_high * self._brick_pct, 2)
        else:  # INITIAL (no trend)
            self._brick_size = round(self._brick_close * self._brick_pct, 2)

        # Continuation thresholds (1 brick size)
        self._green_cont_threshold = round(self._brick_high + self._brick_size, 2)
        self._red_cont_threshold = round(self._brick_low - self._brick_size, 2)

        # Reversal thresholds (2 brick sizes from opposite edge)
        self._green_reversal_threshold = round(
            self._brick_low - (2 * self._brick_size), 2
        )
        self._red_reversal_threshold = round(
            self._brick_high + (2 * self._brick_size), 2
        )

    def _process_close(
        self, current_price: float, volume: float, timestamp: datetime
    ) -> None:
        """
        Process a single close price against current thresholds.

        This is the CORE incremental engine. It checks if the price
        crosses any threshold and adds bricks one at a time.

        Args:
            current_price: Rounded close price
            volume: Bar volume
            timestamp: Bar timestamp
        """
        self._acc_vol += volume

        # CASE 1: GREEN TREND
        if self._trend == 1:
            if current_price >= self._green_cont_threshold:
                # GREEN continuation (price moved up 1 brick)
                self._add_continuation_bricks(current_price, timestamp, direction=1)
            elif current_price <= self._green_reversal_threshold:
                # GREEN → RED reversal (price dropped 2 bricks)
                self._add_reversal_bricks(current_price, timestamp, new_direction=-1)

        # CASE 2: RED TREND
        elif self._trend == -1:
            if current_price <= self._red_cont_threshold:
                # RED continuation (price moved down 1 brick)
                self._add_continuation_bricks(current_price, timestamp, direction=-1)
            elif current_price >= self._red_reversal_threshold:
                # RED → GREEN reversal (price rose 2 bricks)
                self._add_reversal_bricks(current_price, timestamp, new_direction=1)

        # CASE 3: INITIAL (no trend yet)
        else:
            price_up = current_price - self._brick_high
            price_down = self._brick_low - current_price

            if price_up >= self._brick_size and price_up >= price_down:
                self._add_continuation_bricks(current_price, timestamp, direction=1)
            elif price_down >= self._brick_size:
                self._add_continuation_bricks(current_price, timestamp, direction=-1)

    def _add_continuation_bricks(
        self, current_price: float, timestamp: datetime, direction: int
    ) -> None:
        """
        Add continuation bricks (same direction) one at a time.

        Each brick recalculates its own adaptive size.

        ✅ FIX v7.1.0:
        1. Calculate brick_size from EDGE (not close)
        2. Connect bricks edge-to-edge (no gaps)

        Args:
            current_price: Target price
            timestamp: Brick timestamp
            direction: 1 for GREEN, -1 for RED
        """
        first_brick = True

        while True:
            # ✅ FIX: Calculate brick_size from edge
            if direction == 1:  # GREEN
                brick_size = round(self._brick_low * self._brick_pct, 2)
            else:  # RED
                brick_size = round(self._brick_high * self._brick_pct, 2)

            if brick_size <= 0:
                break

            if direction == 1:  # GREEN
                threshold = round(self._brick_high + brick_size, 2)
                if current_price < threshold:
                    break

                # ✅ FIX: Edge-to-edge connection
                self._brick_low = self._brick_high  # Connect to previous high
                self._brick_high = round(self._brick_low + brick_size, 2)
                self._brick_close = self._brick_high

            else:  # RED
                threshold = round(self._brick_low - brick_size, 2)
                if current_price > threshold:
                    break

                # ✅ FIX: Edge-to-edge connection
                self._brick_high = self._brick_low  # Connect to previous low
                self._brick_low = round(self._brick_high - brick_size, 2)
                self._brick_close = self._brick_low

            self._trend = direction
            self._consecutive += 1

            brick = {
                "time": timestamp,
                "close": self._brick_close,
                "type": direction,
                "size": brick_size,
                "volume": self._acc_vol if first_brick else 0.0,
                "consecutive": self._consecutive,
                "high": self._brick_high,
                "low": self._brick_low,
            }

            self.renko_bricks.append(brick)
            self._new_brick_indices.append(len(self.renko_bricks) - 1)

            if first_brick:
                self._acc_vol = 0.0
                first_brick = False

        # Update thresholds after all bricks added
        self._update_thresholds()

    def _add_reversal_bricks(
        self, current_price: float, timestamp: datetime, new_direction: int
    ) -> None:
        """
        Add reversal bricks (direction change) one at a time.

        ✅ FIX v7.1.0:
        1. Start from opposite edge to ensure proper brick connection
        2. Calculate brick_size from reversal_start (edge)
        3. Apply 2-brick rule: plot (actual_bricks - 1), minimum 1

        Args:
            current_price: Target price
            timestamp: Brick timestamp
            new_direction: 1 for GREEN, -1 for RED
        """
        first_brick = True
        bricks_added = 0

        # Start from opposite edge of previous brick
        if new_direction == 1:
            # GREEN reversal: Start from previous RED brick's LOW
            reversal_start = self._brick_low
            price_movement = current_price - reversal_start
        else:
            # RED reversal: Start from previous GREEN brick's HIGH
            reversal_start = self._brick_high
            price_movement = reversal_start - current_price

        # ✅ FIX: Calculate brick_size from reversal_start (edge)
        brick_size = round(reversal_start * self._brick_pct, 2)
        if brick_size <= 0:
            brick_size = self._brick_size  # Fallback to last known size

        actual_bricks = int(price_movement / brick_size) + 1

        # Apply 2-brick rule: plot (actual_bricks - 1), minimum 1
        bricks_to_plot = max(1, actual_bricks - 1)

        # Start building from reversal_start
        self._brick_close = reversal_start

        for _ in range(bricks_to_plot):
            # ✅ FIX: Recalculate brick_size from current edge (adaptive)
            if new_direction == 1:  # GREEN
                brick_size = round(self._brick_close * self._brick_pct, 2)
            else:  # RED
                brick_size = round(self._brick_close * self._brick_pct, 2)

            if brick_size <= 0:
                break

            if new_direction == 1:
                # GREEN brick: close moves UP
                # ✅ FIX: Edge-to-edge connection
                self._brick_low = self._brick_close
                self._brick_high = round(self._brick_low + brick_size, 2)
                self._brick_close = self._brick_high

            else:
                # RED brick: close moves DOWN
                # ✅ FIX: Edge-to-edge connection
                self._brick_high = self._brick_close
                self._brick_low = round(self._brick_high - brick_size, 2)
                self._brick_close = self._brick_low

            self._trend = new_direction

            if first_brick:
                self._consecutive = 1
                first_brick = False
            else:
                self._consecutive += 1

            bricks_added += 1

            brick = {
                "time": timestamp,
                "close": self._brick_close,
                "type": new_direction,
                "size": brick_size,
                "volume": self._acc_vol if bricks_added == 1 else 0.0,
                "consecutive": self._consecutive,
                "high": self._brick_high,
                "low": self._brick_low,
            }

            self.renko_bricks.append(brick)
            self._new_brick_indices.append(len(self.renko_bricks) - 1)

            # Update thresholds for next brick (adaptive sizing)
            self._update_thresholds()

        # Safety: Ensure at least 1 reversal brick was added
        if bricks_added == 0:
            brick_size = round(self._brick_close * self._brick_pct, 2)
            if brick_size > 0:
                if new_direction == 1:
                    # GREEN brick
                    self._brick_low = self._brick_close
                    self._brick_high = round(self._brick_low + brick_size, 2)
                    self._brick_close = self._brick_high
                else:
                    # RED brick
                    self._brick_high = self._brick_close
                    self._brick_low = round(self._brick_high - brick_size, 2)
                    self._brick_close = self._brick_low

                self._trend = new_direction
                self._consecutive = 1

                brick = {
                    "time": timestamp,
                    "close": self._brick_close,
                    "type": new_direction,
                    "size": brick_size,
                    "volume": self._acc_vol,
                    "consecutive": self._consecutive,
                    "high": self._brick_high,
                    "low": self._brick_low,
                }

                self.renko_bricks.append(brick)
                self._new_brick_indices.append(len(self.renko_bricks) - 1)

        self._acc_vol = 0.0

        # Final threshold update after all reversal bricks added
        self._update_thresholds()

    # =========================================================================
    # SIGNAL DETECTION
    # =========================================================================

    def _check_signals_from_new_bricks(self) -> Optional[Dict[str, Any]]:
        """
        Check all newly formed bricks for entry/exit signals.

        Returns first signal found (exit takes priority over entry).
        """
        for idx in self._new_brick_indices:
            if idx < 1:
                continue

            # Exit check (if position open)
            if self.position != 0:
                exit_signal = self._check_trailing_stop_exit(idx)
                if exit_signal:
                    return exit_signal

            # Entry check (if flat)
            if self.position == 0:
                entry_signal = self._check_pattern_entry(idx)
                if entry_signal:
                    return entry_signal

        return None

    def _check_pattern_entry(self, idx: int) -> Optional[Dict[str, Any]]:
        """Check for pattern-based entry at given brick index (v7.4.0 with filters)."""
        pattern = self._detect_pattern(idx)
        if not pattern:
            return None

        current = self.renko_bricks[idx]
        brick_time = current["time"]
        brick_price = current["close"]

        # Log pattern (before filtering)
        self.detected_patterns.append(
            {
                "pattern_name": pattern["pattern_name"],
                "pattern_type": pattern["pattern_type"],
                "brick_idx": idx,
                "time": brick_time,
                "price": brick_price,
            }
        )

        if STRATEGY.get("log_all_patterns", True):
            rsi_val = self.indicators.rsi
            ma_val = self.indicators.ma
            disp_val = self.indicators.disparity
            logging.info(
                "PATTERN: %s (%s) @ %s | Price: %.2f | Brick: %d | "
                "RSI: %.2f | Disp: %.4f",
                pattern["pattern_name"],
                pattern["pattern_type"],
                brick_time.strftime("%H:%M:%S"),
                brick_price,
                idx,
                rsi_val if not math.isnan(rsi_val) else 0,
                disp_val if not math.isnan(disp_val) else 0,
            )

        # Check entry time
        if brick_time.time() < self._entry_start:
            logging.info("Pattern NOT traded: before entry time")
            return None

        # v7.4.0: Apply filters
        side = pattern["side"]
        entry_hour = brick_time.hour

        should_trade, filter_reason = self.filters.should_take_trade(
            pattern_name=pattern["pattern_name"],
            side=side,
            renko_rsi=self.indicators.rsi,
            disparity=self.indicators.disparity,
            entry_hour=entry_hour,
        )

        if not should_trade:
            self.filtered_signals.append(
                {
                    "pattern_name": pattern["pattern_name"],
                    "pattern_type": pattern["pattern_type"],
                    "side": "LONG" if side == 1 else "SHORT",
                    "time": brick_time,
                    "price": brick_price,
                    "rsi": self.indicators.rsi,
                    "disparity": self.indicators.disparity,
                    "filter_reason": filter_reason,
                }
            )
            return None

        # Signal passed all filters
        logging.info(
            "✅ SIGNAL PASSED: %s %s @ %.2f | RSI=%.2f Disp=%.4f",
            "LONG" if side == 1 else "SHORT",
            pattern["pattern_name"],
            brick_price,
            self.indicators.rsi if not math.isnan(self.indicators.rsi) else 0,
            self.indicators.disparity
            if not math.isnan(self.indicators.disparity)
            else 0,
        )

        return {
            "action": "LONG" if side == 1 else "SHORT",
            "timestamp": brick_time,
            "price": brick_price,
            "pattern_name": pattern["pattern_name"],
            "pattern_type": pattern["pattern_type"],
            "entry_type": "pattern",
            "renko_direction": current["type"],
            "renko_rsi": self.indicators.rsi,
            "renko_ma": self.indicators.ma,
            "renko_disparity": self.indicators.disparity,
        }

    def _detect_pattern(self, brick_idx: int) -> Optional[Dict[str, Any]]:
        """Detect pattern at given brick index."""
        if not PATTERNS["enabled"] or brick_idx < 6:
            return None

        recent_types = [
            self.renko_bricks[i]["type"]
            for i in range(max(0, brick_idx - 5), brick_idx + 1)
        ]

        # Check bullish patterns (priority: longest first)
        for pattern_key in ["three_back", "two_back", "one_back", "zigzag"]:
            pattern_config = PATTERNS["bullish"][pattern_key]
            if not pattern_config["enabled"]:
                continue

            seq = pattern_config["sequence"]
            if len(recent_types) >= len(seq) and recent_types[-len(seq) :] == seq:
                return {
                    "pattern_name": pattern_config["name"],
                    "pattern_type": "bullish",
                    "side": 1,
                }

        # Check bearish patterns
        for pattern_key in ["three_back", "two_back", "one_back", "zigzag"]:
            pattern_config = PATTERNS["bearish"][pattern_key]
            if not pattern_config["enabled"]:
                continue

            seq = pattern_config["sequence"]
            if len(recent_types) >= len(seq) and recent_types[-len(seq) :] == seq:
                return {
                    "pattern_name": pattern_config["name"],
                    "pattern_type": "bearish",
                    "side": -1,
                }

        return None

    def _check_trailing_stop_exit(self, idx: int) -> Optional[Dict[str, Any]]:
        """Check 2-brick trailing stop exit."""
        if not self.entry_info:
            return None

        current = self.renko_bricks[idx]
        brick_size = current["size"]
        current_price = current["close"]

        # Initialize peak/trough from entry price
        if self.peak_price is None:
            self.peak_price = self.entry_info["price"]
        if self.trough_price is None:
            self.trough_price = self.entry_info["price"]

        # Update peak/trough
        self.peak_price = max(self.peak_price, current_price)
        self.trough_price = min(self.trough_price, current_price)

        if self.position == 1:
            stop_price = self.peak_price - (self._exit_bricks * brick_size)
            if current_price <= stop_price:
                logging.info(
                    "LONG EXIT: Price %.2f <= Stop %.2f (Peak: %.2f)",
                    current_price,
                    stop_price,
                    self.peak_price,
                )
                return {
                    "action": "EXIT",
                    "timestamp": current["time"],
                    "price": current_price,
                    "exit_reason": "trailing_stop",
                }

        elif self.position == -1:
            stop_price = self.trough_price + (self._exit_bricks * brick_size)
            if current_price >= stop_price:
                logging.info(
                    "SHORT EXIT: Price %.2f >= Stop %.2f (Trough: %.2f)",
                    current_price,
                    stop_price,
                    self.trough_price,
                )
                return {
                    "action": "EXIT",
                    "timestamp": current["time"],
                    "price": current_price,
                    "exit_reason": "trailing_stop",
                }

        return None

    # =========================================================================
    # UTILITIES
    # =========================================================================

    @staticmethod
    def _validate_bar(
        open_p: float, high: float, low: float, close: float, volume: float
    ) -> bool:
        """Validate bar data integrity."""
        if not (low <= open_p <= high and low <= close <= high and low <= high):
            return False
        if any(p <= 0 for p in [open_p, high, low, close]):
            return False
        if volume < 0:
            return False
        return True

    @staticmethod
    def _ensure_datetime(timestamp: Any) -> datetime:
        """Ensure timestamp is timezone-aware datetime (IST)."""
        from src.config import IST

        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                return IST.localize(timestamp)
            return timestamp.astimezone(IST)

        if hasattr(timestamp, "to_pydatetime"):
            dt = timestamp.to_pydatetime()
            if dt.tzinfo is None:
                return IST.localize(dt)
            return dt.astimezone(IST)

        if isinstance(timestamp, str):
            try:
                if "+" in timestamp or "Z" in timestamp:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    return dt.astimezone(IST)
                else:
                    dt = datetime.fromisoformat(timestamp)
                    return IST.localize(dt)
            except ValueError:
                pass

        return datetime.now(IST)

    def debug_renko_thresholds(self) -> None:
        """Print current Renko state, thresholds, and indicators."""
        if not self.renko_bricks:
            print("❌ No bricks available")
            return

        trend_str = (
            "🟢 GREEN"
            if self._trend == 1
            else "🔴 RED"
            if self._trend == -1
            else "⚪ INITIAL"
        )

        print(f"\n{'=' * 80}")
        print("RENKO STATE (v7.4.0)")
        print(f"{'=' * 80}")
        print(f"  Trend:       {trend_str}")
        print(f"  Brick Close: {self._brick_close:,.2f}")
        print(f"  Brick High:  {self._brick_high:,.2f}")
        print(f"  Brick Low:   {self._brick_low:,.2f}")
        print(f"  Brick Size:  {self._brick_size:.2f} ({self._brick_pct * 100:.4f}%)")
        print(f"  Consecutive: {self._consecutive}")
        print(f"  Total Bricks: {len(self.renko_bricks)}")
        print(f"  Acc Volume:  {self._acc_vol:.0f}")
        print()
        print("THRESHOLDS:")
        print(f"  🟢 GREEN (continuation): >= {self._green_cont_threshold:,.2f}")
        print(f"  🔴 RED   (continuation): <= {self._red_cont_threshold:,.2f}")

        if self._trend == 1:
            print(f"  Continue GREEN if price >= {self._green_cont_threshold:,.2f}")
            print(f"  Reverse to RED if price <= {self._green_reversal_threshold:,.2f}")
        elif self._trend == -1:
            print(f"  Continue RED if price <= {self._red_cont_threshold:,.2f}")
            print(f"  Reverse to GREEN if price >= {self._red_reversal_threshold:,.2f}")

        # v7.4.0: Indicators
        rsi_val = self.indicators.rsi
        ma_val = self.indicators.ma
        disp_val = self.indicators.disparity

        print()
        print("INDICATORS:")
        print(f"  RSI(14): {rsi_val:.2f if not math.isnan(rsi_val) else 'N/A'}")
        print(f"  MA(40):  {ma_val:,.2f if not math.isnan(ma_val) else 'N/A'}")
        print(
            f"  Disp:    {disp_val:.4f}%"
            if not math.isnan(disp_val)
            else "  Disp:    N/A"
        )
        print(f"  Ready:   {'YES' if self.indicators.is_ready else 'NO (warming up)'}")

        # v7.4.0: Filter stats
        stats = self.filters.get_stats()
        if stats["total_signals"] > 0:
            print()
            print("FILTERS:")
            print(
                f"  Signals: {stats['total_signals']} | "
                f"Passed: {stats['passed']} | "
                f"Filtered: {stats['total_filtered']}"
            )
            print(
                f"  F1:{stats['filtered_f1']} "
                f"F2:{stats['filtered_f2']} "
                f"F3:{stats['filtered_f3']} "
                f"F4:{stats['filtered_f4']}"
            )

        print(f"{'=' * 80}\n")


# =============================================================================
# FILTER ENGINE (v7.4.0)
# =============================================================================


class FilterEngine:
    """
    Entry filters: F1 MA Alignment, F2 RSI Alignment,
    F3 No Zigzag Short, F4 No Short Hour 15.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._enabled = config.get("enabled", True)
        self._f1 = config.get("f1_ma_alignment", {})
        self._f2 = config.get("f2_rsi_alignment", {})
        self._f3 = config.get("f3_no_zigzag_short", {})
        self._f4 = config.get("f4_no_short_hour15", {})
        self._log_filtered = config.get("log_filtered_trades", True)

        self._stats = {
            "total_signals": 0,
            "passed": 0,
            "filtered_f1": 0,
            "filtered_f2": 0,
            "filtered_f3": 0,
            "filtered_f4": 0,
        }

        active = []
        if self._f1.get("enabled", True):
            active.append("F1:MA")
        if self._f2.get("enabled", True):
            active.append("F2:RSI")
        if self._f3.get("enabled", True):
            active.append("F3:NoZigShort")
        if self._f4.get("enabled", True):
            active.append("F4:NoH15Short")

        logging.info(
            "FilterEngine v7.4.0: %s | [%s]",
            "ENABLED" if self._enabled else "DISABLED",
            ", ".join(active) if active else "NONE",
        )

    def should_take_trade(
        self,
        pattern_name: str,
        side: int,
        renko_rsi: float,
        disparity: float,
        entry_hour: int,
    ) -> tuple:
        """
        Check all filters. Returns (should_trade, filter_reason).

        Args:
            pattern_name: e.g. "One-Back", "Zigzag"
            side: 1=LONG, -1=SHORT
            renko_rsi: Renko RSI (NaN if unavailable)
            disparity: (price-MA)/MA*100 (NaN if unavailable)
            entry_hour: 0-23
        """
        self._stats["total_signals"] += 1

        if not self._enabled:
            self._stats["passed"] += 1
            return True, ""

        # F1: MA Alignment
        if self._f1.get("enabled", True) and not math.isnan(disparity):
            if side == 1 and disparity < 0:
                self._stats["filtered_f1"] += 1
                reason = f"F1_MA: Long below MA (disp={disparity:.4f})"
                if self._log_filtered:
                    logging.info("🚫 FILTERED: %s | %s", pattern_name, reason)
                return False, reason
            if side == -1 and disparity > 0:
                self._stats["filtered_f1"] += 1
                reason = f"F1_MA: Short above MA (disp={disparity:.4f})"
                if self._log_filtered:
                    logging.info("🚫 FILTERED: %s | %s", pattern_name, reason)
                return False, reason

        # F2: RSI Alignment
        if self._f2.get("enabled", True) and not math.isnan(renko_rsi):
            threshold = self._f2.get("rsi_threshold", 50.0)
            if side == 1 and renko_rsi < threshold:
                self._stats["filtered_f2"] += 1
                reason = f"F2_RSI: Long RSI={renko_rsi:.2f} < {threshold}"
                if self._log_filtered:
                    logging.info("🚫 FILTERED: %s | %s", pattern_name, reason)
                return False, reason
            if side == -1 and renko_rsi > threshold:
                self._stats["filtered_f2"] += 1
                reason = f"F2_RSI: Short RSI={renko_rsi:.2f} > {threshold}"
                if self._log_filtered:
                    logging.info("🚫 FILTERED: %s | %s", pattern_name, reason)
                return False, reason

        # F3: No Zigzag Short
        if self._f3.get("enabled", True):
            if pattern_name == "Zigzag" and side == -1:
                self._stats["filtered_f3"] += 1
                reason = "F3_ZS: Zigzag Short skipped"
                if self._log_filtered:
                    logging.info("🚫 FILTERED: %s | %s", pattern_name, reason)
                return False, reason

        # F4: No Short at Hour 15
        if self._f4.get("enabled", True):
            hour_limit = self._f4.get("hour_threshold", 15)
            if side == -1 and entry_hour >= hour_limit:
                self._stats["filtered_f4"] += 1
                reason = f"F4_H15S: Short at hour {entry_hour}"
                if self._log_filtered:
                    logging.info("🚫 FILTERED: %s | %s", pattern_name, reason)
                return False, reason

        self._stats["passed"] += 1
        return True, ""

    def get_stats(self) -> Dict[str, Any]:
        total = self._stats["total_signals"]
        passed = self._stats["passed"]
        filtered = total - passed
        return {
            **self._stats,
            "total_filtered": filtered,
            "pass_rate": (passed / total * 100) if total > 0 else 0,
            "filter_rate": (filtered / total * 100) if total > 0 else 0,
        }

    def log_stats(self) -> None:
        stats = self.get_stats()
        if stats["total_signals"] == 0:
            return
        logging.info(
            "📊 FILTER STATS: %d signals | %d passed (%.1f%%) | "
            "%d filtered (%.1f%%) | F1:%d F2:%d F3:%d F4:%d",
            stats["total_signals"],
            stats["passed"],
            stats["pass_rate"],
            stats["total_filtered"],
            stats["filter_rate"],
            stats["filtered_f1"],
            stats["filtered_f2"],
            stats["filtered_f3"],
            stats["filtered_f4"],
        )

    def reset_stats(self) -> None:
        for key in self._stats:
            self._stats[key] = 0


__all__ = ["RenkoStrategy", "FilterEngine", "RenkoIndicators"]
