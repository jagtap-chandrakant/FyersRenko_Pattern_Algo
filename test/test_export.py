#!/usr/bin/env python3
"""
Test Export with REAL Market Data

Downloads actual Nifty Futures data and generates Renko chart.
Includes warmup data with clear date marking.

Author: Chandrakant Jagtap
Version: 2.1.0 (Fixed - Date Column)
"""

import sys
import logging
from datetime import date
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import get_current_date, STRATEGY
from src.data import DataEngine
from src.strategy import RenkoStrategy
from src.export import export_renko_chart


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def download_real_data(data_engine: DataEngine, trading_date: date = None):
    """
    Download real Nifty Futures data including warmup.
    
    Args:
        data_engine: DataEngine instance
        trading_date: Date to export (default: today)
    
    Returns:
        Tuple of (all_bars, warmup_bar_count, trading_date)
    """
    if trading_date is None:
        trading_date = get_current_date()
    
    print("\n" + "=" * 80)
    print("DOWNLOADING REAL MARKET DATA")
    print("=" * 80)
    print(f"Trading Date: {trading_date.strftime('%d-%b-%Y')}")
    print("Warmup Days: 3")
    print("=" * 80 + "\n")
    
    # Download warmup data (3 days)
    print("Step 1: Downloading warmup data (3 days)...")
    warmup_bars = data_engine.get_warmup_data(days=3)
    
    if not warmup_bars:
        print("❌ Failed to download warmup data")
        return None, 0, trading_date
    
    print(f"✅ Downloaded {len(warmup_bars)} warmup bars")
    
    # Show warmup date range
    if warmup_bars:
        first_warmup = warmup_bars[0]['timestamp'].strftime('%d-%b-%Y %H:%M')
        last_warmup = warmup_bars[-1]['timestamp'].strftime('%d-%b-%Y %H:%M')
        print(f"   Warmup Range: {first_warmup} to {last_warmup}")
    
    # Download today's data
    print(f"\nStep 2: Downloading today's data ({trading_date.strftime('%d-%b-%Y')})...")
    today_bars = []
    
    # If market is open, get latest bar
    if data_engine.is_market_open():
        print("   Market is OPEN - fetching latest data...")
        latest_bar = data_engine.get_latest_bar()
        if latest_bar:
            today_bars.append(latest_bar)
            print(f"✅ Downloaded {len(today_bars)} bars from today")
        else:
            print("⚠️ No data available yet (market just opened?)")
    else:
        print("   Market is CLOSED - using warmup data only")
    
    # Combine all bars
    all_bars = warmup_bars + today_bars
    warmup_count = len(warmup_bars)
    
    print("\n" + "=" * 80)
    print("DOWNLOAD SUMMARY")
    print("=" * 80)
    print(f"Total Bars: {len(all_bars)}")
    print(f"Warmup Bars: {warmup_count}")
    print(f"Today's Bars: {len(today_bars)}")
    
    if all_bars:
        first_bar = all_bars[0]
        last_bar = all_bars[-1]
        print(f"Date Range: {first_bar['timestamp'].strftime('%d-%b %H:%M')} to {last_bar['timestamp'].strftime('%d-%b %H:%M')}")
        print(f"Price Range: {min(b['close'] for b in all_bars):.2f} - {max(b['close'] for b in all_bars):.2f}")
    
    print("=" * 80 + "\n")
    
    return all_bars, warmup_count, trading_date


def generate_renko_from_real_data(bars, warmup_count: int):
    """
    Generate Renko bricks from real market data.
    
    Args:
        bars: List of OHLCV bars
        warmup_count: Number of warmup bars
    
    Returns:
        Tuple of (renko_bricks, warmup_brick_count)
    """
    print("=" * 80)
    print("GENERATING RENKO BRICKS FROM REAL DATA")
    print("=" * 80)
    
    # Create strategy instance
    strategy = RenkoStrategy(STRATEGY)
    
    # Feed all bars to strategy
    print(f"Processing {len(bars)} bars...")
    for bar in bars:
        strategy.on_1min_bar(
            bar['timestamp'],
            bar['open'],
            bar['high'],
            bar['low'],
            bar['close'],
            bar['volume']
        )
    
    # Get generated bricks
    renko_bricks = strategy.renko_bricks
    
    if not renko_bricks:
        print("❌ No Renko bricks generated")
        return None, 0
    
    # Find where warmup ends and today starts
    warmup_brick_count = 0
    if warmup_count > 0 and warmup_count < len(bars):
        warmup_end_time = bars[warmup_count - 1]['timestamp']
        
        # Count bricks before warmup end
        for brick in renko_bricks:
            if brick['time'] <= warmup_end_time:
                warmup_brick_count += 1
            else:
                break
    
    print(f"✅ Generated {len(renko_bricks)} Renko bricks")
    print(f"   Warmup Bricks: {warmup_brick_count}")
    print(f"   Today's Bricks: {len(renko_bricks) - warmup_brick_count}")
    
    # Statistics
    green_bricks = sum(1 for b in renko_bricks if b['type'] == 1)
    red_bricks = sum(1 for b in renko_bricks if b['type'] == -1)
    
    print(f"   Green: {green_bricks} ({green_bricks/len(renko_bricks)*100:.1f}%)")
    print(f"   Red: {red_bricks} ({red_bricks/len(renko_bricks)*100:.1f}%)")
    
    # Count reversals
    reversals = sum(1 for i in range(1, len(renko_bricks)) 
                   if renko_bricks[i]['type'] != renko_bricks[i-1]['type'])
    print(f"   Reversals: {reversals}")
    
    # Show date range
    if renko_bricks:
        first_date = renko_bricks[0]['time'].strftime('%d-%b-%Y')
        last_date = renko_bricks[-1]['time'].strftime('%d-%b-%Y')
        print(f"   Date Range: {first_date} to {last_date}")
    
    print("=" * 80 + "\n")
    
    return renko_bricks, warmup_brick_count


