# src/export.py
"""
Data Export Module

Exports daily Renko charts and option contract data for analysis.
Includes pattern detection marking for debugging.

Author: Chandrakant Jagtap
Version: 1.3.0 (Added Pattern Detection)
"""

import logging
import csv
from datetime import datetime, date
from pathlib import Path
from typing import List, Dict, Any, Optional, Set

import matplotlib

matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from src.config import get_current_date, IST, PATTERNS


# =============================================================================
# CONFIGURATION
# =============================================================================

EXPORT_DIR = Path("data/exports")
RENKO_CHARTS_DIR = EXPORT_DIR / "renko_charts"
OPTION_DATA_DIR = EXPORT_DIR / "option_data"

# Enable/disable exports
EXPORT_RENKO_CHART = True  # Set to False after 10 days
EXPORT_OPTION_DATA = True


# =============================================================================
# DIRECTORY SETUP
# =============================================================================


def _ensure_directories() -> None:
    """Create export directories if they don't exist."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    RENKO_CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    OPTION_DATA_DIR.mkdir(parents=True, exist_ok=True)


_ensure_directories()


# =============================================================================
# PATTERN DETECTION (For Export)
# =============================================================================


def detect_patterns_for_export(
    renko_bricks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Detect all patterns in brick sequence for export.

    Args:
        renko_bricks: List of Renko brick dictionaries

    Returns:
        List of detected patterns with brick index
    """
    if not PATTERNS.get("enabled"):
        return []

    detected = []

    for idx in range(
        6, len(renko_bricks)
    ):  # Need at least 6 bricks for longest pattern
        # Get recent brick types (last 6 bricks)
        recent_types = [
            renko_bricks[i]["type"] for i in range(max(0, idx - 5), idx + 1)
        ]

        # Check bullish patterns (priority order)
        for pattern_key in ["three_back", "two_back", "one_back", "zigzag"]:
            pattern_config = PATTERNS["bullish"][pattern_key]

            if not pattern_config["enabled"]:
                continue

            seq = pattern_config["sequence"]
            seq_len = len(seq)

            if len(recent_types) >= seq_len:
                if recent_types[-seq_len:] == seq:
                    detected.append(
                        {
                            "brick_idx": idx,
                            "pattern_name": pattern_config["name"],
                            "pattern_type": "bullish",
                            "side": "LONG",
                            "time": renko_bricks[idx]["time"],
                            "price": renko_bricks[idx]["close"],
                        }
                    )
                    break  # First match wins

        # Check bearish patterns (priority order)
        for pattern_key in ["three_back", "two_back", "one_back", "zigzag"]:
            pattern_config = PATTERNS["bearish"][pattern_key]

            if not pattern_config["enabled"]:
                continue

            seq = pattern_config["sequence"]
            seq_len = len(seq)

            if len(recent_types) >= seq_len:
                if recent_types[-seq_len:] == seq:
                    detected.append(
                        {
                            "brick_idx": idx,
                            "pattern_name": pattern_config["name"],
                            "pattern_type": "bearish",
                            "side": "SHORT",
                            "time": renko_bricks[idx]["time"],
                            "price": renko_bricks[idx]["close"],
                        }
                    )
                    break  # First match wins

    return detected


# =============================================================================
# RENKO CHART EXPORT (PNG + TXT)
# =============================================================================


def export_renko_chart(
    renko_bricks: List[Dict[str, Any]], trading_date: Optional[date] = None
) -> bool:
    """
    Export Renko chart as PNG image and TXT file with pattern detection.

    Args:
        renko_bricks: List of Renko brick dictionaries
        trading_date: Date for filename (default: today)

    Returns:
        True if exported successfully
    """
    if not EXPORT_RENKO_CHART:
        logging.debug("Renko chart export disabled")
        return False

    if not renko_bricks:
        logging.warning("No Renko bricks to export")
        return False

    if trading_date is None:
        trading_date = get_current_date()

    try:
        # Detect patterns
        patterns = detect_patterns_for_export(renko_bricks)

        # Export TXT file first
        txt_success = _export_renko_txt(renko_bricks, patterns, trading_date)

        # Export PNG chart
        png_success = _export_renko_png(renko_bricks, patterns, trading_date)

        if txt_success and png_success:
            logging.info(
                "✅ Renko chart exported (PNG + TXT) with %d patterns", len(patterns)
            )
            return True
        else:
            logging.warning(
                "⚠️ Partial export: TXT=%s, PNG=%s", txt_success, png_success
            )
            return False

    except Exception as exc:
        logging.error("Failed to export Renko chart: %s", exc, exc_info=True)
        return False


