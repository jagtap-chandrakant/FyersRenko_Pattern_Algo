# src/config.py
"""
Configuration Module

Central configuration for trading system including constants,
strategy parameters, and system settings.

Author: Chandrakant Jagtap
Version: 6.0.0 (Pattern-Based)
"""

import json
import logging
import os
import sys
from datetime import time as dtime, date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Set, Optional

import pytz
from dotenv import load_dotenv

# =============================================================================
# TIMEZONE
# =============================================================================

IST = pytz.timezone("Asia/Kolkata")


def get_current_time() -> datetime:
    """Get current time in IST."""
    return datetime.now(IST)


def get_current_date() -> date:
    """Get current date in IST."""
    return get_current_time().date()


def make_aware(dt: datetime) -> datetime:
    """
    Make naive datetime timezone-aware (IST).

    Args:
        dt: Datetime object (naive or aware)

    Returns:
        Timezone-aware datetime in IST
    """
    if dt.tzinfo is None:
        return IST.localize(dt)
    return dt.astimezone(IST)


# =============================================================================
# MARKET TIMING
# =============================================================================

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
MARKET_CLOSE_BUFFER = dtime(15, 15)

# Black Swan Protection Times
HEDGE_BUY_TIME = dtime(15, 28)  # Buy hedge at 3:25 PM if position open
NEXT_DAY_HEDGE_EXIT_TIME = dtime(9, 16)  # Exit hedge next day at 9:16 AM
HEDGE_STRIKE_OFFSET = 100  # OTM offset for hedge (2 strikes away)
# Expiry day exit times
EXPIRY_EXIT_PRIMARY = dtime(15, 0)
EXPIRY_EXIT_SECONDARY = dtime(14, 30)
EXPIRY_EXIT_FINAL = dtime(15, 15)

# Loop timing (seconds)
MAIN_LOOP_SLEEP = 45
WAIT_FOR_TRADING_SLEEP = 30
STATE_SAVE_INTERVAL = 300
STATUS_DISPLAY_INTERVAL = 300
CACHE_CLEANUP_INTERVAL = 1800

# Time constants
SECONDS_IN_HOUR = 3600
SECONDS_IN_DAY = 86400


# =============================================================================
# DATA LIMITS
# =============================================================================

MAX_HISTORICAL_BARS = 800
MIN_BARS_FOR_INDICATORS = 15
MIN_BARS_FOR_RENKO = 3
MIN_BARS_FOR_ENTRY = 2
DEFAULT_WARMUP_DAYS = 3
MAX_WARMUP_DAYS = 5


# =============================================================================
# API SETTINGS
# =============================================================================

# Fyers API
FYERS_SUCCESS_CODE = 200
FYERS_RATE_LIMIT_CODE = 429
FYERS_BAD_REQUEST_CODE = 400

# Symbols
NIFTY_INDEX_SYMBOL = "NSE:NIFTY50-INDEX"
NIFTY_OPTION_BASE = "NSE:NIFTY"

# Retry settings
API_MAX_RETRIES = 3
API_BASE_DELAY = 2
API_TIMEOUT = 10
RATE_LIMIT_SLEEP = 2

# Auth retry
TOTP_MAX_RETRIES = 3
TOTP_RETRY_DELAY = 5
AUTH_MAX_RETRIES = 5

# Token settings
TOKEN_DIR = "data/accessTokens"
MIN_TOKEN_LENGTH = 20
TOKEN_VALID_DURATION = 86400  # 24 hours


# =============================================================================
# OPTION CONSTANTS
# =============================================================================

STRIKE_INTERVAL = 50
MIN_STRIKE = 50
MIN_OPTION_PRICE = 0.5
OPTION_TYPE_LENGTH = 2
STRIKE_DIGITS = 5


# =============================================================================
# FILE SYSTEM
# =============================================================================

DATA_DIR = "data"
LOG_DIR = "data/logs"
TRADES_DIR = "data/trades"
REPORTS_DIR = "data/reports"
STATE_DIR = "data/state"

MASTER_TRADES_FILE = "trades_master.csv"
MASTER_SUMMARY_FILE = "summary_master.csv"

MAX_LOG_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_LOG_BACKUP_COUNT = 5


# =============================================================================
# DISPLAY SETTINGS
# =============================================================================

SEPARATOR_LENGTH = 60
TARGET_BRICK_COUNT = 10
LOG_PREVIEW_LENGTH = 200

