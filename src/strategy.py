# src/strategy.py
"""
Renko Pattern-Based Strategy Module

Implements pattern-based trading strategy with:
- INCREMENTAL Renko brick construction (no rebuild)
- Adaptive brick size (percentage-based, recalculated per brick)
- 8 pattern detection (4 bullish + 4 bearish)
- 2-brick trailing stop exit

Author: Chandrakant Jagtap
Version: 7.0.0 (Incremental Renko)
"""

import logging
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Any

from src.config import PATTERNS, STRATEGY


class RenkoStrategy:
    """
    Pattern-based Renko trading strategy with INCREMENTAL brick building.

    Bricks are built once during warmup, then incrementally as new bars arrive.
    Thresholds are derived from the last brick — no full rebuild needed.

    Entry: 8 patterns (Three-Back, Two-Back, One-Back, Zigzag)
    Exit: 2-brick trailing stop from peak/trough
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

        # Tracking
        self.last_processed_time: Optional[datetime] = None
        self._new_brick_indices: List[int] = []

        # Pattern logging
        self.detected_patterns: List[Dict[str, Any]] = []

        logging.info(
            "RenkoStrategy initialized: brick=%.4f%%, exit=%d bricks",
            self._brick_pct * 100,
            self._exit_bricks,
        )

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def initialize_from_warmup(self, bars: List[Dict[str, Any]]) -> None:
        """
        Build initial Renko bricks from warmup data.

        Called ONCE at startup. After this, use on_1min_bar() for
        incremental updates.

        Args:
            bars: List of 1-minute bar dictionaries from warmup
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

        # Clear new brick indices from warmup (they are not "new" for signal purposes)
        self._new_brick_indices.clear()

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

        Does NOT rebuild all bricks. Only checks if new bricks form
        from the current close price against existing thresholds.

        Args:
            timestamp: Bar timestamp
            open_price: Open price
            high: High price
            low: Low price
            close: Close price
            volume: Volume

        Returns:
            Signal dict if generated, None otherwise
        """
        timestamp = self._ensure_datetime(timestamp)

        # Validate
        if not self._validate_bar(open_price, high, low, close, volume):
            return None

        # Skip duplicates
        if self.last_processed_time and timestamp <= self.last_processed_time:
            return None

        self.last_processed_time = timestamp

        # If not initialized, warn (shouldn't happen after warmup)
        if not self._initialized:
            logging.warning(
                "Strategy not initialized — call initialize_from_warmup() first"
            )
            return None

        # Clear previous bar's new brick tracking
        self._new_brick_indices.clear()

        # Process this close price incrementally
        self._process_close(round(close, 2), volume, timestamp)

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

    # =========================================================================
    # INCREMENTAL RENKO ENGINE
    # =========================================================================

    def _update_thresholds(self) -> None:
        """
        Update continuation/reversal thresholds from current brick state.

        Called after every brick formation. These thresholds are used
        to determine if the next close price forms a new brick.

        GREEN continuation = brick_high + brick_size
        RED continuation   = brick_low - brick_size
        Reversal thresholds are the same (just opposite direction).
        """
        self._brick_size = round(self._brick_close * self._brick_pct, 2)
        self._green_cont_threshold = round(self._brick_high + self._brick_size, 2)
        self._red_cont_threshold = round(self._brick_low - self._brick_size, 2)

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
                self._add_continuation_bricks(current_price, timestamp, direction=1)
            elif current_price <= self._red_cont_threshold:
                self._add_reversal_bricks(current_price, timestamp, new_direction=-1)

        # CASE 2: RED TREND
        elif self._trend == -1:
            if current_price <= self._red_cont_threshold:
                self._add_continuation_bricks(current_price, timestamp, direction=-1)
            elif current_price >= self._green_cont_threshold:
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

        Args:
            current_price: Target price
            timestamp: Brick timestamp
            direction: 1 for GREEN, -1 for RED
        """
        first_brick = True

        while True:
            brick_size = round(self._brick_close * self._brick_pct, 2)
            if brick_size <= 0:
                break

            if direction == 1:
                threshold = round(self._brick_high + brick_size, 2)
                if current_price < threshold:
                    break

                self._brick_close = round(self._brick_close + brick_size, 2)
                self._brick_low = round(self._brick_close - brick_size, 2)
                self._brick_high = round(self._brick_close, 2)
            else:
                threshold = round(self._brick_low - brick_size, 2)
                if current_price > threshold:
                    break

                self._brick_close = round(self._brick_close - brick_size, 2)
                self._brick_high = round(self._brick_close + brick_size, 2)
                self._brick_low = round(self._brick_close, 2)

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

        CRITICAL FIX: Ensure first reversal brick starts from OPPOSITE edge
        to prevent overlapping bricks at same level.

        Args:
            current_price: Target price
            timestamp: Brick timestamp
            new_direction: 1 for GREEN, -1 for RED
        """
        first_brick = True
        bricks_added = 0

        # CRITICAL: Start from the OPPOSITE edge for reversal
        if new_direction == 1:
            # Reversing to GREEN: Start from previous brick's LOW
            self._brick_close = self._brick_low
        else:
            # Reversing to RED: Start from previous brick's HIGH
            self._brick_close = self._brick_high

        while True:
            brick_size = round(self._brick_close * self._brick_pct, 2)
            if brick_size <= 0:
                break

            if new_direction == 1:
                # GREEN brick
                new_close = round(self._brick_close + brick_size, 2)
                threshold = round(self._brick_high + brick_size, 2)

                if current_price < threshold:
                    break

                self._brick_close = new_close
                self._brick_low = round(self._brick_close - brick_size, 2)
                self._brick_high = round(self._brick_close, 2)
            else:
                # RED brick
                new_close = round(self._brick_close - brick_size, 2)
                threshold = round(self._brick_low - brick_size, 2)

                if current_price > threshold:
                    break

                self._brick_close = new_close
                self._brick_high = round(self._brick_close + brick_size, 2)
                self._brick_low = round(self._brick_close, 2)

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

        # Floating-point safety: ensure at least 1 reversal brick
        if bricks_added == 0:
            brick_size = round(self._brick_close * self._brick_pct, 2)
            if brick_size > 0:
                if new_direction == 1:
                    self._brick_close = round(self._brick_close + brick_size, 2)
                    self._brick_low = round(self._brick_close - brick_size, 2)
                    self._brick_high = round(self._brick_close, 2)
                else:
                    self._brick_close = round(self._brick_close - brick_size, 2)
                    self._brick_high = round(self._brick_close + brick_size, 2)
                    self._brick_low = round(self._brick_close, 2)

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
                bricks_added = 1

        self._acc_vol = 0.0

        # Update thresholds after all bricks added
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
        """Check for pattern-based entry at given brick index."""
        pattern = self._detect_pattern(idx)
        if not pattern:
            return None

        current = self.renko_bricks[idx]
        brick_time = current["time"]
        brick_price = current["close"]

        # Log pattern
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
            logging.info(
                "PATTERN: %s (%s) @ %s | Price: %.2f | Brick: %d",
                pattern["pattern_name"],
                pattern["pattern_type"],
                brick_time.strftime("%H:%M:%S"),
                brick_price,
                idx,
            )

        # Check entry time
        if brick_time.time() < self._entry_start:
            logging.info("Pattern NOT traded: before entry time")
            return None

        return {
            "action": "LONG" if pattern["side"] == 1 else "SHORT",
            "timestamp": brick_time,
            "price": brick_price,
            "pattern_name": pattern["pattern_name"],
            "pattern_type": pattern["pattern_type"],
            "entry_type": "pattern",
            "renko_direction": current["type"],
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
        """Print current Renko state and thresholds for debugging."""
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
        print("RENKO STATE (Incremental)")
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
        print(
            f"  🟢 GREEN (continuation/reversal): >= {self._green_cont_threshold:,.2f}"
        )
        print(f"  🔴 RED   (continuation/reversal): <= {self._red_cont_threshold:,.2f}")

        if self._trend == 1:
            print("\n  Current trend is GREEN:")
            print(f"    Continue GREEN if price >= {self._green_cont_threshold:,.2f}")
            print(f"    Reverse to RED if price <= {self._red_cont_threshold:,.2f}")
        elif self._trend == -1:
            print("\n  Current trend is RED:")
            print(f"    Continue RED if price <= {self._red_cont_threshold:,.2f}")
            print(f"    Reverse to GREEN if price >= {self._green_cont_threshold:,.2f}")

        print(f"{'=' * 80}\n")


__all__ = ["RenkoStrategy"]
