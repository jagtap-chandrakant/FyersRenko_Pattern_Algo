# src/main.py
"""
Nifty Renko Trading System - Main Module

Pattern-based options trading system using Renko bricks with
incremental brick construction and synthetic futures.

Author: Chandrakant Jagtap
Version: 7.0.0 (Incremental Renko + Synthetic Futures Fix)
"""

import atexit
import csv
import json
import logging
import math
import os
import signal
import sys
import time as time_module
from datetime import datetime, date, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from filelock import FileLock, Timeout

from src.config import STRATEGY, TRADING_MODE, HEDGE_STRIKE_OFFSET
from src.trading import OrderSide
from src.alerts import (
    send_trade_alert,
    send_daily_summary,
    send_risk_alert,
    send_system_alert,
    print_brick_formation,
    print_signal,
    print_trade_execution,
    print_startup,
)
from src.utils import (
    parse_expiry_date,
    parse_option_symbol,
    calculate_next_expiry,
    is_market_hours,
    get_current_time,
    get_current_date,
    DATE_FORMAT_EXPIRY,
)

# ============================================================================
# GLOBAL TRADER INSTANCE
# ============================================================================

_trader_instance: Optional["Trader"] = None

# ============================================================================
# CONSTANTS
# ============================================================================

DATA_DIR = Path("data")
LOG_DIR = DATA_DIR / "logs"
TRADES_DIR = DATA_DIR / "trades"
REPORTS_DIR = DATA_DIR / "reports"
STATE_DIR = DATA_DIR / "state"
TOKEN_DIR = DATA_DIR / "accessTokens"

MASTER_TRADES_FILE = "trades_master.csv"
MASTER_SUMMARY_FILE = "summary_master.csv"

MAIN_LOOP_SLEEP = 45
WAIT_SLEEP = 30
STATE_SAVE_INTERVAL = 300
STATUS_DISPLAY_INTERVAL = 300

MAX_CONSECUTIVE_LOSSES = STRATEGY.get("max_consecutive_losses", 30)
MAX_DAILY_LOSS = STRATEGY.get("max_daily_loss", 150000)
CAPITAL_PER_LOT = STRATEGY.get("capital_per_lot", 100000)
LOT_SIZE = STRATEGY.get("lot_size", 75)
COST_PER_LOT = STRATEGY.get("cost_per_lot", 80)

EXPIRY_EXIT_WARNING = dtime(14, 30)
EXPIRY_EXIT_PRIMARY = dtime(15, 0)
EXPIRY_EXIT_FINAL = dtime(15, 15)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

TRADE_HEADERS = [
    "trade_number",
    "timestamp",
    "trading_mode",
    "status",
    "action",
    "buy_symbol",
    "buy_entry_price",
    "buy_exit_price",
    "buy_entry_oi",
    "buy_entry_volume",
    "buy_exit_oi",
    "buy_exit_volume",
    "sell_symbol",
    "sell_entry_price",
    "sell_exit_price",
    "sell_entry_oi",
    "sell_entry_volume",
    "sell_exit_oi",
    "sell_exit_volume",
    "net_premium",
    "atm_strike",
    "buy_leg_pnl",
    "sell_leg_pnl",
    "pnl",
    "costs",
    "expiry",
    "entry_type",
    "pattern_name",
    "pattern_type",
    "exit_reason",
    "lots",
    "nifty_price",
    "entry_price",
    "price",
    "holding_hours",
]


# ============================================================================
# LOGGING SETUP
# ============================================================================


def setup_logging() -> None:
    """Setup two-tier logging (console: INFO, file: DEBUG)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = LOG_DIR / f"trading_{get_current_date().strftime('%Y%m%d')}.log"

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.addFilter(_ConsoleFilter())

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.info("Logging initialized: %s", log_file)


class _ConsoleFilter(logging.Filter):
    """Filter to show only important messages on console."""

    SHOW_KEYWORDS = {
        "BRICK",
        "SIGNAL",
        "ENTRY",
        "EXIT",
        "EXECUTED",
        "ERROR",
        "FAILED",
        "Position",
        "Shutdown",
        "Startup",
        "Authentication",
        "Risk",
    }
    HIDE_KEYWORDS = {"Processing bar", "Price updated", "Cache", "Token", "Validation"}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        if any(kw in msg for kw in self.HIDE_KEYWORDS):
            return False
        if any(kw in msg for kw in self.SHOW_KEYWORDS):
            return True
        return record.levelno >= logging.INFO


# ============================================================================
# STATE MANAGEMENT
# ============================================================================


def get_state_file(target_date: Optional[date] = None) -> Path:
    """
    Get state file path for a specific date.

    Args:
        target_date: Date for state file (default: today)

    Returns:
        Path to state file
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if target_date is None:
        target_date = get_current_date()

    return STATE_DIR / f"state_{target_date.strftime('%Y%m%d')}.json"


def get_latest_state_file() -> Optional[Path]:
    """
    Find the most recent state file (today or previous days).

    Searches backwards up to 7 days to handle overnight/weekend positions.

    Returns:
        Path to latest state file, or None
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    today = get_current_date()

    # Check today first, then go backwards up to 7 days
    for days_back in range(8):  # 0 to 7 days back
        check_date = today - timedelta(days=days_back)
        state_file = get_state_file(check_date)

        if state_file.exists():
            # Found a state file
            if days_back > 0:
                logging.info(
                    "📅 Found state file from %d day(s) ago: %s",
                    days_back,
                    check_date.strftime("%Y-%m-%d"),
                )
            return state_file

    # No state file found
    logging.info("No state file found in last 7 days")
    return None


def load_state() -> Optional[Dict[str, Any]]:
    """
    Load state from most recent state file with timezone-aware datetime handling.

    Searches for state files across multiple days to handle overnight positions.

    Returns:
        State dictionary or None
    """
    from src.config import IST

    state_file = get_latest_state_file()

    if not state_file:
        logging.info("No previous state file found")
        return None

    lock_file = str(state_file) + ".lock"

    try:
        with FileLock(lock_file, timeout=5):
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)

        if "timestamp" in state and isinstance(state["timestamp"], str):
            try:
                dt = datetime.fromisoformat(state["timestamp"])
                if dt.tzinfo is None:
                    state["timestamp"] = IST.localize(dt)
                else:
                    state["timestamp"] = dt.astimezone(IST)
            except ValueError:
                state["timestamp"] = get_current_time()

        if state.get("entry_info") and isinstance(state["entry_info"].get("time"), str):
            try:
                dt = datetime.fromisoformat(state["entry_info"]["time"])
                if dt.tzinfo is None:
                    state["entry_info"]["time"] = IST.localize(dt)
                else:
                    state["entry_info"]["time"] = dt.astimezone(IST)
            except ValueError:
                state["entry_info"]["time"] = get_current_time()

        logging.info("✅ State loaded successfully")
        return state

    except Timeout:
        logging.warning("Could not acquire state file lock")
        return None
    except (json.JSONDecodeError, IOError) as exc:
        logging.error("Failed to load state: %s", exc)
        return None


def save_state(state: Dict[str, Any]) -> bool:
    """Save state to file with atomic write."""
    if not state:
        return False

    state_file = get_state_file()
    lock_file = str(state_file) + ".lock"
    temp_file = str(state_file) + ".tmp"

    state_copy = state.copy()

    if "timestamp" in state_copy and hasattr(state_copy["timestamp"], "isoformat"):
        state_copy["timestamp"] = state_copy["timestamp"].isoformat()

    if state_copy.get("entry_info") and hasattr(
        state_copy["entry_info"].get("time"), "isoformat"
    ):
        state_copy["entry_info"]["time"] = state_copy["entry_info"]["time"].isoformat()

    try:
        with FileLock(lock_file, timeout=5):
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(state_copy, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_file, state_file)

        logging.debug("State saved: %s", state_file)
        return True

    except Timeout:
        logging.error("Could not acquire state file lock for save")
        return False
    except (IOError, OSError) as exc:
        logging.error("Failed to save state: %s", exc)
        return False


# ============================================================================
# TRADE FILE MANAGEMENT
# ============================================================================


def save_trades(trades: List[Dict[str, Any]]) -> bool:
    """Save trades to daily and master CSV files."""
    if not trades:
        return True

    TRADES_DIR.mkdir(parents=True, exist_ok=True)

    daily_file = TRADES_DIR / f"trades_{get_current_date().strftime('%Y%m%d')}.csv"
    master_file = TRADES_DIR / MASTER_TRADES_FILE

    try:
        formatted = [_format_trade(t) for t in trades]

        with open(daily_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_HEADERS)
            writer.writeheader()
            writer.writerows(formatted)

        file_exists = master_file.exists()
        with open(master_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_HEADERS)
            if not file_exists:
                writer.writeheader()
            writer.writerows(formatted)

        logging.info("Saved %d trades to %s", len(trades), daily_file)
        return True

    except (IOError, OSError) as exc:
        logging.error("Failed to save trades: %s", exc)
        return False


def _format_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Format trade for CSV output."""

    def safe_round(val, decimals=2):
        try:
            return round(float(val), decimals) if val is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    def safe_int(val):
        try:
            return int(val) if val is not None else 0
        except (ValueError, TypeError):
            return 0

    timestamp = trade.get("timestamp")
    if hasattr(timestamp, "strftime"):
        timestamp = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "trade_number": trade.get("trade_number", 0),
        "timestamp": timestamp,
        "trading_mode": trade.get("trading_mode", TRADING_MODE),
        "status": trade.get("status", "PAPER" if TRADING_MODE == "PAPER" else "LIVE"),
        "action": trade.get("action", ""),
        "buy_symbol": trade.get("buy_symbol", ""),
        "buy_entry_price": safe_round(trade.get("buy_entry_price", 0)),
        "buy_exit_price": safe_round(trade.get("buy_exit_price", 0)),
        "buy_entry_oi": safe_int(trade.get("buy_entry_oi", 0)),
        "buy_entry_volume": safe_int(trade.get("buy_entry_volume", 0)),
        "buy_exit_oi": safe_int(trade.get("buy_exit_oi", 0)),
        "buy_exit_volume": safe_int(trade.get("buy_exit_volume", 0)),
        "sell_symbol": trade.get("sell_symbol", ""),
        "sell_entry_price": safe_round(trade.get("sell_entry_price", 0)),
        "sell_exit_price": safe_round(trade.get("sell_exit_price", 0)),
        "sell_entry_oi": safe_int(trade.get("sell_entry_oi", 0)),
        "sell_entry_volume": safe_int(trade.get("sell_entry_volume", 0)),
        "sell_exit_oi": safe_int(trade.get("sell_exit_oi", 0)),
        "sell_exit_volume": safe_int(trade.get("sell_exit_volume", 0)),
        "net_premium": safe_round(trade.get("net_premium", 0)),
        "atm_strike": safe_int(trade.get("atm_strike", 0)),
        "buy_leg_pnl": safe_round(trade.get("buy_leg_pnl", 0)),
        "sell_leg_pnl": safe_round(trade.get("sell_leg_pnl", 0)),
        "pnl": safe_round(trade.get("pnl", 0)),
        "costs": safe_round(trade.get("costs", 0)),
        "expiry": trade.get("expiry", ""),
        "entry_type": trade.get("entry_type", "pattern"),
        "pattern_name": trade.get("pattern_name", ""),
        "pattern_type": trade.get("pattern_type", ""),
        "exit_reason": trade.get("exit_reason", ""),
        "lots": int(trade.get("lots", 0)),
        "nifty_price": safe_round(trade.get("nifty_price", 0)),
        "entry_price": safe_round(trade.get("entry_price", 0)),
        "price": safe_round(trade.get("price", 0)),
        "holding_hours": safe_round(trade.get("holding_hours", 0)),
    }