# Telegram
TELEGRAM_TIMEOUT = 30
TELEGRAM_MAX_RETRIES = 3


# =============================================================================
# RISK LIMITS
# =============================================================================

MAX_LOSS_PER_TRADE_PCT = 10.0
MAX_LOSS_PER_LOT = 10000
MAX_LOTS_PER_TRADE = 20


# =============================================================================
# CALENDAR - HOLIDAYS
# =============================================================================

HOLIDAY_FILE = "config/nse_holidays.json"
WEEKEND_DAYS = {5, 6}  # Saturday, Sunday
TUESDAY = 1  # weekday() value for expiry day


# Month codes for option symbols
MONTH_CODES = {
    1: "1",
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "O",
    11: "N",
    12: "D",
}

# Fallback holidays (used if JSON file missing)
_FALLBACK_HOLIDAYS_2025 = {
    date(2025, 1, 26),
    date(2025, 3, 14),
    date(2025, 3, 31),
    date(2025, 4, 10),
    date(2025, 4, 14),
    date(2025, 4, 18),
    date(2025, 5, 1),
    date(2025, 8, 15),
    date(2025, 8, 27),
    date(2025, 10, 2),
    date(2025, 10, 21),
    date(2025, 11, 1),
    date(2025, 11, 5),
    date(2025, 12, 25),
}

_FALLBACK_HOLIDAYS_2026 = {
    date(2026, 1, 26),
    date(2026, 3, 3),
    date(2026, 3, 25),
    date(2026, 4, 2),
    date(2026, 4, 6),
    date(2026, 4, 10),
    date(2026, 4, 14),
    date(2026, 5, 1),
    date(2026, 8, 15),
    date(2026, 9, 16),
    date(2026, 10, 2),
    date(2026, 10, 19),
    date(2026, 11, 8),
    date(2026, 11, 16),
    date(2026, 12, 25),
}

# Holiday cache
_holidays_cache: Optional[Dict[int, Set[date]]] = None


def _load_holidays() -> Dict[int, Set[date]]:
    """Load holidays from JSON file with fallback."""
    global _holidays_cache

    if _holidays_cache is not None:
        return _holidays_cache

    file_path = Path(HOLIDAY_FILE)

    if not file_path.exists():
        logging.warning("Holiday file not found: %s. Using fallback.", file_path)
        _holidays_cache = {2025: _FALLBACK_HOLIDAYS_2025, 2026: _FALLBACK_HOLIDAYS_2026}
        return _holidays_cache

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        holidays_by_year = {}
        for year_str, holiday_list in data.items():
            if not year_str.isdigit():
                continue
            year = int(year_str)
            year_holidays = set()
            for entry in holiday_list:
                if isinstance(entry, dict) and "date" in entry:
                    try:
                        hol_date = datetime.strptime(entry["date"], "%Y-%m-%d").date()
                        year_holidays.add(hol_date)
                    except ValueError:
                        continue
            if year_holidays:
                holidays_by_year[year] = year_holidays

        if holidays_by_year:
            _holidays_cache = holidays_by_year
            logging.info(
                "Loaded holidays for years: %s", sorted(holidays_by_year.keys())
            )
            return _holidays_cache

    except Exception as exc:
        logging.error("Failed to load holidays: %s", exc)

    _holidays_cache = {2025: _FALLBACK_HOLIDAYS_2025, 2026: _FALLBACK_HOLIDAYS_2026}
    return _holidays_cache


def is_holiday(check_date: date) -> bool:
    """Check if date is a trading holiday."""
    holidays = _load_holidays()
    year_holidays = holidays.get(check_date.year, set())
    return check_date in year_holidays


def is_weekend(check_date: date) -> bool:
    """Check if date is a weekend."""
    return check_date.weekday() in WEEKEND_DAYS


def is_trading_day(check_date: date) -> bool:
    """Check if date is a trading day."""
    return not (is_weekend(check_date) or is_holiday(check_date))


# =============================================================================
# TRADING MODE
# =============================================================================

load_dotenv("config/.env")
TRADING_MODE = os.getenv("TRADING_MODE", "LIVE")


# =============================================================================
# PATTERN CONFIGURATION
# =============================================================================