def _export_renko_txt(
    renko_bricks: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
    trading_date: date,
) -> bool:
    """
    Export Renko bricks to TXT file with DATE + TIME + PATTERN + HIGH/LOW columns.

    Format:
    Brick#, Date, Time, Type, Close, High, Low, Size, Volume, Consecutive,
    Continuation, Reversal, Event, Pattern
    """
    try:
        filename = RENKO_CHARTS_DIR / f"renko_{trading_date.strftime('%Y%m%d')}.txt"

        # Create pattern lookup
        pattern_map = {p["brick_idx"]: p for p in patterns}

        with open(filename, "w", encoding="utf-8") as f:
            # Header
            f.write("=" * 180 + "\n")
            f.write(f"NIFTY RENKO CHART - {trading_date.strftime('%d-%b-%Y')}\n")
            f.write("=" * 180 + "\n\n")

            # Statistics
            green_bricks = sum(1 for b in renko_bricks if b["type"] == 1)
            red_bricks = sum(1 for b in renko_bricks if b["type"] == -1)

            # Count reversals
            reversals = 0
            for i in range(1, len(renko_bricks)):
                if renko_bricks[i]["type"] != renko_bricks[i - 1]["type"]:
                    reversals += 1

            # Date range
            first_date = renko_bricks[0]["time"].strftime("%d-%b-%Y")
            last_date = renko_bricks[-1]["time"].strftime("%d-%b-%Y")
            first_time = renko_bricks[0]["time"].strftime("%H:%M:%S")
            last_time = renko_bricks[-1]["time"].strftime("%H:%M:%S")

            # Pattern statistics
            bullish_patterns = sum(
                1 for p in patterns if p["pattern_type"] == "bullish"
            )
            bearish_patterns = sum(
                1 for p in patterns if p["pattern_type"] == "bearish"
            )

            f.write(f"Total Bricks: {len(renko_bricks)}\n")
            f.write(
                f"Green Bricks: {green_bricks} ({green_bricks / len(renko_bricks) * 100:.1f}%)\n"
            )
            f.write(
                f"Red Bricks: {red_bricks} ({red_bricks / len(renko_bricks) * 100:.1f}%)\n"
            )
            f.write(f"Reversals: {reversals}\n")
            f.write(
                f"Date Range: {first_date} {first_time} to {last_date} {last_time}\n"
            )
            f.write(f"\nPatterns Detected: {len(patterns)}\n")
            f.write(f"  Bullish: {bullish_patterns} (LONG signals)\n")
            f.write(f"  Bearish: {bearish_patterns} (SHORT signals)\n")

            if renko_bricks:
                first_brick = renko_bricks[0]
                brick_size = first_brick["size"]
                brick_size_pct = (brick_size / first_brick["close"]) * 100
                f.write(f"\nBrick Size: {brick_size:.2f} ({brick_size_pct:.4f}%)\n")

                f.write(f"Reversal Threshold: {brick_size:.2f} (from brick edge)\n")
                f.write(f"Reversal Rule: Plot (actual_bricks - 1)\n")

            f.write("\n" + "=" * 180 + "\n\n")

            # Column headers
            f.write(
                f"{'Brick#':<8} {'Date':<12} {'Time':<10} {'Type':<6} "
                f"{'Close':<10} {'High':<10} {'Low':<10} {'Size':<8} {'Volume':<10} "
                f"{'Consec':<8} {'Green+':<10} {'Red-':<10} {'Event':<10} {'Pattern':<30}\n"
            )
            f.write("-" * 180 + "\n")

            # Track day changes
            current_day = None

            # Brick data
            for i, brick in enumerate(renko_bricks):
                brick_date = brick["time"].strftime("%d-%b-%Y")
                brick_time = brick["time"].strftime("%H:%M:%S")
                brick_type = "GREEN" if brick["type"] == 1 else "RED"
                brick_close = brick["close"]
                brick_high = brick["high"]
                brick_low = brick["low"]
                brick_size = brick["size"]

                # Add day separator
                if current_day is None:
                    current_day = brick_date
                elif brick_date != current_day:
                    f.write("=" * 180 + "\n")
                    f.write(f">>> NEW DAY: {brick_date}\n")
                    f.write("=" * 180 + "\n")
                    current_day = brick_date

                # Calculate thresholds from brick HIGH/LOW
                if brick["type"] == 1:  # GREEN brick
                    green_continuation = brick_high + brick_size
                    red_reversal = brick_low - brick_size
                else:  # RED brick
                    green_continuation = brick_high + brick_size
                    red_reversal = brick_low - brick_size

                # Detect event (reversal or continuation)
                event = ""
                if i > 0:
                    prev_brick = renko_bricks[i - 1]
                    if brick["type"] != prev_brick["type"]:
                        event = "REVERSAL"
                    else:
                        event = "CONTINUE"
                else:
                    event = "START"

                # Check for pattern at this brick
                pattern_str = ""
                if i in pattern_map:
                    pattern = pattern_map[i]
                    pattern_str = (
                        f"{pattern['pattern_name']} ({pattern['pattern_type']})"
                    )

                # Write row
                f.write(
                    f"{i:<8} {brick_date:<12} {brick_time:<10} {brick_type:<6} "
                    f"{brick_close:<10.2f} {brick_high:<10.2f} {brick_low:<10.2f} "
                    f"{brick_size:<8.2f} {brick['volume']:<10.0f} {brick['consecutive']:<8} "
                    f"{green_continuation:<10.2f} {red_reversal:<10.2f} "
                    f"{event:<10} {pattern_str:<30}\n"
                )

            f.write("\n" + "=" * 180 + "\n\n")

            # Pattern summary
            if patterns:
                f.write("PATTERNS DETECTED:\n")
                f.write("-" * 180 + "\n")
                f.write(
                    f"{'Brick#':<8} {'Date':<12} {'Time':<10} {'Pattern':<15} "
                    f"{'Type':<10} {'Side':<8} {'Price':<10}\n"
                )
                f.write("-" * 180 + "\n")

                for pattern in patterns:
                    pattern_date = pattern["time"].strftime("%d-%b-%Y")
                    pattern_time = pattern["time"].strftime("%H:%M:%S")

                    f.write(
                        f"{pattern['brick_idx']:<8} {pattern_date:<12} {pattern_time:<10} "
                        f"{pattern['pattern_name']:<15} {pattern['pattern_type']:<10} "
                        f"{pattern['side']:<8} {pattern['price']:<10.2f}\n"
                    )

                f.write("\n" + "=" * 180 + "\n\n")

            # Add explanation
            f.write("COLUMN DEFINITIONS:\n")
            f.write("-" * 180 + "\n")
            f.write("Brick#   : Sequential brick number (0, 1, 2, ...)\n")
            f.write("Date     : Date when brick formed (DD-MMM-YYYY)\n")
            f.write("Time     : Time when brick formed (HH:MM:SS)\n")
            f.write("Type     : GREEN (up) or RED (down)\n")
            f.write("Close    : Brick close price\n")
            f.write("High     : Brick high price (top edge)\n")
            f.write("Low      : Brick low price (bottom edge)\n")
            f.write("Size     : Brick size (fixed for all bricks)\n")
            f.write("Volume   : Accumulated volume for this brick\n")
            f.write("Consec   : Consecutive bricks in same direction\n")
            f.write(
                "Green+   : Price needed for GREEN continuation (brick_high + size)\n"
            )
            f.write("Red-     : Price needed for RED reversal (brick_low - size)\n")
            f.write("Event    : START | CONTINUE | REVERSAL\n")
            f.write("Pattern  : Detected pattern at this brick (if any)\n")
            f.write("\n")
            f.write("THRESHOLD LOGIC:\n")
            f.write("-" * 180 + "\n")
            f.write("GREEN brick (close=100, high=100, low=99, size=1):\n")
            f.write("  - GREEN continuation: price >= high + size (101)\n")
            f.write("  - GREEN→RED reversal: price <= low - size (98)\n")
            f.write("\n")
            f.write("RED brick (close=99, high=100, low=99, size=1):\n")
            f.write("  - RED continuation: price <= low - size (98)\n")
            f.write("  - RED→GREEN reversal: price >= high + size (101)\n")
            f.write("\n")
            f.write("REVERSAL RULE (Prashant Shah):\n")
            f.write("-" * 180 + "\n")
            f.write("- Threshold distance = 2 × brick_size (from brick edge)\n")
            f.write("- On reversal: Plot (actual_bricks - 1) in opposite direction\n")
            f.write("- Minimum 1 brick on reversal\n")
            f.write("\n")
            f.write("Example:\n")
            f.write(
                "  Last GREEN brick: close=25,700, high=25,700, low=25,690, size=10\n"
            )
            f.write("  Price drops to 25,675\n")
            f.write("  Reversal threshold: 25,690 - 10 = 25,680\n")
            f.write("  25,675 <= 25,680? YES (reversal triggered)\n")
            f.write("  Price drop from brick_low: 25,690 - 25,675 = 15 points\n")
            f.write("  Actual bricks: 15/10 + 1 = 2 bricks\n")
            f.write("  Plot bricks: 2 - 1 = 1 RED brick\n")
            f.write("\n")
            f.write("PATTERN TYPES:\n")
            f.write("-" * 180 + "\n")
            f.write("Bullish Patterns (LONG signals - Buy CE):\n")
            f.write(
                "  - Three-Back: [1, 1, -1, -1, -1, 1] - Two up, three down, one up\n"
            )
            f.write("  - Two-Back:   [1, 1, -1, -1, 1]    - Two up, two down, one up\n")
            f.write("  - One-Back:   [1, 1, -1, 1]        - Two up, one down, one up\n")
            f.write("  - Zigzag:     [1, -1, 1, 1]        - Up, down, two up\n")
            f.write("\n")
            f.write("Bearish Patterns (SHORT signals - Buy PE):\n")
            f.write(
                "  - Three-Back: [-1, -1, 1, 1, 1, -1] - Two down, three up, one down\n"
            )
            f.write(
                "  - Two-Back:   [-1, -1, 1, 1, -1]    - Two down, two up, one down\n"
            )
            f.write(
                "  - One-Back:   [-1, -1, 1, -1]       - Two down, one up, one down\n"
            )
            f.write("  - Zigzag:     [-1, 1, -1, -1]       - Down, up, two down\n")
            f.write("\n" + "=" * 180 + "\n")

        logging.info("✅ Renko TXT exported: %s", filename)
        return True

    except Exception as exc:
        logging.error("Failed to export Renko TXT: %s", exc)
        return False