def print_brick_summary(renko_bricks, warmup_brick_count: int):
    """Print summary of generated bricks with date marking."""
    print("=" * 110)
    print("RENKO BRICKS SUMMARY (Real Market Data)")
    print("=" * 110)
    
    # Find trading day start
    # trading_day_start_idx = warmup_brick_count
    
    print(f"\n{'#':<5} {'Date':<12} {'Time':<10} {'Type':<8} {'Close':<10} {'Consec':<8} {'Event':<10} {'Phase':<10}")
    print("-" * 110)
    
    # Show last 10 warmup bricks + first 20 today bricks
    start_idx = max(0, warmup_brick_count - 10)
    end_idx = min(len(renko_bricks), warmup_brick_count + 20)
    
    for i in range(start_idx, end_idx):
        brick = renko_bricks[i]
        brick_type = "GREEN" if brick['type'] == 1 else "RED"
        brick_date = brick['time'].strftime('%d-%b-%Y')
        brick_time = brick['time'].strftime('%H:%M:%S')
        
        # Determine event
        if i == 0:
            event = "START"
        elif brick['type'] != renko_bricks[i-1]['type']:
            event = "REVERSAL"
        else:
            event = "CONTINUE"
        
        # Determine phase
        if i < warmup_brick_count:
            phase = "WARMUP"
        elif i == warmup_brick_count:
            phase = ">>> TODAY"
        else:
            phase = "TODAY"
        
        print(
            f"{i:<5} {brick_date:<12} {brick_time:<10} {brick_type:<8} {brick['close']:<10.2f} "
            f"{brick['consecutive']:<8} {event:<10} {phase:<10}"
        )
        
        # Add separator at trading day start
        if i == warmup_brick_count - 1 and warmup_brick_count > 0:
            print("=" * 110)
    
    if len(renko_bricks) > end_idx:
        print(f"... ({len(renko_bricks) - end_idx} more bricks)")
    
    print("=" * 110 + "\n")


def main():
    """Main test function with real market data."""
    print("\n" + "=" * 80)
    print("RENKO EXPORT TEST - REAL MARKET DATA")
    print("=" * 80 + "\n")
    
    try:
        # Initialize data engine
        print("Initializing data engine...")
        data_engine = DataEngine()
        print("✅ Data engine initialized\n")
        
        # Download real data
        all_bars, warmup_count, trading_date = download_real_data(data_engine)
        
        if not all_bars:
            print("❌ Failed to download data")
            return False
        
        # Generate Renko bricks
        renko_bricks, warmup_brick_count = generate_renko_from_real_data(all_bars, warmup_count)
        
        if not renko_bricks:
            print("❌ Failed to generate Renko bricks")
            return False
        
        # Print summary
        print_brick_summary(renko_bricks, warmup_brick_count)
        
        # Export
        print("=" * 80)
        print("EXPORTING RENKO CHART (PNG + TXT)")
        print("=" * 80)
        success = export_renko_chart(renko_bricks, trading_date)
        
        if success:
            print("\n" + "=" * 80)
            print("✅ EXPORT SUCCESSFUL!")
            print("=" * 80)
            print("\nGenerated files:")
            print(f"  1. PNG Chart: data/exports/renko_charts/renko_{trading_date.strftime('%Y%m%d')}.png")
            print(f"  2. TXT Data:  data/exports/renko_charts/renko_{trading_date.strftime('%Y%m%d')}.txt")
            print("\nWhat to check in TXT file:")
            print("  - Date column shows which day each brick belongs to")
            print("  - Day separator (===) when date changes")
            print(f"  - Warmup bricks: 0-{warmup_brick_count-1}")
            print(f"  - Today's bricks: {warmup_brick_count}-{len(renko_bricks)-1}")
            print("  - Continuation column: Price for next brick in same direction")
            print("  - Reversal column: Price for reversal (2 bricks opposite)")
            print("\nWhat to check in PNG chart:")
            print("  - Blue vertical lines mark day changes")
            print("  - Date labels at top of each day")
            print("  - Green bricks stack upward")
            print("  - Red bricks stack downward")
            print("  - No overlapping bricks")
            print("\nHow to compare with broker:")
            print("  1. Open broker's Renko chart for same day")
            print("  2. Set brick size to 0.04%")
            print("  3. Set reversal to 2 bricks")
            print(f"  4. Compare bricks from brick #{warmup_brick_count} onwards")
            print("  5. Verify brick count, sequence, and prices match")
            print("=" * 80 + "\n")
            return True
        else:
            print("\n❌ Export failed")
            return False
    
    except Exception as exc:
        print(f"\n❌ Error: {exc}")
        logging.error("Test failed", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)