PATTERNS = {
    "enabled": True,
    # Bullish patterns (Buy CE)
    "bullish": {
        "one_back": {
            "enabled": True,
            "sequence": [1, 1, -1, 1],
            "name": "One-Back",
            "description": "Two up, one down, one up",
        },
        "two_back": {
            "enabled": True,
            "sequence": [1, 1, -1, -1, 1],
            "name": "Two-Back",
            "description": "Two up, two down, one up",
        },
        "three_back": {
            "enabled": True,
            "sequence": [1, 1, -1, -1, -1, 1],
            "name": "Three-Back",
            "description": "Two up, three down, one up",
        },
        "zigzag": {
            "enabled": True,
            "sequence": [1, -1, 1, 1],
            "name": "Zigzag",
            "description": "Up, down, two up",
        },
    },
    # Bearish patterns (Buy PE)
    "bearish": {
        "one_back": {
            "enabled": True,
            "sequence": [-1, -1, 1, -1],
            "name": "One-Back",
            "description": "Two down, one up, one down",
        },
        "two_back": {
            "enabled": True,
            "sequence": [-1, -1, 1, 1, -1],
            "name": "Two-Back",
            "description": "Two down, two up, one down",
        },
        "three_back": {
            "enabled": True,
            "sequence": [-1, -1, 1, 1, 1, -1],
            "name": "Three-Back",
            "description": "Two down, three up, one down",
        },
        "zigzag": {
            "enabled": True,
            "sequence": [-1, 1, -1, -1],
            "name": "Zigzag",
            "description": "Down, up, two down",
        },
    },
}


# =============================================================================
# STRATEGY CONFIGURATION
# =============================================================================

STRATEGY: Dict[str, Any] = {
    # Renko parameters
    "renko_brick_pct": 0.0003,
    "renko_reversal": 2,
    # Exit parameters
    "exit_trailing_bricks": 2,
    # Position sizing
    "lot_size": 65,
    "initial_lots": 1,
    "max_lots": 20,
    "capital_per_lot": 220000,
    # Trading hours
    "trading_start": "09:15",
    "trading_end": "15:30",
    "entry_start_time": "09:16",
    # Risk management
    "max_consecutive_losses": 10,
    "max_daily_loss": 15000,
    "max_hold_hours": 168,
    # Entry rules
    "enable_pullback_entries": True,
    "enable_breakout_entries": True,
    # Costs
    "cost_per_lot": 60,
    # Option selection
    "option_itm_pct": 0.2,
    # Slippage
    "entry_slippage_pct": 0.02,
    "exit_slippage_pct": 0.02,
    # Order timeouts
    "entry_timeout_seconds": 60,
    "exit_timeout_seconds": 30,
    # Retry logic
    "entry_max_retries": 3,
    # API settings
    "fyers_max_retries": 3,
    "fyers_retry_delay": 30,
    # Token management
    "token_valid_duration": 86400,
    "token_refresh_buffer": 3600,
    # Order management
    "order_max_retries": 3,
    "order_retry_delay": 30,
    # Pattern logging
    "log_all_patterns": True,
    # Black Swan Protection
    "enable_hedge_protection": True,  # Enable/disable hedge buying
    "hedge_strike_offset": 100,  # OTM offset for hedge (2 strikes away)
}

# =============================================================================
# FILTER CONFIGURATION (v7.4.0)
# =============================================================================

FILTERS: Dict[str, Any] = {
    "enabled": True,
    "f1_ma_alignment": {
        "enabled": True,
        "ma_period": 40,
    },
    "f2_rsi_alignment": {
        "enabled": True,
        "rsi_period": 14,
        "rsi_threshold": 50.0,
    },
    "f3_no_zigzag_short": {
        "enabled": True,
    },
    "f4_no_short_hour15": {
        "enabled": True,
        "hour_threshold": 15,
    },
    "log_filtered_trades": True,
    "log_filter_stats": True,
}

# =============================================================================
# DIRECTORY SETUP
# =============================================================================


def create_directories() -> None:
    """Create required directories."""
    for directory in [DATA_DIR, LOG_DIR, TRADES_DIR, REPORTS_DIR, STATE_DIR, TOKEN_DIR]:
        os.makedirs(directory, exist_ok=True)


def _init_holiday_file() -> None:
    """Create default holiday file if missing."""
    file_path = Path(HOLIDAY_FILE)
    if file_path.exists():
        return

    file_path.parent.mkdir(parents=True, exist_ok=True)

    template = {
        "2025": [
            {"date": d.strftime("%Y-%m-%d"), "name": "Holiday"}
            for d in sorted(_FALLBACK_HOLIDAYS_2025)
        ],
        "2026": [
            {"date": d.strftime("%Y-%m-%d"), "name": "Holiday"}
            for d in sorted(_FALLBACK_HOLIDAYS_2026)
        ],
    }

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2)
        logging.info("Created default holiday file: %s", file_path)
    except Exception as exc:
        logging.error("Failed to create holiday file: %s", exc)