def _export_renko_png(
    renko_bricks: List[Dict[str, Any]],
    patterns: List[Dict[str, Any]],
    trading_date: date,
) -> bool:
    """
    Export Renko chart as PNG image with pattern annotations.

    Uses brick HIGH/LOW for correct visualization.
    """
    fig = None

    try:
        # Create figure
        fig, ax = plt.subplots(figsize=(24, 12), dpi=100)

        # Track day changes for markers
        day_markers = []
        current_day = None

        # Plot bricks using HIGH/LOW
        for i, brick in enumerate(renko_bricks):
            brick_close = brick["close"]
            brick_high = brick["high"]
            brick_low = brick["low"]
            brick_type = brick["type"]
            brick_date = brick["time"].strftime("%d-%b")

            if current_day is None:
                current_day = brick_date
                day_markers.append((i, brick_date))
            elif brick_date != current_day:
                current_day = brick_date
                day_markers.append((i, brick_date))

            # Draw brick using HIGH/LOW (not close)
            if brick_type == 1:  # GREEN
                rect_bottom = brick_low
                rect_height = brick_high - brick_low
                color = "green"
                edge_color = "darkgreen"
            else:  # RED
                rect_bottom = brick_low
                rect_height = brick_high - brick_low
                color = "red"
                edge_color = "darkred"

            rect = Rectangle(
                (i - 0.4, rect_bottom),
                width=0.8,
                height=rect_height,
                facecolor=color,
                edgecolor=edge_color,
                linewidth=1.5,
                alpha=0.8,
            )
            ax.add_patch(rect)

            # Add brick number every 20 bricks
            if i % 20 == 0:
                ax.text(
                    i,
                    brick_high + (brick_high - brick_low) * 0.3,
                    str(i),
                    ha="center",
                    va="bottom",
                    fontsize=8,
                    color="gray",
                    fontweight="bold",
                )

        # Add day markers
        for marker_idx, marker_date in day_markers:
            ax.axvline(
                x=marker_idx, color="blue", linestyle="--", linewidth=2, alpha=0.5
            )

            if renko_bricks:
                max_price = max(b["high"] for b in renko_bricks)
                ax.text(
                    marker_idx,
                    max_price,
                    marker_date,
                    ha="left",
                    va="bottom",
                    fontsize=10,
                    color="blue",
                    fontweight="bold",
                    rotation=0,
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="yellow", alpha=0.7),
                )

        # Add pattern markers
        for pattern in patterns:
            idx = pattern["brick_idx"]
            brick = renko_bricks[idx]

            if pattern["pattern_type"] == "bullish":
                marker_y = brick["low"] - brick["size"] * 0.5
                marker_color = "green"
                arrow_direction = "^"
            else:
                marker_y = brick["high"] + brick["size"] * 0.5
                marker_color = "red"
                arrow_direction = "v"

            ax.plot(
                idx,
                marker_y,
                marker=arrow_direction,
                markersize=12,
                color=marker_color,
                markeredgecolor="black",
                markeredgewidth=1.5,
            )

            pattern_label = f"{pattern['pattern_name']}\n({pattern['side']})"
            ax.text(
                idx,
                marker_y,
                pattern_label,
                ha="center",
                va="top" if pattern["pattern_type"] == "bullish" else "bottom",
                fontsize=7,
                color="black",
                fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8),
            )

        # Set axis limits
        if renko_bricks:
            all_highs = [b["high"] for b in renko_bricks]
            all_lows = [b["low"] for b in renko_bricks]

            min_price = min(all_lows)
            max_price = max(all_highs)
            price_range = max_price - min_price

            ax.set_xlim(-1, len(renko_bricks))
            ax.set_ylim(min_price - price_range * 0.1, max_price + price_range * 0.15)

        ax.set_xlabel("Brick Number", fontsize=12, fontweight="bold")
        ax.set_ylabel("Nifty Price", fontsize=12, fontweight="bold")

        # Title
        first_date = renko_bricks[0]["time"].strftime("%d-%b-%Y")
        last_date = renko_bricks[-1]["time"].strftime("%d-%b-%Y")

        if first_date == last_date:
            title = f"Nifty Renko Chart - {first_date} ({len(renko_bricks)} bricks, {len(patterns)} patterns)"
        else:
            title = f"Nifty Renko Chart - {first_date} to {last_date} ({len(renko_bricks)} bricks, {len(patterns)} patterns)"

        ax.set_title(title, fontsize=14, fontweight="bold")

        ax.grid(True, alpha=0.3, linestyle="--", axis="y")
        ax.grid(True, alpha=0.2, linestyle=":", axis="x")

        # Statistics
        green_bricks = sum(1 for b in renko_bricks if b["type"] == 1)
        red_bricks = sum(1 for b in renko_bricks if b["type"] == -1)

        first_price = renko_bricks[0]["close"]
        last_price = renko_bricks[-1]["close"]
        price_change = last_price - first_price
        price_change_pct = (price_change / first_price) * 100

        reversals = sum(
            1
            for i in range(1, len(renko_bricks))
            if renko_bricks[i]["type"] != renko_bricks[i - 1]["type"]
        )

        bullish_patterns = sum(1 for p in patterns if p["pattern_type"] == "bullish")
        bearish_patterns = sum(1 for p in patterns if p["pattern_type"] == "bearish")

        stats_text = (
            f"Total Bricks: {len(renko_bricks)}\n"
            f"Green: {green_bricks} ({green_bricks / len(renko_bricks) * 100:.1f}%)\n"
            f"Red: {red_bricks} ({red_bricks / len(renko_bricks) * 100:.1f}%)\n"
            f"Reversals: {reversals}\n"
            f"Brick Size: {renko_bricks[0]['size']:.2f} ({renko_bricks[0]['size'] / renko_bricks[0]['close'] * 100:.4f}%)\n"
            f"Price Change: {price_change:+.2f} ({price_change_pct:+.2f}%)\n"
            f"Range: {min_price:.2f} - {max_price:.2f}\n"
            f"\n"
            f"Patterns: {len(patterns)}\n"
            f"  Bullish: {bullish_patterns}\n"
            f"  Bearish: {bearish_patterns}"
        )

        ax.text(
            0.02,
            0.98,
            stats_text,
            transform=ax.transAxes,
            fontsize=10,
            verticalalignment="top",
            bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.9),
            family="monospace",
        )

        # Save
        filename = RENKO_CHARTS_DIR / f"renko_{trading_date.strftime('%Y%m%d')}.png"
        plt.tight_layout()
        plt.savefig(filename, dpi=100, bbox_inches="tight")

        logging.info("✅ Renko PNG exported: %s", filename)
        return True

    except Exception as exc:
        logging.error("Failed to export Renko PNG: %s", exc)
        return False

    finally:
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass


# =============================================================================
# OPTION DATA EXPORT
# =============================================================================


def export_option_data(
    option_symbols: Set[str], data_engine, trading_date: Optional[date] = None
) -> bool:
    """
    Export 1-minute OHLCV data for traded option contracts.

    Args:
        option_symbols: Set of option symbols traded today
        data_engine: DataEngine instance for fetching data
        trading_date: Date for filename (default: today)

    Returns:
        True if exported successfully
    """
    if not EXPORT_OPTION_DATA:
        logging.debug("Option data export disabled")
        return False

    if not option_symbols:
        logging.warning("No option symbols to export")
        return False

    if trading_date is None:
        trading_date = get_current_date()

    success_count = 0

    for symbol in option_symbols:
        try:
            # Fetch 1-minute data for the symbol
            bars = _fetch_option_bars(symbol, trading_date, data_engine)

            if not bars:
                logging.warning("No data for %s", symbol)
                continue

            # Save to CSV
            filename = (
                OPTION_DATA_DIR
                / f"{_sanitize_symbol(symbol)}_{trading_date.strftime('%Y%m%d')}.csv"
            )

            with open(filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)

                # Header
                writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])

                # Data
                for bar in bars:
                    writer.writerow(
                        [
                            bar["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
                            bar["open"],
                            bar["high"],
                            bar["low"],
                            bar["close"],
                            bar["volume"],
                        ]
                    )

            logging.info("✅ Option data exported: %s (%d bars)", symbol, len(bars))
            success_count += 1

        except Exception as exc:
            logging.error("Failed to export data for %s: %s", symbol, exc)

    if success_count > 0:
        logging.info(
            "✅ Exported data for %d/%d option contracts",
            success_count,
            len(option_symbols),
        )
        return True
    else:
        logging.warning("Failed to export any option data")
        return False


def _fetch_option_bars(
    symbol: str, trading_date: date, data_engine
) -> List[Dict[str, Any]]:
    """
    Fetch 1-minute bars for option symbol.

    Args:
        symbol: Option symbol (e.g., "NSE:NIFTY2512124500CE")
        trading_date: Date to fetch data for
        data_engine: DataEngine instance

    Returns:
        List of bar dictionaries
    """
    try:
        date_str = trading_date.strftime("%Y-%m-%d")

        response = data_engine._fyers.history(
            {
                "symbol": symbol,
                "resolution": "1",
                "date_format": "1",
                "range_from": date_str,
                "range_to": date_str,
                "cont_flag": "1",
            }
        )

        if response.get("code") != 200:
            logging.warning("Failed to fetch data for %s: %s", symbol, response)
            return []

        candles = response.get("candles", [])

        bars = []
        for candle in candles:
            if len(candle) < 6:
                continue

            bars.append(
                {
                    "timestamp": datetime.fromtimestamp(candle[0], tz=IST),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
            )

        return bars

    except Exception as exc:
        logging.error("Error fetching bars for %s: %s", symbol, exc)
        return []


def _sanitize_symbol(symbol: str) -> str:
    """
    Sanitize symbol for filename.

    Args:
        symbol: Option symbol (e.g., "NSE:NIFTY2512124500CE")

    Returns:
        Sanitized string (e.g., "NIFTY2512124500CE")
    """
    return symbol.replace("NSE:", "").replace(":", "_")


def export_1min_nifty_data(
    bars: List[Dict[str, Any]], trading_date: Optional[date] = None
) -> bool:
    """
    Export 1-minute Nifty/Futures OHLCV data to CSV.

    Includes warmup data + current day data.

    Args:
        bars: List of 1-minute bar dictionaries
        trading_date: Date for filename (default: today)

    Returns:
        True if exported successfully
    """
    if not bars:
        logging.warning("No 1-min bars to export")
        return False

    if trading_date is None:
        trading_date = get_current_date()

    try:
        # Create directory
        oneminute_dir = EXPORT_DIR / "1min_data"
        oneminute_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename
        filename = oneminute_dir / f"nifty_1min_{trading_date.strftime('%Y%m%d')}.csv"

        # Write CSV
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            # Header
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])

            # Data rows
            for bar in bars:
                writer.writerow(
                    [
                        bar["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
                        bar["open"],
                        bar["high"],
                        bar["low"],
                        bar["close"],
                        bar["volume"],
                    ]
                )

        logging.info("✅ Exported %d 1-min bars to %s", len(bars), filename)
        return True

    except Exception as exc:
        logging.error("Failed to export 1-min data: %s", exc)
        return False


# =============================================================================
# MAIN EXPORT FUNCTION
# =============================================================================


def export_daily_data(
    renko_bricks: List[Dict[str, Any]],
    option_symbols: Set[str],
    data_engine,
    oneminute_bars: Optional[List[Dict[str, Any]]] = None,
    trading_date: Optional[date] = None,
) -> Dict[str, bool]:
    """
    Export all daily data (Renko chart + option data).

    Args:
        renko_bricks: List of Renko brick dictionaries
        option_symbols: Set of option symbols traded today
        data_engine: DataEngine instance
        trading_date: Date for export (default: today)

    Returns:
        Dictionary with export results
    """
    if trading_date is None:
        trading_date = get_current_date()

    logging.info("=" * 60)
    logging.info("EXPORTING DAILY DATA: %s", trading_date.strftime("%d-%b-%Y"))
    logging.info("=" * 60)

    results = {"renko_chart": False, "option_data": False, "oneminute_data": False}

    # Export Renko chart
    if EXPORT_RENKO_CHART:
        results["renko_chart"] = export_renko_chart(renko_bricks, trading_date)

    # Export option data
    if EXPORT_OPTION_DATA:
        results["option_data"] = export_option_data(
            option_symbols, data_engine, trading_date
        )

    # Export 1-minute data
    if oneminute_bars:
        results["oneminute_data"] = export_1min_nifty_data(oneminute_bars, trading_date)

    # Summary
    logging.info("=" * 60)
    logging.info("EXPORT SUMMARY:")
    logging.info(
        "  Renko Chart: %s",
        "✅ Success" if results["renko_chart"] else "❌ Failed/Disabled",
    )
    logging.info(
        "  Option Data: %s",
        "✅ Success" if results["option_data"] else "❌ Failed/Disabled",
    )
    logging.info(
        "  1-Min Data: %s",
        "✅ Success" if results["oneminute_data"] else "❌ Failed/Disabled",
    )
    logging.info("=" * 60)

    return results


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "export_daily_data",
    "export_renko_chart",
    "export_option_data",
    "EXPORT_RENKO_CHART",
    "EXPORT_OPTION_DATA",
]