def load_daily_trades() -> List[Dict[str, Any]]:
    """Load trades from today's daily file."""
    daily_file = TRADES_DIR / f"trades_{get_current_date().strftime('%Y%m%d')}.csv"

    if not daily_file.exists():
        return []

    try:
        trades = []
        with open(daily_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                for key in ["trade_number", "lots"]:
                    if row.get(key):
                        row[key] = int(row[key])
                for key in [
                    "nifty_price",
                    "entry_price",
                    "price",
                    "pnl",
                    "costs",
                    "holding_hours",
                ]:
                    if row.get(key):
                        row[key] = float(row[key])
                trades.append(row)

        logging.info("Loaded %d trades from %s", len(trades), daily_file)
        return trades

    except (IOError, csv.Error) as exc:
        logging.error("Failed to load trades: %s", exc)
        return []


# ============================================================================
# EXPIRY MANAGEMENT
# ============================================================================


def calculate_expiry_for_date(check_date: date, fyers_client=None) -> str:
    """Calculate expiry for given date."""
    next_expiry = calculate_next_expiry(check_date)

    if next_expiry == check_date:
        logging.info("Today is expiry day, using next week's expiry")
        next_expiry = calculate_next_expiry(check_date + timedelta(days=1))

    if fyers_client:
        try:
            response = fyers_client.optionchain(
                {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""}
            )

            if response.get("code") == 200:
                expiry_data = response.get("data", {}).get("expiryData", [])
                if expiry_data:
                    api_date_str = expiry_data[0]["date"]
                    api_date = datetime.strptime(api_date_str, "%d-%m-%Y").date()

                    if api_date == check_date and len(expiry_data) > 1:
                        api_date_str = expiry_data[1]["date"]
                        api_date = datetime.strptime(api_date_str, "%d-%m-%Y").date()

                    next_expiry = api_date
                    logging.info("Expiry verified from Fyers API: %s", next_expiry)
        except Exception as exc:
            logging.warning("Fyers expiry fetch failed (using calculated): %s", exc)

    expiry_str = next_expiry.strftime(DATE_FORMAT_EXPIRY)
    logging.info("Calculated expiry: %s", expiry_str)
    return expiry_str


# ============================================================================
# BLACK SWAN PROTECTION - HEDGE MANAGEMENT
# ============================================================================


def calculate_hedge_symbol(sell_symbol: str, position: int) -> Optional[str]:
    """
    Calculate hedge symbol for SHORT leg protection.

    For LONG synthetic (short PE): Buy OTM PE as hedge
    For SHORT synthetic (short CE): Buy OTM CE as hedge

    Args:
        sell_symbol: Symbol of the SHORT leg
        position: 1=LONG, -1=SHORT

    Returns:
        Hedge symbol or None
    """
    if not sell_symbol:
        return None

    try:
        # Parse sell symbol
        parsed = parse_option_symbol(sell_symbol)
        if not parsed:
            return None

        strike = parsed["strike"]
        option_type = parsed["option_type"]
        year = parsed["year"][-2:]
        month_code = parsed["month_code"]
        day = parsed["day"]

        # Calculate hedge strike (2 strikes OTM)
        from src.config import STRATEGY

        hedge_offset = STRATEGY.get("hedge_strike_offset", 100)

        if option_type == "PE":
            # Short PE → Buy lower strike PE as hedge
            hedge_strike = strike - hedge_offset
        else:
            # Short CE → Buy higher strike CE as hedge
            hedge_strike = strike + hedge_offset

        # Build hedge symbol
        hedge_symbol = f"NSE:NIFTY{year}{month_code}{day}{hedge_strike}{option_type}"

        logging.info("Hedge calculated: %s (protects %s)", hedge_symbol, sell_symbol)

        return hedge_symbol

    except Exception as exc:
        logging.error("Failed to calculate hedge symbol: %s", exc)
        return None


def should_buy_hedge(
    position: int, current_time: datetime, hedge_bought: bool
) -> Tuple[bool, str]:
    """
    Check if hedge should be bought before market close.

    Args:
        position: Current position (0=flat, 1=long, -1=short)
        current_time: Current datetime
        hedge_bought: Whether hedge already bought

    Returns:
        Tuple of (should_buy, reason)
    """
    if position == 0:
        return False, "No position open"

    if hedge_bought:
        return False, "Hedge already bought"

    from src.config import HEDGE_BUY_TIME, STRATEGY

    if not STRATEGY.get("enable_hedge_protection", True):
        return False, "Hedge protection disabled"

    time_now = current_time.time()

    if time_now >= HEDGE_BUY_TIME:
        return True, f"Market closing - buying hedge at {time_now.strftime('%H:%M:%S')}"

    return False, ""


def should_exit_hedge_only(
    position: int, hedge_bought: bool, hedge_exit_pending: bool, current_time: datetime
) -> Tuple[bool, str]:
    """
    Check if hedge should be exited (without closing main position).

    Called next day at 9:16 AM if no exit signal generated.

    Only exit hedge if:
    1. Position is still open (position != 0)
    2. Hedge is active (hedge_bought=True)
    3. Hedge exit is pending (hedge_exit_pending=True)
    4. Time is >= 9:16 AM

    Args:
        position: Current position
        hedge_bought: Whether hedge is active
        hedge_exit_pending: Flag set after hedge was bought at 3:25 PM
        current_time: Current datetime

    Returns:
        Tuple of (should_exit_hedge, reason)
    """
    if position == 0:
        # Main position closed - hedge should have been closed already
        # But if hedge_exit_pending is still True, it means state wasn't cleared
        if hedge_bought and hedge_exit_pending:
            logging.warning(
                "⚠️ Hedge still active but position is flat - forcing hedge exit"
            )
            return True, "Orphaned hedge cleanup"
        return False, "No position"

    if not hedge_bought:
        return False, "No hedge to exit"

    if not hedge_exit_pending:
        return False, "Hedge exit not pending"

    from src.config import NEXT_DAY_HEDGE_EXIT_TIME

    time_now = current_time.time()

    if time_now >= NEXT_DAY_HEDGE_EXIT_TIME:
        return True, f"Next day hedge exit at {time_now.strftime('%H:%M:%S')}"

    return False, ""


# ============================================================================
# EXPIRY DAY EXIT (FIXED)
# ============================================================================


def check_expiry_exit(position: int, current_expiry: str) -> Tuple[bool, str]:
    """
    Check if should exit due to EXPIRY DAY timing.

    ✅ FIXED: Only exits on expiry day (not every day at 3:15 PM)

    Args:
        position: Current position (0=flat, 1=long, -1=short)
        current_expiry: Expiry date string (DD-MMM-YYYY)

    Returns:
        Tuple of (should_exit, reason)
    """
    if position == 0:
        return False, ""

    current_time = get_current_time()
    current_date = current_time.date()

    # Parse expiry date
    try:
        expiry_date = parse_expiry_date(current_expiry)
    except ValueError:
        logging.error("Invalid expiry format: %s", current_expiry)
        return False, ""

    # ✅ CRITICAL FIX: Only exit on expiry day
    if current_date != expiry_date:
        return False, ""

    # Check time-based exits on expiry day
    time_now = current_time.time()

    if time_now >= EXPIRY_EXIT_FINAL:
        return True, f"Expiry day final exit at {time_now.strftime('%H:%M:%S')}"

    if time_now >= EXPIRY_EXIT_PRIMARY:
        return True, f"Expiry day primary exit at {time_now.strftime('%H:%M:%S')}"

    return False, ""


# ============================================================================
# PNL & POSITION SIZING
# ============================================================================


def calculate_pnl(
    entry_price: float,
    exit_price: float,
    lots: int,
    lot_size: int = LOT_SIZE,
    cost_per_lot: float = COST_PER_LOT,
) -> Tuple[float, float]:
    """Calculate PnL for option trade."""
    if entry_price < 0 or exit_price < 0 or lots <= 0:
        return 0.0, 0.0

    gross_pnl = (exit_price - entry_price) * lot_size * lots
    total_costs = cost_per_lot * lots
    net_pnl = gross_pnl - total_costs

    return net_pnl, total_costs


def calculate_lots(
    broker_capital: Optional[float],
    current_pnl: float,
    initial_lots: int,
    max_lots: int,
    capital_per_lot: float = CAPITAL_PER_LOT,
) -> int:
    """
    Calculate position size based on broker capital or system equity.

    Priority:
    1. Use broker capital if available (LIVE mode)
    2. Fallback to system calculation (initial capital + PnL)

    Args:
        broker_capital: Available capital from broker (or None)
        current_pnl: Current day's PnL
        initial_lots: Minimum lots to trade
        max_lots: Maximum lots allowed
        capital_per_lot: Capital required per lot

    Returns:
        Number of lots to trade
    """
    # Use broker capital if available
    if broker_capital is not None and broker_capital > 0:
        affordable_lots = int(broker_capital / capital_per_lot)
        lots = max(initial_lots, min(affordable_lots, max_lots))

        logging.info(
            "📊 Position sizing (BROKER): Capital=₹%.2f → %d lots (min=%d, max=%d, per_lot=₹%.0f)",
            broker_capital,
            lots,
            initial_lots,
            max_lots,
            capital_per_lot,
        )

        return lots

    # Fallback: Use system calculation
    system_capital = initial_lots * capital_per_lot + current_pnl
    affordable_lots = int(system_capital / capital_per_lot)
    lots = max(initial_lots, min(affordable_lots, max_lots))

    logging.info(
        "📊 Position sizing (SYSTEM): Capital=₹%.2f (PnL=₹%.2f) → %d lots (min=%d, max=%d)",
        system_capital,
        current_pnl,
        lots,
        initial_lots,
        max_lots,
    )

    return lots


def check_risk_limits(consecutive_losses: int, daily_pnl: float) -> Tuple[bool, str]:
    """Check if risk limits are breached."""
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        reason = f"Consecutive losses: {consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}"
        send_risk_alert("consecutive_losses", str(MAX_CONSECUTIVE_LOSSES))
        return True, reason

    if daily_pnl <= -MAX_DAILY_LOSS:
        reason = f"Daily loss: ₹{daily_pnl:,.0f}"
        send_risk_alert("daily_loss", str(round(daily_pnl)))
        return True, reason

    return False, ""


# ============================================================================
# REPORT GENERATION
# ============================================================================


def generate_daily_report(
    trades: List[Dict[str, Any]],
    broker_capital_start: Optional[float] = None,
    broker_capital_end: Optional[float] = None,
    broker_brokerage: float = 0.0,
    open_position: Optional[Dict[str, Any]] = None,
    unrealized_pnl: float = 0.0,
) -> Dict[str, Any]:
    """
    Generate daily performance report with broker capital and open position tracking.

    Args:
        trades: List of trade dictionaries (entries + exits)
        broker_capital_start: Broker capital at day start
        broker_capital_end: Broker capital at day end
        broker_brokerage: Total brokerage charges
        open_position: Dict with open position details (if any)
        unrealized_pnl: Unrealized PnL of open position

    Returns:
        Report dictionary
    """
    # Filter to COMPLETED trades only (exits)
    completed_trades = [t for t in trades if t.get("action") == "EXIT"]

    if not completed_trades:
        report = _empty_report()
        report["broker_capital_start"] = broker_capital_start
        report["broker_capital_end"] = broker_capital_end
        report["broker_brokerage"] = broker_brokerage
        report["has_open_position"] = open_position is not None
        report["open_position"] = open_position
        report["unrealized_pnl"] = unrealized_pnl

        if broker_capital_start is not None:
            report["initial_capital"] = broker_capital_start
        if broker_capital_end is not None:
            report["final_equity"] = broker_capital_end

        return report

    # Calculate stats from COMPLETED trades only
    total_completed = len(completed_trades)
    winning = [t for t in completed_trades if t.get("pnl", 0) > 0]
    losing = [t for t in completed_trades if t.get("pnl", 0) < 0]
    win_rate = len(winning) / total_completed if total_completed > 0 else 0
    realized_pnl = sum(t.get("pnl", 0) for t in completed_trades)

    # Separate LONG and SHORT completed trades
    long_trades = [t for t in completed_trades if t.get("pattern_type") == "bullish"]
    short_trades = [t for t in completed_trades if t.get("pattern_type") == "bearish"]

    # Pattern breakdown
    long_pattern_stats = _calculate_pattern_stats(long_trades)
    short_pattern_stats = _calculate_pattern_stats(short_trades)

    # Capital calculations
    if broker_capital_start is not None:
        initial_capital = broker_capital_start
    else:
        initial_capital = STRATEGY["initial_lots"] * CAPITAL_PER_LOT

    if broker_capital_end is not None:
        final_equity = broker_capital_end
    else:
        final_equity = initial_capital + realized_pnl

    # Calculate drawdown from completed trades
    equity = initial_capital
    max_dd = 0
    peak_equity = initial_capital

    for trade in completed_trades:
        equity += trade.get("pnl", 0)
        peak_equity = max(peak_equity, equity)
        dd = peak_equity - equity
        max_dd = max(max_dd, dd)

    max_dd_pct = (max_dd / initial_capital * 100) if initial_capital > 0 else 0

    # Broker variance calculation
    broker_capital_diff = None
    if broker_capital_start is not None and broker_capital_end is not None:
        broker_capital_diff = broker_capital_end - broker_capital_start

    broker_pnl = broker_capital_diff
    variance = None
    if broker_pnl is not None:
        variance = broker_pnl - realized_pnl

    return {
        "date": get_current_date().strftime("%Y-%m-%d"),
        # Capital
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(final_equity, 2),
        "broker_capital_start": broker_capital_start,
        "broker_capital_end": broker_capital_end,
        "broker_capital_diff": round(broker_capital_diff, 2)
        if broker_capital_diff is not None
        else None,
        "broker_brokerage": round(broker_brokerage, 2),
        "broker_pnl": round(broker_pnl, 2) if broker_pnl is not None else None,
        "variance": round(variance, 2) if variance is not None else None,
        # Completed trades stats
        "completed_trades": total_completed,
        "wins": len(winning),
        "losses": len(losing),
        "win_rate": round(win_rate, 4),
        "realized_pnl": round(realized_pnl, 2),
        # Open position
        "has_open_position": open_position is not None,
        "open_position": open_position,
        "unrealized_pnl": round(unrealized_pnl, 2),
        # Combined
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        # For backward compatibility
        "total_trades": total_completed,
        "total_pnL": round(realized_pnl, 2),
        "avg_trades_per_day": total_completed,
        "max_lots_used": max(
            (t.get("lots", 1) for t in completed_trades),
            default=STRATEGY["initial_lots"],
        ),
        "open_positions": 1 if open_position else 0,
        # Pattern breakdown
        "long_trades": len(long_trades),
        "long_wins": len([t for t in long_trades if t.get("pnl", 0) > 0]),
        "long_losses": len([t for t in long_trades if t.get("pnl", 0) < 0]),
        "long_pattern_stats": long_pattern_stats,
        "short_trades": len(short_trades),
        "short_wins": len([t for t in short_trades if t.get("pnl", 0) > 0]),
        "short_losses": len([t for t in short_trades if t.get("pnl", 0) < 0]),
        "short_pattern_stats": short_pattern_stats,
    }


def _calculate_pattern_stats(trades: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """
    Calculate pattern-level statistics.

    Args:
        trades: List of trade dictionaries

    Returns:
        Dict mapping pattern_name -> {total, wins, losses}
    """
    pattern_stats: Dict[str, Dict[str, int]] = {}

    for trade in trades:
        pattern_name = trade.get("pattern_name", "Unknown")

        if pattern_name not in pattern_stats:
            pattern_stats[pattern_name] = {"total": 0, "wins": 0, "losses": 0}

        pattern_stats[pattern_name]["total"] += 1

        pnl = trade.get("pnl", 0)
        if pnl > 0:
            pattern_stats[pattern_name]["wins"] += 1
        elif pnl < 0:
            pattern_stats[pattern_name]["losses"] += 1

    return pattern_stats


def _empty_report() -> Dict[str, Any]:
    """Return empty report structure with broker data support."""
    return {
        "date": get_current_date().strftime("%Y-%m-%d"),
        "initial_capital": STRATEGY["initial_lots"] * CAPITAL_PER_LOT,
        "final_equity": STRATEGY["initial_lots"] * CAPITAL_PER_LOT,
        "system_equity": STRATEGY["initial_lots"] * CAPITAL_PER_LOT,
        "broker_capital_start": None,
        "broker_capital_end": None,
        "broker_capital_diff": None,
        "broker_brokerage": 0.0,
        "system_pnl": 0.0,
        "broker_pnl": None,
        "variance": None,
        "total_trades": 0,
        "avg_trades_per_day": 0,
        "max_trades_in_a_day": 0,
        "min_trades_in_a_day": 0,
        "win_rate": 0,
        "total_pnL": 0,
        "max_drawdown": 0,
        "max_drawdown_pct": 0,
        "max_lots_used": STRATEGY["initial_lots"],
        "open_positions": 0,
        "long_trades": 0,
        "long_wins": 0,
        "long_losses": 0,
        "long_pattern_stats": {},
        "short_trades": 0,
        "short_wins": 0,
        "short_losses": 0,
        "short_pattern_stats": {},
    }


def save_daily_report(report: Dict[str, Any]) -> bool:
    """Save daily report to JSON and update master summary."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report_file = REPORTS_DIR / f"summary_{get_current_date().strftime('%Y%m%d')}.json"

    try:
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        logging.info("Report saved: %s", report_file)

        master_file = REPORTS_DIR / MASTER_SUMMARY_FILE
        file_exists = master_file.exists()

        headers = list(report.keys())
        with open(master_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            if not file_exists:
                writer.writeheader()
            writer.writerow(report)

        return True

    except (IOError, OSError) as exc:
        logging.error("Failed to save report: %s", exc)
        return False


# ============================================================================
# TRADE EXECUTOR
# ============================================================================


class TradeExecutor:
    """
    Executes synthetic future trades (ATM CE + ATM PE).

    Features:
    - Basket order with sequential fallback
    - Proper partial fill handling
    - Emergency close for failed legs
    """

    def __init__(self, strategy, data_engine, order_manager=None):
        self.strategy = strategy
        self.data_engine = data_engine
        self.order_manager = order_manager

        self.buy_symbol: Optional[str] = None
        self.sell_symbol: Optional[str] = None
        self.buy_entry_price: Optional[float] = None
        self.sell_entry_price: Optional[float] = None

        self.buy_entry_oi = 0
        self.buy_entry_volume = 0
        self.sell_entry_oi = 0
        self.sell_entry_volume = 0

        self.current_expiry: Optional[str] = None

    def execute_entry(
        self, signal: Dict[str, Any], expiry: str, lots: int
    ) -> Optional[Dict[str, Any]]:
        """Execute synthetic future entry (ATM CE + ATM PE)."""
        action = signal["action"]
        nifty_price = signal["price"]
        timestamp = self._ensure_datetime(signal["timestamp"])

        # ✅ NEW: Get symbols from Fyers optionchain API v3
        from src.utils import get_synthetic_future_symbols_from_optionchain_v3

        symbols = get_synthetic_future_symbols_from_optionchain_v3(
            nifty_price=nifty_price,
            signal=action,
            fyers_client=self.data_engine._fyers,
            expiry_str=expiry,
        )

        if not symbols:
            logging.error("❌ Failed to get option symbols from optionchain")
            return None

        buy_symbol = symbols["buy_symbol"]
        sell_symbol = symbols["sell_symbol"]
        buy_price = symbols.get("buy_ltp", 0)
        sell_price = symbols.get("sell_ltp", 0)

        if buy_price <= 0 or sell_price <= 0:
            logging.error(
                "❌ Invalid prices from optionchain: Buy=%.2f, Sell=%.2f",
                buy_price,
                sell_price,
            )
            return None

        if TRADING_MODE == "LIVE" and self.order_manager:
            success, buy_filled, sell_filled = self._execute_entry_basket(
                buy_symbol, sell_symbol, buy_price, sell_price, lots
            )
            if not success:
                logging.error("❌ Entry execution failed")
                return None
            buy_filled_price = buy_filled
            sell_filled_price = sell_filled
        else:
            buy_filled_price = buy_price
            sell_filled_price = sell_price

        self.buy_symbol = buy_symbol
        self.sell_symbol = sell_symbol
        self.buy_entry_price = buy_filled_price
        self.sell_entry_price = sell_filled_price
        self.buy_entry_oi = 0  # Will be updated from quotes if needed
        self.buy_entry_volume = 0
        self.sell_entry_oi = 0
        self.sell_entry_volume = 0

        net_premium = buy_filled_price - sell_filled_price

        logging.info(
            "✅ Synthetic %s Entry: Buy %s @ ₹%.2f | Sell %s @ ₹%.2f | Net: ₹%.2f",
            action,
            symbols["buy_type"],
            buy_filled_price,
            symbols["sell_type"],
            sell_filled_price,
            net_premium,
        )

        return {
            "action": action,
            "trade_number": signal.get("trade_number", 0),
            "timestamp": timestamp,
            "nifty_price": nifty_price,
            "price": nifty_price,
            "buy_symbol": buy_symbol,
            "buy_entry_price": buy_filled_price,
            "buy_entry_oi": self.buy_entry_oi,
            "buy_entry_volume": self.buy_entry_volume,
            "sell_symbol": sell_symbol,
            "sell_entry_price": sell_filled_price,
            "sell_entry_oi": self.sell_entry_oi,
            "sell_entry_volume": self.sell_entry_volume,
            "net_premium": net_premium,
            "atm_strike": symbols["atm_strike"],
            "lots": lots,
            "expiry": expiry,
            "entry_type": signal.get("entry_type", "pattern"),
            "pattern_name": signal.get("pattern_name", "Unknown"),
            "pattern_type": signal.get("pattern_type", ""),
            "pnl": 0,
            "costs": 0,
            "status": "LIVE" if TRADING_MODE == "LIVE" else "PAPER",
            "trading_mode": TRADING_MODE,
        }

    def execute_exit(
        self, signal: Dict[str, Any], expiry: str, lots: int
    ) -> Optional[Dict[str, Any]]:
        """Execute synthetic future exit (close both legs)."""
        if not self.strategy.entry_info:
            logging.warning("⚠️ No entry info for exit")
            return None

        timestamp = self._ensure_datetime(signal["timestamp"])
        entry_timestamp = self._ensure_datetime(self.strategy.entry_info["time"])

        buy_data = self._get_option_data(self.buy_symbol)
        sell_data = self._get_option_data(self.sell_symbol)

        if not buy_data or not sell_data:
            logging.error("❌ Failed to get exit data for synthetic future")
            return None

        buy_exit_price = buy_data["price"]
        sell_exit_price = sell_data["price"]

        if TRADING_MODE == "LIVE" and self.order_manager:
            success, buy_close, sell_close = self._execute_exit_basket(
                self.buy_symbol,
                self.sell_symbol,
                buy_exit_price,
                sell_exit_price,
                lots,
            )
            buy_close_price = buy_close
            sell_close_price = sell_close
        else:
            buy_close_price = buy_exit_price
            sell_close_price = sell_exit_price

        buy_leg_pnl = (buy_close_price - self.buy_entry_price) * LOT_SIZE * lots
        sell_leg_pnl = (self.sell_entry_price - sell_close_price) * LOT_SIZE * lots

        gross_pnl = buy_leg_pnl + sell_leg_pnl
        total_costs = COST_PER_LOT * lots * 2
        net_pnl = gross_pnl - total_costs

        holding_hours = (timestamp - entry_timestamp).total_seconds() / 3600

        logging.info(
            "✅ Synthetic Exit: Buy PnL: ₹%.0f | Sell PnL: ₹%.0f | Net: ₹%.0f",
            buy_leg_pnl,
            sell_leg_pnl,
            net_pnl,
        )

        return {
            "action": "EXIT",
            "trade_number": signal.get("trade_number", 0),
            "timestamp": timestamp,
            "nifty_price": signal["price"],
            "price": signal["price"],
            "entry_price": self.strategy.entry_info["price"],
            "buy_symbol": self.buy_symbol,
            "buy_entry_price": self.buy_entry_price,
            "buy_exit_price": buy_close_price,
            "buy_entry_oi": self.buy_entry_oi,
            "buy_entry_volume": self.buy_entry_volume,
            "buy_exit_oi": buy_data.get("oi", 0),
            "buy_exit_volume": buy_data.get("volume", 0),
            "sell_symbol": self.sell_symbol,
            "sell_entry_price": self.sell_entry_price,
            "sell_exit_price": sell_close_price,
            "sell_entry_oi": self.sell_entry_oi,
            "sell_entry_volume": self.sell_entry_volume,
            "sell_exit_oi": sell_data.get("oi", 0),
            "sell_exit_volume": sell_data.get("volume", 0),
            "buy_leg_pnl": round(buy_leg_pnl, 2),
            "sell_leg_pnl": round(sell_leg_pnl, 2),
            "pnl": round(net_pnl, 2),
            "costs": round(total_costs, 2),
            "lots": lots,
            "expiry": expiry,
            "holding_hours": round(holding_hours, 2),
            "entry_type": signal.get("entry_type", "pattern"),
            "pattern_name": self.strategy.entry_info.get("pattern_name", "Unknown"),
            "pattern_type": self.strategy.entry_info.get("pattern_type", ""),
            "exit_reason": signal.get("exit_reason", "trailing_stop"),
            "status": "LIVE" if TRADING_MODE == "LIVE" else "PAPER",
            "trading_mode": TRADING_MODE,
        }

    def _execute_entry_basket(
        self,
        buy_symbol: str,
        sell_symbol: str,
        buy_price: float,
        sell_price: float,
        lots: int,
    ) -> Tuple[bool, float, float]:
        """Execute synthetic future entry with proper partial fill handling."""
        quantity = lots * LOT_SIZE

        logging.info(
            "🔄 ENTRY: BUY %s + SELL %s (qty=%d)", buy_symbol, sell_symbol, quantity
        )

        buy_filled = False
        sell_filled = False
        buy_filled_price = buy_price
        sell_filled_price = sell_price

        basket = [
            {
                "symbol": buy_symbol,
                "side": OrderSide.BUY,
                "quantity": quantity,
                "type": 2,
            },
            {
                "symbol": sell_symbol,
                "side": OrderSide.SELL,
                "quantity": quantity,
                "type": 2,
            },
        ]

        orders = self.order_manager.place_basket_orders(basket)

        if len(orders) >= 2:
            buy_order, sell_order = orders[0], orders[1]
            orders = self.order_manager.wait_for_basket_fill(orders, timeout_seconds=30)

            if buy_order.order_id and (buy_order.is_complete or buy_order.is_partial):
                buy_filled = True
                buy_filled_price = (
                    buy_order.filled_price if buy_order.filled_price > 0 else buy_price
                )

            if sell_order.order_id and (
                sell_order.is_complete or sell_order.is_partial
            ):
                sell_filled = True
                sell_filled_price = (
                    sell_order.filled_price
                    if sell_order.filled_price > 0
                    else sell_price
                )

        if buy_filled and sell_filled:
            return True, buy_filled_price, sell_filled_price

        if not buy_filled and not sell_filled:
            return self._execute_entry_sequential(
                buy_symbol, sell_symbol, buy_price, sell_price, lots
            )

        if buy_filled and not sell_filled:
            sell_order = self.order_manager.place_market_order(
                sell_symbol, OrderSide.SELL, quantity
            )
            if sell_order.order_id:
                sell_order = self.order_manager.wait_for_fill(
                    sell_order, timeout_seconds=30
                )
            if sell_order.is_complete or sell_order.is_partial:
                sell_filled_price = (
                    sell_order.filled_price
                    if sell_order.filled_price > 0
                    else sell_price
                )
                return True, buy_filled_price, sell_filled_price
            self._emergency_close_leg(buy_symbol, OrderSide.SELL, lots)
            return False, buy_price, sell_price

        if sell_filled and not buy_filled:
            buy_order = self.order_manager.place_market_order(
                buy_symbol, OrderSide.BUY, quantity
            )
            if buy_order.order_id:
                buy_order = self.order_manager.wait_for_fill(
                    buy_order, timeout_seconds=30
                )
            if buy_order.is_complete or buy_order.is_partial:
                buy_filled_price = (
                    buy_order.filled_price if buy_order.filled_price > 0 else buy_price
                )
                return True, buy_filled_price, sell_filled_price
            self._emergency_close_leg(sell_symbol, OrderSide.BUY, lots)
            return False, buy_price, sell_price

        return False, buy_price, sell_price

    def _execute_entry_sequential(
        self,
        buy_symbol: str,
        sell_symbol: str,
        buy_price: float,
        sell_price: float,
        lots: int,
    ) -> Tuple[bool, float, float]:
        """Execute entry using sequential orders (fallback)."""
        quantity = lots * LOT_SIZE

        buy_order = self.order_manager.place_market_order(
            buy_symbol, OrderSide.BUY, quantity
        )
        if not buy_order.order_id:
            return False, buy_price, sell_price

        buy_order = self.order_manager.wait_for_fill(buy_order, timeout_seconds=30)
        if not (buy_order.is_complete or buy_order.is_partial):
            if buy_order.order_id:
                self.order_manager.cancel_order(buy_order.order_id)
            return False, buy_price, sell_price

        buy_filled_price = (
            buy_order.filled_price if buy_order.filled_price > 0 else buy_price
        )

        sell_order = self.order_manager.place_market_order(
            sell_symbol, OrderSide.SELL, quantity
        )
        if not sell_order.order_id:
            self._emergency_close_leg(buy_symbol, OrderSide.SELL, lots)
            return False, buy_price, sell_price

        sell_order = self.order_manager.wait_for_fill(sell_order, timeout_seconds=30)
        if not (sell_order.is_complete or sell_order.is_partial):
            if sell_order.order_id:
                self.order_manager.cancel_order(sell_order.order_id)
            self._emergency_close_leg(buy_symbol, OrderSide.SELL, lots)
            return False, buy_price, sell_price

        sell_filled_price = (
            sell_order.filled_price if sell_order.filled_price > 0 else sell_price
        )

        return True, buy_filled_price, sell_filled_price

    def _execute_exit_basket(
        self,
        buy_symbol: str,
        sell_symbol: str,
        buy_price: float,
        sell_price: float,
        lots: int,
    ) -> Tuple[bool, float, float]:
        """Execute synthetic future exit with retries for each leg."""
        quantity = lots * LOT_SIZE

        buy_closed = False
        sell_closed = False
        buy_close_price = buy_price
        sell_close_price = sell_price

        basket = [
            {
                "symbol": buy_symbol,
                "side": OrderSide.SELL,
                "quantity": quantity,
                "type": 2,
            },
            {
                "symbol": sell_symbol,
                "side": OrderSide.BUY,
                "quantity": quantity,
                "type": 2,
            },
        ]

        orders = self.order_manager.place_basket_orders(basket)

        if len(orders) >= 2:
            close_buy_order, close_sell_order = orders[0], orders[1]
            orders = self.order_manager.wait_for_basket_fill(orders, timeout_seconds=30)

            if close_buy_order.is_complete or close_buy_order.is_partial:
                buy_closed = True
                buy_close_price = (
                    close_buy_order.filled_price
                    if close_buy_order.filled_price > 0
                    else buy_price
                )

            if close_sell_order.is_complete or close_sell_order.is_partial:
                sell_closed = True
                sell_close_price = (
                    close_sell_order.filled_price
                    if close_sell_order.filled_price > 0
                    else sell_price
                )

        if buy_closed and sell_closed:
            return True, buy_close_price, sell_close_price

        if not buy_closed:
            for attempt in range(3):
                order = self.order_manager.place_market_order(
                    buy_symbol, OrderSide.SELL, quantity
                )
                if order.order_id:
                    order = self.order_manager.wait_for_fill(order, timeout_seconds=20)
                    if order.is_complete or order.is_partial:
                        buy_closed = True
                        buy_close_price = (
                            order.filled_price if order.filled_price > 0 else buy_price
                        )
                        break

        if not sell_closed:
            for attempt in range(3):
                order = self.order_manager.place_market_order(
                    sell_symbol, OrderSide.BUY, quantity
                )
                if order.order_id:
                    order = self.order_manager.wait_for_fill(order, timeout_seconds=20)
                    if order.is_complete or order.is_partial:
                        sell_closed = True
                        sell_close_price = (
                            order.filled_price if order.filled_price > 0 else sell_price
                        )
                        break

        if not (buy_closed and sell_closed):
            logging.error("🚨 PARTIAL EXIT - Manual intervention may be needed!")

        return True, buy_close_price, sell_close_price

    def _emergency_close_leg(self, symbol: str, side: OrderSide, lots: int) -> bool:
        """Emergency close a single leg with multiple retries."""
        quantity = lots * LOT_SIZE
        logging.error("🚨 EMERGENCY CLOSE: %s %s qty=%d", side.name, symbol, quantity)

        for attempt in range(3):
            try:
                order = self.order_manager.place_market_order(symbol, side, quantity)
                if order.order_id:
                    order = self.order_manager.wait_for_fill(order, timeout_seconds=30)
                    if order.is_complete or order.is_partial:
                        logging.info(
                            "✅ Emergency close SUCCESS (attempt %d)", attempt + 1
                        )
                        return True
            except Exception as exc:
                logging.error("Emergency close attempt %d failed: %s", attempt + 1, exc)
            time_module.sleep(2)

        logging.error("🚨 EMERGENCY CLOSE FAILED - MANUAL INTERVENTION REQUIRED!")
        send_risk_alert(
            "emergency_close_failed",
            f"Failed to close {symbol} {side.name} {quantity} qty",
        )
        return False

    def _get_option_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get option price, OI, Volume from quotes API."""
        try:
            quotes = self.data_engine.get_quotes([symbol])
            if symbol in quotes:
                quote = quotes[symbol]
                return {
                    "price": quote.get("ltp", 0),
                    "bid": quote.get("bid", 0),
                    "ask": quote.get("ask", 0),
                    "oi": quote.get("oi", 0),
                    "volume": quote.get("volume", 0),
                    "symbol": symbol,
                }
            return None
        except Exception as exc:
            logging.error("Failed to fetch quote for %s: %s", symbol, exc)
            return None

    # def _get_expiry_timestamp_from_fyers(self) -> str:
    #     """Get expiry timestamp from Fyers API."""
    #     try:
    #         expiry_str = self.current_expiry
    #         if not expiry_str:
    #             return ""

    #         from src.utils import parse_expiry_date

    #         our_expiry_date = parse_expiry_date(expiry_str)

    #         response = self.data_engine._fyers.optionchain(
    #             {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""}
    #         )

    #         if response.get("code") != 200:
    #             return ""

    #         expiry_data = response.get("data", {}).get("expiryData", [])

    #         for expiry_item in expiry_data:
    #             fyers_date_str = expiry_item.get("date")
    #             fyers_timestamp = expiry_item.get("expiry")

    #             if not fyers_date_str or not fyers_timestamp:
    #                 continue

    #             fyers_date = datetime.strptime(fyers_date_str, "%d-%m-%Y").date()

    #             if fyers_date == our_expiry_date:
    #                 return str(fyers_timestamp)

    #         return ""

    #     except Exception:
    #         return ""

    # def _find_option_in_cache(self, symbol: str) -> Optional[Dict[str, Any]]:
    #     """Find option in cached optionchain response."""
    #     if not self._optionchain_cache:
    #         return None

    #     parsed = parse_option_symbol(symbol)
    #     if not parsed:
    #         return None

    #     target_strike = parsed["strike"]
    #     option_type = parsed["option_type"]

    #     try:
    #         options_chain = self._optionchain_cache.get("optionsChain", [])

    #         for strike_data in options_chain:
    #             strike_price = strike_data.get("strike_price", 0)
    #             data_option_type = strike_data.get("option_type", "")

    #             if strike_price == target_strike and data_option_type == option_type:
    #                 ltp = strike_data.get("ltp", 0)
    #                 if ltp <= 0:
    #                     return None

    #                 return {
    #                     "price": ltp,
    #                     "oi": strike_data.get("oi", 0),
    #                     "volume": strike_data.get("volume", 0),
    #                     "symbol": strike_data.get("symbol", symbol),
    #                 }

    #         return None

    #     except Exception:
    #         return None

    @staticmethod
    def _ensure_datetime(timestamp) -> datetime:
        """Ensure timestamp is timezone-aware datetime (IST)."""
        from src.config import IST

        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                return IST.localize(timestamp)
            return timestamp.astimezone(IST)

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

        if hasattr(timestamp, "to_pydatetime"):
            dt = timestamp.to_pydatetime()
            if dt.tzinfo is None:
                return IST.localize(dt)
            return dt.astimezone(IST)

        from src.config import get_current_time

        return get_current_time()


# ============================================================================
# MAIN TRADER CLASS
# ============================================================================


class Trader:
    """
    Main trading system orchestrator.

    Manages data fetching, strategy execution, trade execution,
    state persistence, and risk management.
    """

    def __init__(self):
        global _trader_instance
        _trader_instance = self

        self.running = True
        self.startup_time = get_current_time()

        for d in [DATA_DIR, LOG_DIR, TRADES_DIR, REPORTS_DIR, STATE_DIR, TOKEN_DIR]:
            d.mkdir(parents=True, exist_ok=True)

        from src.config import (
            USE_FUTURES_DATA,
            get_trading_symbol,
            get_current_futures_month,
        )

        if USE_FUTURES_DATA:
            symbol = get_trading_symbol()
            futures_month = get_current_futures_month()
            logging.info("DATA SOURCE: FUTURES | Symbol: %s", symbol)
            print(
                f"\n✅ Using Futures Data: {symbol} ({futures_month.strftime('%B %Y')})\n"
            )
        else:
            logging.info("DATA SOURCE: INDEX (NSE:NIFTY50-INDEX)")
            print("\n✅ Using Index Data (NSE:NIFTY50-INDEX)\n")

        # Position state
        self.position = 0
        self.entry_info: Optional[Dict[str, Any]] = None
        self.option_symbol: Optional[str] = None
        self.entry_option_price: Optional[float] = None
        self.current_option_price: Optional[float] = None

        # Synthetic future tracking
        self.buy_symbol: Optional[str] = None
        self.sell_symbol: Optional[str] = None
        self.buy_entry_price: Optional[float] = None
        self.sell_entry_price: Optional[float] = None

        # ✅ NEW: Hedge tracking
        self.hedge_symbol: Optional[str] = None
        self.hedge_entry_price: Optional[float] = None
        self.hedge_bought: bool = False
        self.hedge_exit_pending: bool = False  # Flag for next-day hedge exit

        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.trades_today: List[Dict[str, Any]] = []
        self.trade_number = 0
        self.current_lots = STRATEGY["initial_lots"]

        # ✅ ADD THESE NEW LINES (after self.current_lots = ...)
        self.broker_capital_start: Optional[float] = None
        self.broker_capital_end: Optional[float] = None
        self.broker_brokerage_today: float = 0.0

        # Bar storage for export (NOT in strategy)
        self._all_bars: List[Dict[str, Any]] = []

        self._stored_expiry: Optional[str] = None
        self._last_state_save = get_current_time()
        self._last_status_display = get_current_time()

        # Initialize expiry before components
        self.current_expiry = calculate_expiry_for_date(get_current_date(), None)
        self.fyers_client = None

        # Initialize components
        self._init_components()

        # Properly initialize expiry with Fyers client
        self.current_expiry = self._initialize_expiry()

        # Load previous state
        self._load_state()

        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        atexit.register(self.cleanup)

    def _init_components(self):
        """
        Initialize trading components with broker verification.

        Order of operations:
        1. Initialize DataEngine (handles authentication)
        2. Initialize Strategy
        3. Initialize OrderManager (LIVE mode only)
        4. Initialize TradeExecutor
        5. Load previous state from file
        6. Verify position with broker (LIVE mode only)
        7. Load warmup data and build Renko bricks
        8. Display initial status
        9. Record broker capital at start
        """
        # ================================================================
        # STEP 1: Initialize Data Engine (handles Fyers authentication)
        # ================================================================
        from src.data import DataEngine

        self.data_engine = DataEngine()

        # ================================================================
        # STEP 2: Initialize Strategy
        # ================================================================
        from src.strategy import RenkoStrategy

        self.strategy = RenkoStrategy(STRATEGY)

        # ================================================================
        # STEP 3: Initialize Order Manager (LIVE mode only)
        # ================================================================
        self.order_manager = None
        if TRADING_MODE == "LIVE":
            from src.trading import OrderManager

            fyers_client = self.data_engine.get_fyers_client()
            if fyers_client:
                self.order_manager = OrderManager(fyers_client, LOT_SIZE)
                logging.info("✅ OrderManager initialized (LIVE mode)")
            else:
                logging.error("❌ Failed to get Fyers client for OrderManager")

        # ================================================================
        # STEP 4: Initialize Trade Executor
        # ================================================================
        self.executor = TradeExecutor(
            self.strategy, self.data_engine, self.order_manager
        )
        self.executor.current_expiry = self.current_expiry

        # Store Fyers client reference
        self.fyers_client = self.data_engine.get_fyers_client()

        # ================================================================
        # STEP 5: Load previous state from file
        # ================================================================
        self._load_state()

        # ================================================================
        # STEP 6: Verify position with broker (LIVE mode only)
        # This ensures state file matches actual broker positions
        # ================================================================
        self._verify_position_with_broker()

        # ================================================================
        # STEP 7: Load warmup data and build initial Renko bricks
        # ================================================================
        self._load_warmup_data()

        # ================================================================
        # STEP 8: Display initial status
        # ================================================================
        state = self.strategy.get_current_state()
        if state:
            print(f"\n{'=' * 80}")
            print("📊 INITIAL STATUS (After Warmup)")
            print(f"   Nifty Price: {state.get('nifty_price', 0):,.2f}")
            print(f"   Total Renko Bricks: {state.get('total_bricks', 0)}")
            print(
                f"   Pattern Detection: "
                f"{'✅ Ready' if state.get('total_bricks', 0) >= 6 else '⏳ Waiting'}"
            )
            print(f"   Last Pattern: {state.get('last_pattern_name', 'None')}")
            print(f"   Brick Size: {state.get('brick_size', 0):.2f}")
            print(f"   🟢 GREEN threshold: {state.get('green_threshold', 0):,.2f}")
            print(f"   🔴 RED threshold: {state.get('red_threshold', 0):,.2f}")

            # Show indicator status
            if state.get("indicators_ready"):
                print(f"   📈 RSI: {state.get('renko_rsi', 0):.2f}")
                print(f"   📈 MA: {state.get('renko_ma', 0):,.2f}")
            else:
                print(f"   📈 Indicators: Warming up...")

            # Show position status
            if self.position != 0:
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"   💼 Position: {direction} ({self.current_lots} lots)")
            else:
                print(f"   💼 Position: FLAT")

            print(f"{'=' * 80}\n")

        # ================================================================
        # STEP 9: Record broker capital at start of day
        # ================================================================
        self._record_broker_capital_start()

    def _load_warmup_data(self):
        """Load warmup data and build initial Renko bricks (ONE TIME)."""
        logging.info("Loading warmup data...")

        warmup_bars = self.data_engine.get_warmup_data(days=3)

        if warmup_bars:
            # Store bars for export
            self._all_bars.extend(warmup_bars)

            # Build initial bricks (ONE TIME — never rebuilt)
            self.strategy.initialize_from_warmup(warmup_bars)

            logging.info(
                "Warmup: %d bars → %d Renko bricks",
                len(warmup_bars),
                len(self.strategy.renko_bricks),
            )
        else:
            logging.warning("No warmup data available")

    def _initialize_expiry(self) -> str:
        """Initialize expiry at startup."""
        from src.utils import parse_expiry_date

        today = get_current_date()

        stored_expiry = getattr(self, "_stored_expiry", None)

        if stored_expiry:
            try:
                expiry_date = parse_expiry_date(stored_expiry)
                if expiry_date > today or (
                    expiry_date == today and get_current_time().time() < dtime(15, 15)
                ):
                    logging.info("EXPIRY FROM STATE: %s", stored_expiry)
                    if hasattr(self, "executor"):
                        self.executor.current_expiry = stored_expiry
                    return stored_expiry
            except ValueError as exc:
                logging.error("Invalid stored expiry '%s': %s", stored_expiry, exc)

        if self.position != 0 and self.entry_info:
            old_expiry = self.entry_info.get("expiry")
            if old_expiry:
                try:
                    expiry_date = parse_expiry_date(old_expiry)
                    if expiry_date > today:
                        self._stored_expiry = old_expiry
                        if hasattr(self, "executor"):
                            self.executor.current_expiry = old_expiry
                        return old_expiry
                except ValueError:
                    pass

        expiry_str = self._get_expiry_from_fyers()

        if not expiry_str:
            expiry_date = calculate_next_expiry(today)
            expiry_str = expiry_date.strftime(DATE_FORMAT_EXPIRY)

        self._stored_expiry = expiry_str

        logging.info("EXPIRY INITIALIZED: %s", expiry_str)

        if hasattr(self, "executor"):
            self.executor.current_expiry = expiry_str

        return expiry_str

    def _fetch_broker_capital(self) -> Optional[float]:
        """
        Fetch current available capital from broker with multiple fallback options.

        Priority order:
        1. "Available Balance" (most accurate for trading)
        2. "Clear Balance" (fallback if Available Balance not found)
        3. "Total Balance" (last resort)

        Returns:
            Available capital or None if fetch fails
        """
        try:
            if not self.order_manager or TRADING_MODE != "LIVE":
                logging.debug("Broker capital fetch skipped (not in LIVE mode)")
                return None

            fyers = self.order_manager.fyers
            response = fyers.funds()

            if not response:
                logging.error("❌ Empty response from fyers.funds()")
                return None

            code = response.get("code")
            status = response.get("s", "")
            message = response.get("message", "")

            if code != 200 or status != "ok":
                logging.error(
                    "❌ Funds API failed: code=%s, status=%s, message=%s",
                    code,
                    status,
                    message,
                )
                return None

            fund_limit = response.get("fund_limit", [])
            if not fund_limit:
                logging.error("❌ No fund_limit in response")
                return None

            # Log all fund segments for debugging (only in DEBUG mode)
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                logging.debug("Fund segments received:")
                for segment in fund_limit:
                    logging.debug(
                        "  - %s: ₹%.2f",
                        segment.get("title", "N/A"),
                        segment.get("equityAmount", 0),
                    )

            # Build lookup dictionary (case-insensitive)
            funds_dict = {}
            for segment in fund_limit:
                title = segment.get("title", "").strip()
                equity_amount = segment.get("equityAmount", 0)
                if title:
                    funds_dict[title.lower()] = equity_amount

            # Priority 1: Available Balance
            if "available balance" in funds_dict:
                available = funds_dict["available balance"]
                if available > 0:
                    logging.info(
                        "✅ Broker capital: ₹%.2f (Available Balance)", available
                    )
                    return float(available)
                else:
                    logging.warning(
                        "⚠️ Available Balance is %.2f (non-positive)", available
                    )

            # Priority 2: Clear Balance
            if "clear balance" in funds_dict:
                clear = funds_dict["clear balance"]
                if clear > 0:
                    logging.info(
                        "✅ Broker capital: ₹%.2f (Clear Balance - fallback)", clear
                    )
                    return float(clear)

            # Priority 3: Total Balance
            if "total balance" in funds_dict:
                total = funds_dict["total balance"]
                if total > 0:
                    logging.warning(
                        "⚠️ Using Total Balance: ₹%.2f (Available/Clear not found)",
                        total,
                    )
                    return float(total)

            # Nothing found
            logging.error(
                "❌ No valid balance found. Available titles: %s",
                ", ".join(funds_dict.keys()),
            )
            return None

        except KeyError as exc:
            logging.error("❌ Missing key in funds response: %s", exc)
            return None
        except (ValueError, TypeError) as exc:
            logging.error("❌ Invalid data type in funds response: %s", exc)
            return None
        except Exception as exc:
            logging.error(
                "❌ Unexpected error fetching broker capital: %s", exc, exc_info=True
            )
            return None

    def _fetch_broker_brokerage(self) -> float:
        """
        Fetch today's brokerage charges from broker.

        Returns:
            Total brokerage charges for today (0 if unavailable)
        """
        try:
            if not self.order_manager or TRADING_MODE != "LIVE":
                logging.debug("Brokerage fetch skipped (not in LIVE mode)")
                return 0.0

            fyers = self.order_manager.fyers

            # Get today's tradebook
            response = fyers.tradebook()

            if not response or response.get("code") != 200:
                logging.warning("Failed to fetch tradebook for brokerage: %s", response)
                return 0.0

            tradebook = response.get("tradeBook", [])
            if not tradebook:
                logging.info("No trades in tradebook today")
                return 0.0

            # Sum up all charges for today's trades
            total_brokerage = 0.0
            trade_count = 0

            for trade in tradebook:
                # Get various charge components
                brokerage = float(trade.get("brokerage", 0) or 0)
                stt = float(trade.get("stt", 0) or 0)
                exchange_charges = float(trade.get("exchangeTxnCharge", 0) or 0)
                sebi_charges = float(trade.get("sebiTurnoverFee", 0) or 0)
                stamp_duty = float(trade.get("stampDuty", 0) or 0)
                gst = float(trade.get("gst", 0) or 0)

                # Total charges for this trade
                trade_charges = (
                    brokerage + stt + exchange_charges + sebi_charges + stamp_duty + gst
                )

                total_brokerage += trade_charges
                trade_count += 1

                logging.debug(
                    "Trade %d charges: Brokerage=%.2f, STT=%.2f, Exchange=%.2f, "
                    "SEBI=%.2f, Stamp=%.2f, GST=%.2f, Total=%.2f",
                    trade_count,
                    brokerage,
                    stt,
                    exchange_charges,
                    sebi_charges,
                    stamp_duty,
                    gst,
                    trade_charges,
                )

            if total_brokerage > 0:
                logging.info(
                    "✅ Total brokerage/charges from %d trades: ₹%.2f",
                    trade_count,
                    total_brokerage,
                )
            else:
                logging.info("No brokerage charges found in tradebook")

            return total_brokerage

        except Exception as exc:
            logging.error("Failed to fetch broker brokerage: %s", exc, exc_info=True)
            return 0.0

    def _record_broker_capital_start(self) -> None:
        """Record broker capital at day start with enhanced logging."""
        capital = self._fetch_broker_capital()

        if capital is not None:
            self.broker_capital_start = capital
            logging.info("📊 Day Start Capital (BROKER): ₹%.2f", capital)

            print(f"\n{'=' * 80}")
            print(f"📊 BROKER CAPITAL (Day Start)")
            print(f"   Available: ₹{capital:,.2f}")
            print(f"   Source: Fyers API (Available Balance)")
            print(f"   Mode: LIVE")
            print(f"{'=' * 80}\n")
        else:
            # Fallback to configured capital
            self.broker_capital_start = STRATEGY["initial_lots"] * CAPITAL_PER_LOT

            logging.warning(
                "⚠️ Broker capital unavailable - using configured: ₹%.2f",
                self.broker_capital_start,
            )

            print(f"\n{'=' * 80}")
            print(f"⚠️ BROKER CAPITAL UNAVAILABLE")
            print(f"   Using Configured: ₹{self.broker_capital_start:,.2f}")
            print(f"   Reason: Not in LIVE mode or API error")
            print(f"   Mode: {TRADING_MODE}")
            print(f"{'=' * 80}\n")

    def _get_expiry_from_fyers(self) -> Optional[str]:
        """Get next valid expiry from Fyers API."""
        if not self.fyers_client:
            return None

        try:
            response = self.fyers_client.optionchain(
                {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""}
            )

            if response.get("code") != 200:
                return None

            expiry_data = response.get("data", {}).get("expiryData", [])
            if not expiry_data:
                return None

            today = get_current_date()

            for expiry_item in expiry_data:
                expiry_date_str = expiry_item.get("date")
                if not expiry_date_str:
                    continue

                try:
                    expiry_date = datetime.strptime(expiry_date_str, "%d-%m-%Y").date()
                    if expiry_date <= today:
                        continue
                    expiry_str = expiry_date.strftime(DATE_FORMAT_EXPIRY)
                    logging.info("✅ Expiry from Fyers: %s", expiry_str)
                    return expiry_str
                except ValueError:
                    continue

            return None

        except Exception as exc:
            logging.error("Exception getting expiry from Fyers: %s", exc)
            return None

    def _recalculate_expiry(self) -> None:
        """Recalculate expiry (called after exit)."""
        old_expiry = self.current_expiry

        new_expiry = self._get_expiry_from_fyers()

        if not new_expiry:
            from src.utils import calculate_next_expiry

            expiry_date = calculate_next_expiry(get_current_date())
            new_expiry = expiry_date.strftime(DATE_FORMAT_EXPIRY)

        self.current_expiry = new_expiry
        self._stored_expiry = new_expiry

        if hasattr(self, "executor"):
            self.executor.current_expiry = new_expiry

        if old_expiry != new_expiry:
            logging.info("EXPIRY UPDATED: %s → %s", old_expiry, new_expiry)

    def _load_state(self):
        """Load previous state from file (verification happens separately)."""
        state = load_state()
        if not state:
            return

        # Load position state
        self.position = state.get("position", 0)
        self.entry_info = state.get("entry_info")
        self.option_symbol = state.get("option_symbol")

        self.buy_symbol = state.get("buy_symbol")
        self.sell_symbol = state.get("sell_symbol")
        self.buy_entry_price = state.get("buy_entry_price")
        self.sell_entry_price = state.get("sell_entry_price")

        # Load hedge state
        self.hedge_symbol = state.get("hedge_symbol")
        self.hedge_entry_price = state.get("hedge_entry_price")
        self.hedge_bought = state.get("hedge_bought", False)
        self.hedge_exit_pending = state.get("hedge_exit_pending", False)

        # Recover option_symbol from buy_symbol if missing
        if self.position != 0 and not self.option_symbol and self.buy_symbol:
            self.option_symbol = self.buy_symbol
            logging.info("✅ Recovered option_symbol: %s", self.option_symbol)

        self.entry_option_price = state.get("entry_option_price")

        if self.position != 0 and not self.entry_option_price and self.buy_entry_price:
            self.entry_option_price = self.buy_entry_price
            logging.info(
                "✅ Recovered entry_option_price: ₹%.2f", self.entry_option_price
            )

        self.current_option_price = state.get("current_option_price")
        self.daily_pnl = state.get("daily_pnl", 0)
        self.consecutive_losses = state.get("consecutive_losses", 0)
        self.trades_today = state.get("trades_today", [])
        self.trade_number = state.get("trade_number", 0)
        self.current_lots = state.get("current_lots", STRATEGY["initial_lots"])

        self._stored_expiry = state.get("stored_expiry")
        if self._stored_expiry:
            logging.info("Loaded stored expiry: %s", self._stored_expiry)

        # Sync with strategy
        self.strategy.position = self.position
        self.strategy.entry_info = self.entry_info
        self.strategy.peak_price = state.get("peak_price")
        self.strategy.trough_price = state.get("trough_price")

        # Load into executor
        if self.position != 0 and hasattr(self, "executor"):
            self.executor.buy_symbol = self.buy_symbol
            self.executor.sell_symbol = self.sell_symbol
            self.executor.buy_entry_price = self.buy_entry_price
            self.executor.sell_entry_price = self.sell_entry_price
            self.executor.buy_entry_oi = state.get("buy_entry_oi", 0)
            self.executor.buy_entry_volume = state.get("buy_entry_volume", 0)
            self.executor.sell_entry_oi = state.get("sell_entry_oi", 0)
            self.executor.sell_entry_volume = state.get("sell_entry_volume", 0)

        # Display loaded position (verification happens later)
        if self.position != 0:
            direction = "LONG" if self.position == 1 else "SHORT"
            print(f"\n{'=' * 80}")
            print(
                f"💼 LOADED {direction} POSITION FROM STATE ({self.current_lots} lots)"
            )

            if self.buy_symbol and self.sell_symbol:
                print(f"   📗 BUY:  {self.buy_symbol} @ ₹{self.buy_entry_price:.2f}")
                print(f"   📕 SELL: {self.sell_symbol} @ ₹{self.sell_entry_price:.2f}")

                if self.hedge_bought and self.hedge_symbol:
                    print(
                        f"   🛡️ HEDGE: {self.hedge_symbol} @ ₹{self.hedge_entry_price:.2f}"
                    )
                    if self.hedge_exit_pending:
                        print(f"   ⏰ Hedge exit pending at 9:16 AM")

            if self.strategy.peak_price:
                print(f"   📈 Peak: {self.strategy.peak_price:.2f}")
            if self.strategy.trough_price:
                print(f"   📉 Trough: {self.strategy.trough_price:.2f}")

            print(f"{'=' * 80}")
            print(f"⚠️ Will verify with broker after authentication...")
            print(f"{'=' * 80}\n")

        send_system_alert("info", "Loaded previous state")

    def _verify_position_with_broker(self):
        """
        Verify loaded position state with broker.

        Called after authentication to ensure state matches broker.
        """
        if TRADING_MODE != "LIVE" or not self.order_manager:
            logging.info("Skipping broker verification (not in LIVE mode)")
            return

        if self.position == 0:
            logging.info("No position to verify")
            return

        logging.info("=" * 80)
        logging.info("🔄 VERIFYING POSITION WITH BROKER")
        logging.info("=" * 80)

        try:
            response = self.order_manager.fyers.positions()

            if not response or response.get("code") != 200:
                logging.error("❌ Failed to fetch broker positions")
                return

            net_positions = response.get("netPositions", [])

            # Build dict of broker positions
            broker_positions = {}
            for pos in net_positions:
                symbol = pos.get("symbol")
                net_qty = pos.get("netQty", 0)
                if net_qty != 0:
                    broker_positions[symbol] = net_qty

            # Verify main position legs
            buy_exists = (
                self.buy_symbol in broker_positions
                and broker_positions[self.buy_symbol] > 0
            )
            sell_exists = (
                self.sell_symbol in broker_positions
                and broker_positions[self.sell_symbol] < 0
            )

            if buy_exists and sell_exists:
                logging.info("✅ Main position verified at broker")

                # Verify hedge
                if self.hedge_bought and self.hedge_symbol:
                    hedge_exists = (
                        self.hedge_symbol in broker_positions
                        and broker_positions[self.hedge_symbol] > 0
                    )

                    if hedge_exists:
                        logging.info("✅ Hedge position verified at broker")
                    else:
                        logging.warning(
                            "⚠️ Hedge NOT found at broker - clearing hedge state"
                        )
                        self.hedge_symbol = None
                        self.hedge_entry_price = None
                        self.hedge_bought = False
                        self.hedge_exit_pending = False
                        self._save_state()

                # Update current prices
                self._update_position_ltp()

                # Display verified position
                direction = "LONG" if self.position == 1 else "SHORT"
                print(f"\n{'=' * 80}")
                print(
                    f"✅ VERIFIED {direction} POSITION WITH BROKER ({self.current_lots} lots)"
                )
                print(f"   📗 BUY:  {self.buy_symbol} @ ₹{self.buy_entry_price:.2f}")
                print(f"   📕 SELL: {self.sell_symbol} @ ₹{self.sell_entry_price:.2f}")

                if self.hedge_bought and self.hedge_symbol:
                    print(
                        f"   🛡️ HEDGE: {self.hedge_symbol} @ ₹{self.hedge_entry_price:.2f}"
                    )
                    if self.hedge_exit_pending:
                        print(f"   ⏰ Hedge exit pending at 9:16 AM")

                print(f"{'=' * 80}\n")

            else:
                logging.error("❌ POSITION MISMATCH WITH BROKER!")
                logging.error(
                    "   State shows: BUY=%s, SELL=%s", self.buy_symbol, self.sell_symbol
                )
                logging.error("   Broker has: %s", broker_positions)

                # Check if position was closed externally
                if not buy_exists and not sell_exists:
                    logging.error("🚨 Position appears CLOSED at broker!")

                    print(f"\n{'=' * 80}")
                    print(f"🚨 POSITION MISMATCH!")
                    print(f"   State shows OPEN position but broker shows FLAT")
                    print(f"   This could mean position was closed manually/externally")
                    print(f"{'=' * 80}\n")

                    # Send alert
                    send_risk_alert(
                        "position_mismatch",
                        f"State shows {self.buy_symbol}/{self.sell_symbol} but broker is FLAT",
                    )

                    # Reset state to match broker
                    logging.warning("Resetting state to match broker (FLAT)")
                    self.position = 0
                    self.entry_info = None
                    self.option_symbol = None
                    self.entry_option_price = None
                    self.current_option_price = None
                    self.buy_symbol = None
                    self.sell_symbol = None
                    self.buy_entry_price = None
                    self.sell_entry_price = None
                    self.hedge_symbol = None
                    self.hedge_entry_price = None
                    self.hedge_bought = False
                    self.hedge_exit_pending = False

                    self.strategy.position = 0
                    self.strategy.entry_info = None

                    self._save_state()

        except Exception as exc:
            logging.error("❌ Broker verification failed: %s", exc, exc_info=True)

    def _save_state(self):
        """Save current state to file."""
        state = {
            "position": self.position,
            "entry_info": self.entry_info,
            "option_symbol": self.option_symbol,
            "buy_symbol": self.buy_symbol,
            "sell_symbol": self.sell_symbol,
            "buy_entry_price": self.buy_entry_price,
            "sell_entry_price": self.sell_entry_price,
            "entry_option_price": self.entry_option_price,
            "current_option_price": self.current_option_price,
            "buy_entry_oi": getattr(self.executor, "buy_entry_oi", 0),
            "buy_entry_volume": getattr(self.executor, "buy_entry_volume", 0),
            "sell_entry_oi": getattr(self.executor, "sell_entry_oi", 0),
            "sell_entry_volume": getattr(self.executor, "sell_entry_volume", 0),
            # ✅ NEW: Save hedge state
            "hedge_symbol": self.hedge_symbol,
            "hedge_entry_price": self.hedge_entry_price,
            "hedge_bought": self.hedge_bought,
            "hedge_exit_pending": self.hedge_exit_pending,
            # Existing state
            "daily_pnl": self.daily_pnl,
            "consecutive_losses": self.consecutive_losses,
            "trades_today": self.trades_today,
            "trade_number": self.trade_number,
            "current_lots": self.current_lots,
            "stored_expiry": getattr(self, "_stored_expiry", None),
            "peak_price": self.strategy.peak_price,
            "trough_price": self.strategy.trough_price,
            "timestamp": get_current_time(),
        }
        save_state(state)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logging.info("Shutdown signal received")
        self.running = False

    # ========================================================================
    # MAIN LOOP
    # ========================================================================

    def run(self):
        """Main trading loop."""
        print_startup(self.startup_time, TRADING_MODE, STRATEGY)
        send_system_alert("startup", TRADING_MODE)

        while self.running:
            current_time = get_current_time()

            if current_time.time() >= MARKET_CLOSE:
                logging.info("Market closed")
                break

            if not is_market_hours(current_time.time()):
                self._wait_for_market(current_time)
                continue

            self._process_minute(current_time)

            time_module.sleep(MAIN_LOOP_SLEEP)

        self.cleanup()

    def _wait_for_market(self, current_time: datetime):
        """Wait for market to open."""
        if current_time.minute % 5 == 0 and current_time.second < 5:
            print(f"⏳ Waiting for market... ({current_time.strftime('%H:%M:%S')})")
        time_module.sleep(WAIT_SLEEP)

    def _process_minute(self, current_time: datetime):
        """Process each minute of trading with corrected hedge logic."""

        # Position reconciliation (every 5 minutes)
        if current_time.minute % 5 == 0 and current_time.second < 10:
            self._reconcile_positions()

        expiry = self.current_expiry

        # ================================================================
        # STEP 1: Check expiry day exit (MUST exit on expiry day)
        # ================================================================
        should_exit, reason = check_expiry_exit(self.position, expiry)
        if should_exit:
            logging.warning("⚠️ Expiry exit: %s", reason)
            self._force_exit_with_hedge(current_time, reason)
            return

        # ================================================================
        # STEP 2: Check if hedge should be BOUGHT (3:25 PM)
        # Only if position is open and hedge not already bought
        # ================================================================
        if self.position != 0 and not self.hedge_bought:
            should_buy, buy_reason = should_buy_hedge(
                self.position, current_time, self.hedge_bought
            )
            if should_buy:
                logging.info("🛡️ Buying hedge: %s", buy_reason)
                self._buy_hedge(current_time, buy_reason)

        # ================================================================
        # STEP 3: Fetch and process bar
        # ================================================================
        logging.info("📊 Fetching latest bar...")
        bar = self.data_engine.get_latest_bar()

        if not bar:
            logging.warning("⚠️ No bar data received - skipping minute")
            return

        logging.info(
            "✅ Bar: %s | O:%.2f H:%.2f L:%.2f C:%.2f V:%.0f",
            bar["timestamp"].strftime("%H:%M:%S"),
            bar["open"],
            bar["high"],
            bar["low"],
            bar["close"],
            bar["volume"],
        )

        # Store bar for export
        self._all_bars.append(bar)

        # Process strategy to check for signals
        signal_result = self.strategy.on_1min_bar(
            bar["timestamp"],
            bar["open"],
            bar["high"],
            bar["low"],
            bar["close"],
            bar["volume"],
        )

        # Update LTP if position open
        if self.position != 0 and (self.buy_symbol or self.option_symbol):
            self._update_position_ltp()

            # Save state every 2 minutes when position is open
            if current_time.minute % 2 == 0:
                self._save_state()

        # Display status periodically
        self._maybe_display_status(current_time)

        # Handle new bricks
        if self.strategy.has_new_bricks():
            self._handle_new_bricks(current_time)

        # ================================================================
        # STEP 4: Check if hedge should be EXITED (next day 9:16 AM)
        # This is ONLY for the case where NO exit signal was generated
        # and we want to close hedge but KEEP main position
        # ================================================================
        exit_signal_generated = signal_result and signal_result.get("action") == "EXIT"

        if not exit_signal_generated and self.hedge_bought and self.hedge_exit_pending:
            should_exit_hedge, hedge_reason = should_exit_hedge_only(
                self.position, self.hedge_bought, self.hedge_exit_pending, current_time
            )

            if should_exit_hedge:
                logging.info("🛡️ Exiting hedge only (continuation): %s", hedge_reason)
                self._exit_hedge_only(current_time, hedge_reason)
                # Main position remains OPEN - do NOT reset position state

        # ================================================================
        # STEP 5: Handle trading signal (entry or exit)
        # If EXIT signal, close EVERYTHING (main + hedge)
        # ================================================================
        if signal_result:
            self._handle_signal(signal_result, expiry, current_time)

        # Periodic state save
        if (
            current_time - self._last_state_save
        ).total_seconds() >= STATE_SAVE_INTERVAL:
            self._save_state()
            self._last_state_save = current_time

    def _reconcile_positions(self):
        """Reconcile positions with broker."""
        if not self.order_manager or TRADING_MODE != "LIVE":
            return

        try:
            mismatches = self.order_manager.reconcile_with_broker()
            if mismatches:
                logging.error("🚨 POSITION MISMATCH: %s", mismatches)
                send_risk_alert("position_mismatch", str(mismatches))

                # Check if our position was closed at broker
                for symbol, (internal, broker) in mismatches.items():
                    if symbol in (self.buy_symbol, self.sell_symbol):
                        if broker == 0 and self.position != 0:
                            logging.error(
                                "🚨 Position closed at broker but not in system!"
                            )
                            # Don't auto-reset - alert for manual review
                            send_risk_alert(
                                "position_closed_externally",
                                f"{symbol} closed at broker",
                            )
        except Exception as exc:
            logging.error("❌ Position reconciliation error: %s", exc)

    def _update_position_ltp(self):
        """Update LTP for open position legs."""
        symbols_to_fetch = []
        if self.buy_symbol:
            symbols_to_fetch.append(self.buy_symbol)
        if self.sell_symbol:
            symbols_to_fetch.append(self.sell_symbol)
        if not symbols_to_fetch and self.option_symbol:
            symbols_to_fetch.append(self.option_symbol)

        if not symbols_to_fetch:
            return

        try:
            quotes = self.data_engine.get_quotes(symbols_to_fetch)
            if quotes:
                if self.buy_symbol and self.buy_symbol in quotes:
                    ltp = quotes[self.buy_symbol].get("ltp", 0)
                    if ltp > 0:
                        self.current_option_price = ltp
                        logging.debug("💰 Buy LTP: %s @ ₹%.2f", self.buy_symbol, ltp)
                if self.sell_symbol and self.sell_symbol in quotes:
                    sell_ltp = quotes[self.sell_symbol].get("ltp", 0)
                    if sell_ltp > 0:
                        logging.debug(
                            "💰 Sell LTP: %s @ ₹%.2f", self.sell_symbol, sell_ltp
                        )
            else:
                logging.warning("⚠️ No quote data - keeping previous prices")
        except Exception as exc:
            logging.error("❌ LTP fetch failed: %s", exc)

    def _maybe_display_status(self, current_time: datetime):
        """Display detailed status with brick info and position."""
        elapsed = (current_time - self._last_status_display).total_seconds()

        if elapsed >= STATUS_DISPLAY_INTERVAL or self.strategy.has_new_bricks():
            state = self.strategy.get_current_state()

            if state:
                last_10 = state.get("last_10_bricks", [])
                brick_visual = "".join(
                    "🟢" if b == 1 else "🔴" if b == -1 else "⚪" for b in last_10[-10:]
                )

                brick_pct = self.strategy._brick_pct * 100
                # Get pre-computed indicator values from state
                rsi_val = state.get("renko_rsi", float("nan"))
                ma_val = state.get("renko_ma", float("nan"))
                disp_val = state.get("renko_disparity", float("nan"))
                indicators_ready = state.get("indicators_ready", False)

                # Format indicator display
                rsi_str = f"RSI: {rsi_val:.2f}" if indicators_ready else "RSI: N/A"
                ma_str = f"MA: {ma_val:,.2f}" if indicators_ready else "MA: N/A"
                disp_str = f"Disp: {disp_val:.4f}%" if indicators_ready else "Disp: N/A"
                ready_str = "✅" if indicators_ready else "⏳"

                print(f"\n{'=' * 100}")
                print("📊 DETAILED STATUS")
                print(
                    f"   {current_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"Nifty: {state.get('nifty_price', 0):,.2f} | "
                    f"Brick%: {brick_pct:.4f}%"
                )
                print(
                    f"   Renko: {brick_visual} | Total: {state.get('total_bricks', 0)}"
                )

                brick_size = state.get("brick_size", 0)
                if brick_size > 0:
                    brick_high = state.get("brick_high", 0)
                    brick_low = state.get("brick_low", 0)
                    green_thresh = state.get("green_threshold", 0)
                    red_thresh = state.get("red_threshold", 0)
                    direction = state.get("renko_direction", 0)
                    brick_emoji = (
                        "🟢" if direction == 1 else "🔴" if direction == -1 else "⚪"
                    )

                    print(
                        f"   {brick_emoji} High: {brick_high:,.2f} | "
                        f"Low: {brick_low:,.2f} | "
                        f"Size: {brick_size:.2f} | "
                        f"🟢>= {green_thresh:,.2f} | "
                        f"🔴<= {red_thresh:,.2f}"
                    )

                    print(
                        f"  {ready_str} | Indicators: {rsi_str} | {ma_str} | {disp_str}"
                    )

                # ✅ IMPROVED POSITION DISPLAY
                if self.position != 0:
                    direction_str = "LONG" if self.position == 1 else "SHORT"
                    direction_emoji = "🟢" if self.position == 1 else "🔴"

                    pattern_name = state.get("last_pattern_name", "None")
                    pattern_type = state.get("last_pattern_type", "")

                    print(
                        f"   Pattern: {pattern_name} ({pattern_type}) | "
                        f"{direction_emoji} Position: {direction_str} SYNTHETIC | {self.current_lots}L"
                    )

                    if self.buy_symbol and self.sell_symbol:
                        # Get current prices
                        buy_current = self.current_option_price or self.buy_entry_price

                        # Fetch sell leg LTP
                        sell_current = self.sell_entry_price
                        try:
                            quotes = self.data_engine.get_quotes([self.sell_symbol])
                            if quotes and self.sell_symbol in quotes:
                                sell_current = quotes[self.sell_symbol].get(
                                    "ltp", self.sell_entry_price
                                )
                        except Exception:
                            pass

                        # Calculate unrealized PnL for each leg
                        buy_unrealized = (
                            (buy_current - self.buy_entry_price)
                            * LOT_SIZE
                            * self.current_lots
                        )
                        sell_unrealized = (
                            (self.sell_entry_price - sell_current)
                            * LOT_SIZE
                            * self.current_lots
                        )
                        net_unrealized = buy_unrealized + sell_unrealized

                        # Display with color coding
                        buy_sign = "+" if buy_unrealized >= 0 else ""
                        sell_sign = "+" if sell_unrealized >= 0 else ""
                        net_sign = "+" if net_unrealized >= 0 else ""

                        print(
                            f"   📗 BUY:  {self.buy_symbol} | "
                            f"Entry: ₹{self.buy_entry_price:.2f} | "
                            f"Current: ₹{buy_current:.2f} | "
                            f"Unrealized: {buy_sign}₹{buy_unrealized:,.0f}"
                        )
                        print(
                            f"   📕 SELL: {self.sell_symbol} | "
                            f"Entry: ₹{self.sell_entry_price:.2f} | "
                            f"Current: ₹{sell_current:.2f} | "
                            f"Unrealized: {sell_sign}₹{sell_unrealized:,.0f}"
                        )

                        # ✅ NEW: Show hedge if active
                        if self.hedge_bought and self.hedge_symbol:
                            try:
                                hedge_quotes = self.data_engine.get_quotes(
                                    [self.hedge_symbol]
                                )
                                hedge_current = self.hedge_entry_price
                                if hedge_quotes and self.hedge_symbol in hedge_quotes:
                                    hedge_current = hedge_quotes[self.hedge_symbol].get(
                                        "ltp", self.hedge_entry_price
                                    )

                                hedge_unrealized = (
                                    (hedge_current - self.hedge_entry_price)
                                    * LOT_SIZE
                                    * self.current_lots
                                )
                                hedge_sign = "+" if hedge_unrealized >= 0 else ""

                                print(
                                    f"   🛡️ HEDGE: {self.hedge_symbol} | "
                                    f"Entry: ₹{self.hedge_entry_price:.2f} | "
                                    f"Current: ₹{hedge_current:.2f} | "
                                    f"Unrealized: {hedge_sign}₹{hedge_unrealized:,.0f}"
                                )

                                # Adjust net unrealized to include hedge
                                net_unrealized += hedge_unrealized
                                net_sign = "+" if net_unrealized >= 0 else ""
                            except Exception:
                                pass

                        print(f"   💰 NET UNREALIZED: {net_sign}₹{net_unrealized:,.0f}")

                        # Peak/Trough and Stop
                        peak = state.get("peak_price", 0)
                        trough = state.get("trough_price", 0)

                        if self.position == 1 and peak and brick_size > 0:
                            stop = round(peak - (2 * brick_size), 2)
                            print(
                                f"   📈 Peak: {peak:.2f} | "
                                f"Stop: {stop:.2f} (2 bricks down)"
                            )
                        elif self.position == -1 and trough and brick_size > 0:
                            stop = round(trough + (2 * brick_size), 2)
                            print(
                                f"   📉 Trough: {trough:.2f} | "
                                f"Stop: {stop:.2f} (2 bricks up)"
                            )

                        # Holding time
                        if self.entry_info and self.entry_info.get("time"):
                            entry_time = self.entry_info["time"]
                            if hasattr(entry_time, "strftime"):
                                holding = (
                                    current_time - entry_time
                                ).total_seconds() / 3600
                                print(
                                    f"   ⏱️ Entry: "
                                    f"{entry_time.strftime('%d-%b %H:%M')} | "
                                    f"Holding: {holding:.1f}h"
                                )
                    elif self.option_symbol:
                        current_opt = self.current_option_price or 0
                        entry_opt = self.entry_option_price or 0
                        unrealized = (
                            (current_opt - entry_opt) * LOT_SIZE * self.current_lots
                            if entry_opt > 0
                            else 0
                        )
                        print(
                            f"   💼 {self.option_symbol} | "
                            f"LTP: ₹{current_opt:.2f} | "
                            f"Entry: ₹{entry_opt:.2f} | "
                            f"Unrealized: ₹{unrealized:,.0f}"
                        )
                    else:
                        print(f"   ⚠️ Position: {direction_str} but NO SYMBOLS!")
                else:
                    print("   💼 Position: FLAT | Waiting for pattern signal...")

                print(f"{'=' * 100}\n")

            self._last_status_display = current_time

    def _handle_new_bricks(self, current_time: datetime):
        """Handle new brick formation."""
        count = self.strategy.get_new_brick_count()
        state = self.strategy.get_current_state()

        if state:
            print_brick_formation(
                count,
                state.get("renko_direction", 0),
                state.get("nifty_price", 0),
                current_time,
            )

        if count > 0:
            self.strategy.debug_renko_thresholds()

        self.strategy.acknowledge_new_bricks()

    def _handle_signal(
        self, signal_data: Dict[str, Any], expiry: str, current_time: datetime
    ):
        """Handle trading signal with broker capital integration."""
        action = signal_data.get("action")

        if action in ("LONG", "SHORT"):
            self.trade_number += 1
            signal_data["trade_number"] = self.trade_number

            # ✅ FETCH BROKER CAPITAL FOR POSITION SIZING
            broker_capital = self._fetch_broker_capital()

            # Calculate lots using broker capital (with fallback to system calculation)
            signal_data["lots"] = calculate_lots(
                broker_capital=broker_capital,
                current_pnl=self.daily_pnl,
                initial_lots=STRATEGY["initial_lots"],
                max_lots=STRATEGY["max_lots"],
                capital_per_lot=CAPITAL_PER_LOT,
            )

            signal_data["expiry"] = expiry

        elif action == "EXIT":
            signal_data["trade_number"] = self.trade_number
            signal_data["lots"] = self.current_lots
            signal_data["expiry"] = expiry

        print_signal(signal_data, signal_data.get("trade_number", 0))

        # Check risk limits
        breached, reason = check_risk_limits(self.consecutive_losses, self.daily_pnl)
        if breached:
            logging.error("🚨 RISK LIMIT BREACHED: %s", reason)
            return

        # Execute trade
        trade_result = self._execute_trade(signal_data, expiry)

        if trade_result:
            self._process_trade_result(trade_result, action)

    def _execute_trade(
        self, signal_data: Dict[str, Any], expiry: str
    ) -> Optional[Dict[str, Any]]:
        """Execute trade based on signal."""
        action = signal_data.get("action")
        lots = signal_data.get("lots", self.current_lots)

        if action == "EXIT":
            return self.executor.execute_exit(signal_data, expiry, lots)
        elif action in ("LONG", "SHORT"):
            return self.executor.execute_entry(signal_data, expiry, lots)

        return None

    def _process_trade_result(self, result: Dict[str, Any], action: str):
        """
        Process trade result and update state.

        HEDGE LOGIC (SIMPLIFIED):
        - When main position EXITS, ALWAYS close hedge too
        - No complex "pending" logic during exit
        """
        self.trades_today.append(result)
        save_trades(self.trades_today)

        send_trade_alert(result)
        print_trade_execution(result, result.get("trade_number", 0))

        if action == "EXIT":
            pnl = result.get("pnl", 0)
            self.daily_pnl += pnl

            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

            # ============================================
            # HEDGE: Always close with main position exit
            # ============================================
            if self.hedge_bought and self.hedge_symbol:
                logging.info("🛡️ Closing hedge with main position exit")
                self._exit_hedge_with_main(
                    get_current_time(),
                    f"Main position exited: {result.get('exit_reason', 'unknown')}",
                )

            # Reset ALL position state
            self.position = 0
            self.entry_info = None
            self.option_symbol = None
            self.entry_option_price = None
            self.current_option_price = None
            self.buy_symbol = None
            self.sell_symbol = None
            self.buy_entry_price = None
            self.sell_entry_price = None

            # Reset hedge state
            self.hedge_symbol = None
            self.hedge_entry_price = None
            self.hedge_bought = False
            self.hedge_exit_pending = False

            self.current_lots = STRATEGY["initial_lots"]

            # Sync with strategy
            self.strategy.position = 0
            self.strategy.entry_info = None
            self.strategy.peak_price = None
            self.strategy.trough_price = None

            # Clear executor state
            if hasattr(self, "executor"):
                self.executor.buy_symbol = None
                self.executor.sell_symbol = None
                self.executor.buy_entry_price = None
                self.executor.sell_entry_price = None
                logging.info("✅ Cleared executor state after exit")

            self._recalculate_expiry()

        elif action in ("LONG", "SHORT"):
            self.position = 1 if action == "LONG" else -1
            self.entry_info = {
                "time": result["timestamp"],
                "price": result["price"],
                "symbol": result.get("buy_symbol", ""),
                "expiry": result["expiry"],
                "pattern_name": result.get("pattern_name", ""),
                "pattern_type": result.get("pattern_type", ""),
            }

            self.buy_symbol = result["buy_symbol"]
            self.sell_symbol = result["sell_symbol"]
            self.buy_entry_price = result["buy_entry_price"]
            self.sell_entry_price = result["sell_entry_price"]
            self.current_lots = result["lots"]

            # Set option_symbol for tracking
            self.option_symbol = result["buy_symbol"]
            self.entry_option_price = result["buy_entry_price"]
            self.current_option_price = result["buy_entry_price"]

            # Reset hedge state on new entry
            self.hedge_symbol = None
            self.hedge_entry_price = None
            self.hedge_bought = False
            self.hedge_exit_pending = False

            # Sync with strategy
            self.strategy.position = self.position
            self.strategy.entry_info = self.entry_info
            self.strategy.peak_price = result["price"]
            self.strategy.trough_price = result["price"]

            logging.info(
                "✅ Position: %s | Buy: %s @ ₹%.2f | Sell: %s @ ₹%.2f",
                "LONG" if self.position == 1 else "SHORT",
                self.buy_symbol,
                self.buy_entry_price,
                self.sell_symbol,
                self.sell_entry_price,
            )

        self._save_state()

    # ========================================================================
    # HEDGE MANAGEMENT
    # ========================================================================

    def _buy_hedge(self, current_time: datetime, reason: str) -> None:
        """
        Buy hedge for SHORT leg protection.

        Args:
            current_time: Current datetime
            reason: Reason for buying hedge
        """
        if self.position == 0 or not self.sell_symbol:
            logging.warning("Cannot buy hedge - no position or sell symbol")
            return

        if self.hedge_bought:
            logging.warning("Hedge already bought")
            return

        # Calculate hedge symbol
        hedge_symbol = calculate_hedge_symbol(self.sell_symbol, self.position)

        if not hedge_symbol:
            logging.error("Failed to calculate hedge symbol")
            return

        # Get hedge price
        try:
            quotes = self.data_engine.get_quotes([hedge_symbol])
            if not quotes or hedge_symbol not in quotes:
                logging.error("Failed to get hedge price for %s", hedge_symbol)
                return

            hedge_price = quotes[hedge_symbol].get("ltp", 0)

            if hedge_price <= 0:
                logging.error("Invalid hedge price: %.2f", hedge_price)
                return

            logging.info(
                "🛡️ Hedge: %s @ ₹%.2f (protects %s)",
                hedge_symbol,
                hedge_price,
                self.sell_symbol,
            )

            # Place hedge order (BUY)
            if TRADING_MODE == "LIVE" and self.order_manager:
                quantity = self.current_lots * LOT_SIZE

                hedge_order = self.order_manager.place_market_order(
                    hedge_symbol, OrderSide.BUY, quantity
                )

                if hedge_order.order_id:
                    hedge_order = self.order_manager.wait_for_fill(
                        hedge_order, timeout_seconds=30
                    )

                if hedge_order.is_complete or hedge_order.is_partial:
                    filled_price = (
                        hedge_order.filled_price
                        if hedge_order.filled_price > 0
                        else hedge_price
                    )

                    self.hedge_symbol = hedge_symbol
                    self.hedge_entry_price = filled_price
                    self.hedge_bought = True
                    self.hedge_exit_pending = True  # Flag for next-day exit

                    logging.info(
                        "✅ Hedge bought: %s @ ₹%.2f (qty=%d)",
                        hedge_symbol,
                        filled_price,
                        quantity,
                    )

                    print(f"\n{'=' * 80}")
                    print(f"🛡️ HEDGE PROTECTION ACTIVATED")
                    print(f"   Symbol: {hedge_symbol}")
                    print(f"   Price: ₹{filled_price:.2f}")
                    print(f"   Quantity: {quantity}")
                    print(f"   Protects: {self.sell_symbol}")
                    print(f"   Next Action: Exit hedge at 9:16 AM (if no exit signal)")
                    print(f"{'=' * 80}\n")

                    self._save_state()
                else:
                    logging.error("❌ Hedge order failed: %s", hedge_order.status)
            else:
                # Paper trading
                self.hedge_symbol = hedge_symbol
                self.hedge_entry_price = hedge_price
                self.hedge_bought = True
                self.hedge_exit_pending = True

                logging.info(
                    "✅ Hedge bought (PAPER): %s @ ₹%.2f", hedge_symbol, hedge_price
                )

                self._save_state()

        except Exception as exc:
            logging.error("Failed to buy hedge: %s", exc, exc_info=True)

    def _exit_hedge_only(self, current_time: datetime, reason: str) -> bool:
        """
        Exit hedge ONLY (keep main position open).

        Called on next day at 9:16 AM when NO exit signal was generated.
        Main position continues running.

        Args:
            current_time: Current datetime
            reason: Reason for exiting hedge

        Returns:
            True if hedge exited successfully
        """
        if not self.hedge_bought or not self.hedge_symbol:
            logging.warning("No hedge to exit")
            return False

        logging.info("🛡️ Exiting hedge only: %s", reason)

        hedge_exited = False
        hedge_pnl = 0.0
        hedge_exit_price = self.hedge_entry_price or 0

        try:
            # Get current hedge price
            quotes = self.data_engine.get_quotes([self.hedge_symbol])

            if quotes and self.hedge_symbol in quotes:
                hedge_exit_price = quotes[self.hedge_symbol].get(
                    "ltp", self.hedge_entry_price or 0
                )

            # Place exit order (SELL the hedge we bought)
            if TRADING_MODE == "LIVE" and self.order_manager:
                quantity = self.current_lots * LOT_SIZE

                exit_order = self.order_manager.place_market_order(
                    self.hedge_symbol, OrderSide.SELL, quantity
                )

                if exit_order.order_id:
                    exit_order = self.order_manager.wait_for_fill(
                        exit_order, timeout_seconds=30
                    )

                if exit_order.is_complete or exit_order.is_partial:
                    filled_price = (
                        exit_order.filled_price
                        if exit_order.filled_price > 0
                        else hedge_exit_price
                    )

                    hedge_pnl = (
                        (filled_price - (self.hedge_entry_price or 0))
                        * LOT_SIZE
                        * self.current_lots
                    )

                    hedge_exited = True
                    hedge_exit_price = filled_price

                    logging.info(
                        "✅ Hedge exited: %s @ ₹%.2f | PnL: ₹%.0f",
                        self.hedge_symbol,
                        filled_price,
                        hedge_pnl,
                    )
                else:
                    logging.error("❌ Hedge exit order failed: %s", exit_order.status)
            else:
                # Paper trading
                hedge_pnl = (
                    (hedge_exit_price - (self.hedge_entry_price or 0))
                    * LOT_SIZE
                    * self.current_lots
                )
                hedge_exited = True

                logging.info(
                    "✅ Hedge exited (PAPER): %s @ ₹%.2f | PnL: ₹%.0f",
                    self.hedge_symbol,
                    hedge_exit_price,
                    hedge_pnl,
                )

        except Exception as exc:
            logging.error("Failed to exit hedge: %s", exc, exc_info=True)

        # Only clear hedge state if successfully exited
        if hedge_exited:
            print(f"\n{'=' * 80}")
            print(f"🛡️ HEDGE EXITED (Main Position Continues)")
            print(f"   Symbol: {self.hedge_symbol}")
            print(f"   Entry: ₹{self.hedge_entry_price:.2f}")
            print(f"   Exit: ₹{hedge_exit_price:.2f}")
            print(f"   PnL: ₹{hedge_pnl:,.0f}")
            print(f"   Main Position: STILL OPEN")
            print(f"{'=' * 80}\n")

            # Clear hedge state
            self.hedge_symbol = None
            self.hedge_entry_price = None
            self.hedge_bought = False
            self.hedge_exit_pending = False

            self._save_state()

            return True
        else:
            logging.error("⚠️ Hedge exit failed - will retry next minute")
            return False

    def _exit_hedge_with_main(self, current_time: datetime, reason: str) -> bool:
        """
        Exit hedge when main position is exiting.

        Called from _process_trade_result() when main position exits.

        Args:
            current_time: Current datetime
            reason: Reason for exit

        Returns:
            True if hedge exited successfully
        """
        if not self.hedge_bought or not self.hedge_symbol:
            return True  # No hedge to exit

        logging.info("🛡️ Closing hedge with main: %s", reason)

        try:
            # Get current hedge price
            quotes = self.data_engine.get_quotes([self.hedge_symbol])
            hedge_exit_price = self.hedge_entry_price or 0

            if quotes and self.hedge_symbol in quotes:
                hedge_exit_price = quotes[self.hedge_symbol].get(
                    "ltp", self.hedge_entry_price or 0
                )

            # Place exit order
            if TRADING_MODE == "LIVE" and self.order_manager:
                quantity = self.current_lots * LOT_SIZE

                exit_order = self.order_manager.place_market_order(
                    self.hedge_symbol, OrderSide.SELL, quantity
                )

                if exit_order.order_id:
                    exit_order = self.order_manager.wait_for_fill(
                        exit_order, timeout_seconds=30
                    )

                if exit_order.is_complete or exit_order.is_partial:
                    filled_price = (
                        exit_order.filled_price
                        if exit_order.filled_price > 0
                        else hedge_exit_price
                    )

                    hedge_pnl = (
                        (filled_price - (self.hedge_entry_price or 0))
                        * LOT_SIZE
                        * self.current_lots
                    )

                    logging.info(
                        "✅ Hedge closed with main: %s @ ₹%.2f | PnL: ₹%.0f",
                        self.hedge_symbol,
                        filled_price,
                        hedge_pnl,
                    )
                    return True
                else:
                    logging.error("❌ Hedge exit failed: %s", exit_order.status)
                    # Continue anyway - main position exit is more important
                    return False
            else:
                # Paper trading
                hedge_pnl = (
                    (hedge_exit_price - (self.hedge_entry_price or 0))
                    * LOT_SIZE
                    * self.current_lots
                )
                logging.info(
                    "✅ Hedge closed (PAPER): %s | PnL: ₹%.0f",
                    self.hedge_symbol,
                    hedge_pnl,
                )
                return True

        except Exception as exc:
            logging.error("Failed to close hedge: %s", exc)
            return False

    def _force_exit_with_hedge(self, current_time: datetime, reason: str):
        """
        Force exit position AND hedge (for expiry day or emergency).

        This is different from normal exit - it's a forced close of everything.

        Args:
            current_time: Current datetime
            reason: Reason for force exit
        """
        if self.position == 0:
            return

        from src.config import make_aware

        current_time = make_aware(current_time)

        logging.warning("🚨 FORCE EXIT: %s", reason)

        # Get last price
        last_price = 0
        if self.strategy.renko_bricks:
            last_price = self.strategy.renko_bricks[-1]["close"]

        signal_data = {
            "action": "EXIT",
            "timestamp": current_time,
            "price": last_price,
            "exit_reason": reason,
            "trade_number": self.trade_number,
            "lots": self.current_lots,
        }

        expiry = self.current_expiry

        try:
            # Step 1: Exit hedge first (if any)
            if self.hedge_bought and self.hedge_symbol:
                logging.info("🛡️ Force closing hedge")
                self._exit_hedge_with_main(current_time, reason)

            # Step 2: Exit main position
            trade_result = self.executor.execute_exit(
                signal_data, expiry, self.current_lots
            )

            if trade_result:
                trade_result["exit_reason"] = reason
                self._process_trade_result(trade_result, "EXIT")
                logging.info("✅ Force exit completed: %s", reason)
            else:
                logging.error("❌ Force exit failed!")
                send_risk_alert("exit_failed", reason)

                # Still reset state - position may be partially closed
                self._emergency_state_reset()

        except Exception as exc:
            logging.error("Force exit exception: %s", exc, exc_info=True)
            send_risk_alert("exit_exception", f"{reason}: {exc}")

            # Emergency reset
            self._emergency_state_reset()

    def _emergency_state_reset(self):
        """Emergency reset of all state (last resort)."""
        logging.error("🚨 EMERGENCY STATE RESET")

        self.position = 0
        self.entry_info = None
        self.option_symbol = None
        self.entry_option_price = None
        self.current_option_price = None
        self.buy_symbol = None
        self.sell_symbol = None
        self.buy_entry_price = None
        self.sell_entry_price = None
        self.hedge_symbol = None
        self.hedge_entry_price = None
        self.hedge_bought = False
        self.hedge_exit_pending = False

        self.strategy.position = 0
        self.strategy.entry_info = None

        self._save_state()

        send_risk_alert(
            "emergency_reset",
            "State reset due to error - verify broker positions manually",
        )

    def cleanup(self):
        """
        Cleanup on shutdown - preserve position for next day recovery.

        IMPORTANT: Does NOT force exit positions. Positions persist at broker
        and will be recovered on next startup.
        """
        logging.info("=" * 60)
        logging.info("STARTING CLEANUP")
        logging.info("=" * 60)

        # Step 1: Save current state FIRST (before anything else)
        self._save_state()
        logging.info("✅ State saved")

        # Step 2: Log open position warning (but don't close it!)
        if self.position != 0:
            direction = "LONG" if self.position == 1 else "SHORT"

            logging.warning("=" * 60)
            logging.warning("⚠️ OPEN POSITION AT SHUTDOWN - WILL RECOVER NEXT DAY")
            logging.warning("=" * 60)
            logging.warning("   Direction: %s", direction)
            logging.warning("   Lots: %d", self.current_lots)
            logging.warning(
                "   Buy Symbol: %s @ ₹%.2f", self.buy_symbol, self.buy_entry_price or 0
            )
            logging.warning(
                "   Sell Symbol: %s @ ₹%.2f",
                self.sell_symbol,
                self.sell_entry_price or 0,
            )

            if self.hedge_bought and self.hedge_symbol:
                logging.warning(
                    "   Hedge: %s @ ₹%.2f (exit pending: %s)",
                    self.hedge_symbol,
                    self.hedge_entry_price or 0,
                    self.hedge_exit_pending,
                )

            if self.entry_info:
                entry_time = self.entry_info.get("time")
                if hasattr(entry_time, "strftime"):
                    logging.warning(
                        "   Entry Time: %s", entry_time.strftime("%Y-%m-%d %H:%M:%S")
                    )

            logging.warning("=" * 60)

            # Send Telegram alert about open position
            send_system_alert(
                "warning",
                f"⚠️ OPEN {direction} POSITION at shutdown\n"
                f"Buy: {self.buy_symbol}\n"
                f"Sell: {self.sell_symbol}\n"
                f"Lots: {self.current_lots}\n"
                f"Will recover on next startup",
            )

        # Step 3: Fetch broker capital at end of day
        self.broker_capital_end = self._fetch_broker_capital()
        self.broker_brokerage_today = self._fetch_broker_brokerage()

        if self.broker_capital_end is not None:
            logging.info("📊 Day End Capital: ₹%.2f", self.broker_capital_end)

            print(f"\n{'=' * 80}")
            print(f"📊 BROKER CAPITAL (Day End)")
            print(f"   Available: ₹{self.broker_capital_end:,.2f}")

            if self.broker_capital_start is not None:
                diff = self.broker_capital_end - self.broker_capital_start
                print(f"   Day Change: ₹{diff:+,.2f}")

            if self.broker_brokerage_today > 0:
                print(f"   Brokerage: ₹{self.broker_brokerage_today:,.2f}")

            print(f"{'=' * 80}\n")

        # Step 4: Log filter statistics
        try:
            if hasattr(self, "strategy"):
                self.strategy.log_filter_stats()
        except Exception as exc:
            logging.warning("Failed to log filter stats: %s", exc)

        # Step 5: Generate and send daily report
        trades = self.trades_today or load_daily_trades()

        # Calculate unrealized PnL for open position
        unrealized_pnl = 0.0
        open_position_info = None

        if self.position != 0:
            unrealized_pnl = self._calculate_unrealized_pnl()
            open_position_info = {
                "direction": "LONG" if self.position == 1 else "SHORT",
                "entry_time": self.entry_info.get("time") if self.entry_info else None,
                "entry_price": self.entry_info.get("price") if self.entry_info else 0,
                "current_price": self.strategy.renko_bricks[-1]["close"]
                if self.strategy.renko_bricks
                else 0,
                "unrealized_pnl": unrealized_pnl,
                "lots": self.current_lots,
                "buy_symbol": self.buy_symbol,
                "sell_symbol": self.sell_symbol,
                "hedge_active": self.hedge_bought,
                "hedge_symbol": self.hedge_symbol,
            }

        # Generate report with open position info
        report = generate_daily_report(
            trades,
            broker_capital_start=self.broker_capital_start,
            broker_capital_end=self.broker_capital_end,
            broker_brokerage=self.broker_brokerage_today,
            open_position=open_position_info,
            unrealized_pnl=unrealized_pnl,
        )

        save_daily_report(report)
        send_daily_summary(report)

        # Step 6: Export daily data
        try:
            from src.export import export_daily_data

            option_symbols = set()
            for trade in self.trades_today:
                for key in ["buy_symbol", "sell_symbol", "symbol"]:
                    sym = trade.get(key)
                    if sym:
                        option_symbols.add(sym)

            # Add current position symbols
            if self.buy_symbol:
                option_symbols.add(self.buy_symbol)
            if self.sell_symbol:
                option_symbols.add(self.sell_symbol)
            if self.hedge_symbol:
                option_symbols.add(self.hedge_symbol)

            export_daily_data(
                renko_bricks=self.strategy.renko_bricks,
                option_symbols=option_symbols,
                data_engine=self.data_engine,
                oneminute_bars=self._all_bars,
                trading_date=get_current_date(),
            )
        except Exception as exc:
            logging.error("Failed to export daily data: %s", exc)

        # Step 7: Cleanup order manager (cancel pending orders only)
        if self.order_manager:
            try:
                # Only cancel PENDING orders, not positions
                cancelled = self.order_manager.cancel_all_pending()
                if cancelled > 0:
                    logging.info("Cancelled %d pending orders", cancelled)
            except Exception as exc:
                logging.error("Order manager cleanup error: %s", exc)

        # Step 8: Cleanup data engine
        if self.data_engine:
            self.data_engine.cleanup()

        # Step 9: Delete old state files (keep last 7 days)
        try:
            cutoff_date = get_current_date() - timedelta(days=7)
            deleted = 0

            for state_file in STATE_DIR.glob("state_*.json"):
                try:
                    date_str = state_file.stem.replace("state_", "")
                    file_date = datetime.strptime(date_str, "%Y%m%d").date()

                    if file_date < cutoff_date:
                        state_file.unlink()
                        deleted += 1

                except (ValueError, OSError):
                    pass

            if deleted > 0:
                logging.info("🧹 Cleaned up %d old state files", deleted)

        except Exception as exc:
            logging.warning("Failed to cleanup old state files: %s", exc)

        # Step 10: Send shutdown alert
        if self.position != 0:
            send_system_alert(
                "info", f"💤 Shutdown complete\n⚠️ Open position preserved for next day"
            )
        else:
            send_system_alert("shutdown")

        print(
            f"\n💤 Shutdown complete at {get_current_time().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        if self.position != 0:
            print(f"⚠️ OPEN POSITION will be recovered on next startup")

    def _calculate_unrealized_pnl(self) -> float:
        """Calculate unrealized PnL for open position."""
        if self.position == 0:
            return 0.0

        if not self.buy_symbol or not self.sell_symbol:
            return 0.0

        try:
            # Get current prices
            quotes = self.data_engine.get_quotes([self.buy_symbol, self.sell_symbol])

            if not quotes:
                return 0.0

            buy_current = quotes.get(self.buy_symbol, {}).get(
                "ltp", self.buy_entry_price or 0
            )
            sell_current = quotes.get(self.sell_symbol, {}).get(
                "ltp", self.sell_entry_price or 0
            )

            # Calculate PnL for each leg
            buy_pnl = (
                (buy_current - (self.buy_entry_price or 0))
                * LOT_SIZE
                * self.current_lots
            )
            sell_pnl = (
                ((self.sell_entry_price or 0) - sell_current)
                * LOT_SIZE
                * self.current_lots
            )

            # Add hedge PnL if active
            hedge_pnl = 0.0
            if self.hedge_bought and self.hedge_symbol:
                hedge_quotes = self.data_engine.get_quotes([self.hedge_symbol])
                if hedge_quotes and self.hedge_symbol in hedge_quotes:
                    hedge_current = hedge_quotes[self.hedge_symbol].get(
                        "ltp", self.hedge_entry_price or 0
                    )
                    hedge_pnl = (
                        (hedge_current - (self.hedge_entry_price or 0))
                        * LOT_SIZE
                        * self.current_lots
                    )

            return buy_pnl + sell_pnl + hedge_pnl

        except Exception as exc:
            logging.error("Failed to calculate unrealized PnL: %s", exc)
            return 0.0


# ============================================================================
# ENTRY POINT
# ============================================================================


def main():
    """Main entry point."""
    setup_logging()

    try:
        trader = Trader()
        trader.run()
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted by user")
    except Exception as exc:
        logging.error("Fatal error: %s", exc, exc_info=True)
        print(f"\n❌ Fatal error: {exc}")
        raise


if __name__ == "__main__":
    main()