# =============================================================================
# VALIDATION
# =============================================================================


def validate_config() -> bool:
    """
    Validate configuration parameters.

    Returns:
        True if valid

    Raises:
        SystemExit if invalid
    """
    errors = []

    # Validate strategy parameters
    s = STRATEGY

    # Numeric ranges
    if not (0 < s["renko_brick_pct"] < 0.01):
        errors.append(f"renko_brick_pct must be 0-0.01, got {s['renko_brick_pct']}")

    if s["initial_lots"] < 1 or s["initial_lots"] > s["max_lots"]:
        errors.append(f"Invalid lots: initial={s['initial_lots']}, max={s['max_lots']}")

    if s["max_daily_loss"] <= 0:
        errors.append(f"max_daily_loss must be positive: {s['max_daily_loss']}")

    if s["exit_trailing_bricks"] < 1:
        errors.append(f"exit_trailing_bricks must be >= 1: {s['exit_trailing_bricks']}")

    # Trading mode
    if TRADING_MODE not in ["PAPER", "LIVE", "BACKTEST"]:
        errors.append(f"Invalid TRADING_MODE: {TRADING_MODE}")

    # Pattern validation
    if PATTERNS.get("enabled"):
        for pattern_type in ["bullish", "bearish"]:
            if pattern_type not in PATTERNS:
                errors.append(f"Missing pattern type: {pattern_type}")
                continue

            for pattern_name in ["three_back", "two_back", "one_back", "zigzag"]:
                if pattern_name not in PATTERNS[pattern_type]:
                    errors.append(f"Missing pattern: {pattern_type}.{pattern_name}")
                    continue

                pattern = PATTERNS[pattern_type][pattern_name]
                if "sequence" not in pattern or not pattern["sequence"]:
                    errors.append(f"Invalid sequence for {pattern_type}.{pattern_name}")

    # Report errors
    if errors:
        print("\n❌ CONFIGURATION ERRORS:")
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}")
        sys.exit(1)

    return True


def print_config_summary() -> None:
    """Print configuration summary."""
    sep = "=" * SEPARATOR_LENGTH
    print(f"\n{sep}")
    print("TRADING SYSTEM CONFIGURATION")
    print(sep)
    print(f"Mode: {TRADING_MODE}")
    print("Strategy: Pattern-Based Renko")
    print(f"Renko Brick: {STRATEGY['renko_brick_pct'] * 100:.4f}%")
    print(f"Reversal: {STRATEGY.get('renko_reversal', 2)} bricks")
    print(f"Exit: {STRATEGY['exit_trailing_bricks']}-brick trailing stop")
    print(f"Patterns: {'Enabled' if PATTERNS['enabled'] else 'Disabled'}")
    print(f"Position: {STRATEGY['initial_lots']}-{STRATEGY['max_lots']} lots")
    print(f"Risk: Max Loss ₹{STRATEGY['max_daily_loss']:,}")
    print(f"Filters v7.4.0: {'Enabled' if FILTERS.get('enabled') else 'Disabled'}")
    print(f"Trading Hours: {STRATEGY['trading_start']}-{STRATEGY['trading_end']}")
    print(f"Entry Start: {STRATEGY['entry_start_time']}")
    print(sep)


# =============================================================================
# FUTURES SYMBOL GENERATION (Auto-Rollover)
# =============================================================================

# Data source selection
USE_FUTURES_DATA = True  # True = Futures, False = Index

# Rollover settings
FUTURES_ROLLOVER_DAYS = 2  # Roll to next month 2 days before expiry


def get_last_thursday_of_month(year: int, month: int) -> date:
    """
    Get last Thursday of a given month.

    Args:
        year: Year
        month: Month (1-12)

    Returns:
        Date of last Thursday
    """
    # Get last day of month
    if month == 12:
        last_day = date(year, 12, 31)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)

    # Find last Thursday (weekday 3)
    while last_day.weekday() != 3:
        last_day -= timedelta(days=1)

    return last_day


def get_current_futures_month(reference_date: Optional[date] = None) -> date:
    """
    Get the futures month to use based on rollover logic.

    Rolls to next month when within FUTURES_ROLLOVER_DAYS of expiry.

    Args:
        reference_date: Date to check (default: today)

    Returns:
        Date representing the futures month to use
    """
    if reference_date is None:
        reference_date = get_current_date()

    # Get expiry of current month
    current_expiry = get_last_thursday_of_month(
        reference_date.year, reference_date.month
    )

    # Calculate days to expiry
    days_to_expiry = (current_expiry - reference_date).days

    # If within rollover window, use next month
    if days_to_expiry < FUTURES_ROLLOVER_DAYS:
        if reference_date.month == 12:
            next_month = date(reference_date.year + 1, 1, 1)
        else:
            next_month = date(reference_date.year, reference_date.month + 1, 1)
        return next_month
    else:
        return reference_date


def generate_futures_symbol(reference_date: Optional[date] = None) -> str:
    """
    Generate Nifty Futures symbol for current/next month.

    Format: NSE:NIFTY{YY}{MONTH}FUT
    Example: NSE:NIFTY25JANFUT

    Args:
        reference_date: Date to generate symbol for (default: today)

    Returns:
        Futures symbol string
    """
    futures_month = get_current_futures_month(reference_date)

    # Year (2 digits)
    year_2digit = futures_month.strftime("%y")

    # Month (3 letters, uppercase)
    month_3letter = futures_month.strftime("%b").upper()

    symbol = f"NSE:NIFTY{year_2digit}{month_3letter}FUT"

    return symbol


def get_trading_symbol() -> str:
    """
    Get symbol for trading data (OHLCV + option strikes).

    Returns:
        Futures symbol (if USE_FUTURES_DATA=True) or Index symbol
    """
    if USE_FUTURES_DATA:
        return generate_futures_symbol()
    else:
        return NIFTY_INDEX_SYMBOL


# =============================================================================
# INITIALIZATION (runs on import)
# =============================================================================

create_directories()
_init_holiday_file()
validate_config()
print_config_summary()


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Timezone
    "IST",
    "get_current_time",
    "get_current_date",
    "make_aware",
    # Timing
    "MARKET_OPEN",
    "MARKET_CLOSE",
    "MARKET_CLOSE_BUFFER",
    "EXPIRY_EXIT_PRIMARY",
    "EXPIRY_EXIT_SECONDARY",
    "EXPIRY_EXIT_FINAL",
    "MAIN_LOOP_SLEEP",
    "WAIT_FOR_TRADING_SLEEP",
    "STATE_SAVE_INTERVAL",
    "SECONDS_IN_HOUR",
    "SECONDS_IN_DAY",
    # Black Swan Protection
    "HEDGE_BUY_TIME",
    "NEXT_DAY_HEDGE_EXIT_TIME",
    "HEDGE_STRIKE_OFFSET",
    # Data limits
    "MAX_HISTORICAL_BARS",
    "MIN_BARS_FOR_INDICATORS",
    "MIN_BARS_FOR_RENKO",
    "DEFAULT_WARMUP_DAYS",
    # API
    "FYERS_SUCCESS_CODE",
    "FYERS_RATE_LIMIT_CODE",
    "NIFTY_INDEX_SYMBOL",
    "NIFTY_OPTION_BASE",
    "API_MAX_RETRIES",
    "API_TIMEOUT",
    "TOKEN_DIR",
    "MIN_TOKEN_LENGTH",
    # Options
    "STRIKE_INTERVAL",
    "MIN_OPTION_PRICE",
    "OPTION_TYPE_LENGTH",
    "STRIKE_DIGITS",
    "MONTH_CODES",
    # Files
    "DATA_DIR",
    "LOG_DIR",
    "TRADES_DIR",
    "REPORTS_DIR",
    "STATE_DIR",
    "MASTER_TRADES_FILE",
    "MASTER_SUMMARY_FILE",
    "MAX_LOG_FILE_SIZE",
    "MAX_LOG_BACKUP_COUNT",
    # Display
    "SEPARATOR_LENGTH",
    "TARGET_BRICK_COUNT",
    "TELEGRAM_TIMEOUT",
    # Calendar
    "TUESDAY",
    "is_holiday",
    "is_weekend",
    "is_trading_day",
    # Config
    "TRADING_MODE",
    "STRATEGY",
    "PATTERNS",
    "FILTERS",
    "create_directories",
    "validate_config",
    "print_config_summary",
    # Futures
    "USE_FUTURES_DATA",
    "FUTURES_ROLLOVER_DAYS",
    "get_trading_symbol",
    "generate_futures_symbol",
    "get_current_futures_month",
    "get_last_thursday_of_month",
]
