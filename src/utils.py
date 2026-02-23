# src/utils.py
"""
Utility Functions - Option symbols, expiry calculation, market timing.

IMPORTANT: This is a LONG-ONLY options system.
- LONG signal = Buy CE (Call) - bullish
- SHORT signal = Buy PE (Put) - bearish
- Both are BUY orders (we never short/write options)
"""

import json
import logging
from datetime import datetime, timedelta, time as dtime, date
from pathlib import Path
from typing import Optional, Dict, Any, Set

# ============================================================================
# CONSTANTS
# ============================================================================

# Strike calculation
STRIKE_INTERVAL = 50
MIN_STRIKE = 50
DEFAULT_ITM_PCT = 0.2  # 0.2% ITM

# Month codes for option symbols (Fyers uses 3-letter abbreviations)
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

# Calendar
TUESDAY = 1  # weekday() value (0=Monday, 1=Tuesday, 2=Wednesday, ...)
DAYS_IN_WEEK = 7
MAX_HOLIDAY_SEARCH_DAYS = 7

# Market timing
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
MARKET_CLOSE_BUFFER = dtime(15, 15)  # For expiry day exits

# Date formats
DATE_FORMAT_EXPIRY = "%d-%b-%Y"  # 16-Jan-2025
DATE_FORMAT_EXPIRY_ALT = "%d-%m-%Y"  # 16-01-2025

# Holiday file path
HOLIDAY_FILE = Path("config/nse_holidays.json")

# ============================================================================
# HOLIDAY MANAGEMENT (Configuration-based)
# ============================================================================

_holidays_cache: Optional[Set[date]] = None
_holidays_cache_time: Optional[float] = None
HOLIDAY_CACHE_TTL = 86400  # 24 hours


def load_holidays(force_reload: bool = False) -> Set[date]:
    """
    Load NSE holidays from JSON configuration file.

    File format (config/nse_holidays.json):
    {
        "2025": [
            {"date": "2025-01-26", "name": "Republic Day"},
            {"date": "2025-03-14", "name": "Mahashivratri"},
            ...
        ],
        "2026": [...]
    }

    Args:
        force_reload: Force reload from file (ignore cache)

    Returns:
        Set of holiday dates
    """
    global _holidays_cache, _holidays_cache_time

    # Check cache
    import time

    current_time = time.time()

    if not force_reload and _holidays_cache is not None:
        if (
            _holidays_cache_time
            and (current_time - _holidays_cache_time) < HOLIDAY_CACHE_TTL
        ):
            return _holidays_cache

    holidays: Set[date] = set()

    # Try to load from file
    if HOLIDAY_FILE.exists():
        try:
            with open(HOLIDAY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            for year_str, holiday_list in data.items():
                # Skip non-year keys (like "_note")
                if not year_str.isdigit():
                    continue

                if not isinstance(holiday_list, list):
                    continue

                for entry in holiday_list:
                    if isinstance(entry, dict) and "date" in entry:
                        try:
                            hol_date = datetime.strptime(
                                entry["date"], "%Y-%m-%d"
                            ).date()
                            holidays.add(hol_date)
                        except ValueError:
                            logging.warning(
                                "Invalid date format in holidays: %s", entry
                            )

            logging.info("Loaded %d holidays from %s", len(holidays), HOLIDAY_FILE)

        except json.JSONDecodeError as exc:
            logging.error("Failed to parse holiday file: %s", exc)
        except Exception as exc:
            logging.error("Failed to load holiday file: %s", exc)
    else:
        logging.warning("Holiday file not found: %s - using fallback", HOLIDAY_FILE)
        holidays = _get_fallback_holidays()

    # Update cache
    _holidays_cache = holidays
    _holidays_cache_time = current_time

    return holidays


def _get_fallback_holidays() -> Set[date]:
    """Fallback holidays if JSON file not available."""
    return {
        # 2025
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
        # 2026
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


def load_special_sessions() -> Set[date]:
    """
    Load special trading sessions from JSON file.

    Special sessions = Trading on weekends/holidays (e.g., Budget Day).

    Returns:
        Set of special session dates
    """
    special_sessions: Set[date] = set()

    if not HOLIDAY_FILE.exists():
        return special_sessions

    try:
        with open(HOLIDAY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Look for keys like "special_sessions_2025", "special_sessions_2026"
        for key, session_list in data.items():
            if not key.startswith("special_sessions_"):
                continue

            if not isinstance(session_list, list):
                continue

            for entry in session_list:
                if isinstance(entry, dict) and "date" in entry:
                    try:
                        session_date = datetime.strptime(
                            entry["date"], "%Y-%m-%d"
                        ).date()
                        special_sessions.add(session_date)
                    except ValueError:
                        logging.warning("Invalid date in special sessions: %s", entry)

        if special_sessions:
            logging.info("Loaded %d special trading sessions", len(special_sessions))

    except Exception as exc:
        logging.error("Failed to load special sessions: %s", exc)

    return special_sessions


def is_special_session(check_date: date) -> bool:
    """Check if date is a special trading session."""
    special_sessions = load_special_sessions()
    return check_date in special_sessions


def reload_holidays() -> int:
    """
    Force reload holidays from file.

    Returns:
        Number of holidays loaded
    """
    holidays = load_holidays(force_reload=True)
    return len(holidays)


# ============================================================================
# TRADING DAY HELPERS
# ============================================================================


def is_weekend(check_date: date) -> bool:
    """Check if date is Saturday (5) or Sunday (6)."""
    return check_date.weekday() >= 5


def is_holiday(check_date: date) -> bool:
    """Check if date is an NSE holiday."""
    holidays = load_holidays()
    return check_date in holidays


def is_trading_day(check_date: date) -> bool:
    """
    Check if date is a trading day.

    Logic:
    1. Special sessions override everything (trading on weekends/holidays)
    2. Weekends are not trading days
    3. Holidays are not trading days

    Args:
        check_date: Date to check

    Returns:
        True if trading day, False otherwise
    """
    # Special sessions override weekends/holidays
    if is_special_session(check_date):
        return True

    # Regular logic
    return not is_weekend(check_date) and not is_holiday(check_date)


# ============================================================================
# EXPIRY CALCULATION
# ============================================================================


def calculate_next_expiry(base_date: Optional[date] = None) -> date:
    """
    Calculate next weekly expiry (Tuesday or previous trading day if holiday).

    Args:
        base_date: Starting date (default: today)

    Returns:
        Next expiry date (Tuesday)
    """
    if base_date is None:
        base_date = date.today()
    elif isinstance(base_date, datetime):
        base_date = base_date.date()
    elif isinstance(base_date, str):
        base_date = datetime.strptime(base_date, "%Y-%m-%d").date()

    # Find next Tuesday
    days_until_tuesday = (TUESDAY - base_date.weekday()) % DAYS_IN_WEEK

    if days_until_tuesday == 0:
        # Today is Tuesday - get next week's Tuesday
        candidate = base_date + timedelta(days=DAYS_IN_WEEK)
    else:
        candidate = base_date + timedelta(days=days_until_tuesday)

    # Check if Tuesday is a trading day
    if is_trading_day(candidate):
        return candidate

    # Tuesday is holiday - find previous trading day
    logging.info("Tuesday %s is holiday, finding previous trading day", candidate)

    for offset in range(1, MAX_HOLIDAY_SEARCH_DAYS + 1):
        prev_day = candidate - timedelta(days=offset)
        if is_trading_day(prev_day):
            logging.info("Expiry adjusted: %s -> %s", candidate, prev_day)
            return prev_day

    return candidate


def parse_expiry_date(expiry_str: str) -> date:
    """
    Parse expiry date string to date object.

    Supports: DD-MMM-YYYY or DD-MM-YYYY
    """
    if not expiry_str or not expiry_str.strip():
        raise ValueError("Expiry string cannot be empty")

    expiry_str = expiry_str.strip()

    # Try DD-MMM-YYYY format
    try:
        return datetime.strptime(expiry_str, DATE_FORMAT_EXPIRY).date()
    except ValueError:
        pass

    # Try DD-MM-YYYY format
    try:
        return datetime.strptime(expiry_str, DATE_FORMAT_EXPIRY_ALT).date()
    except ValueError:
        raise ValueError(f"Invalid expiry format: {expiry_str}")


def format_expiry_for_symbol(expiry_date: date) -> Dict[str, str]:
    """Format expiry date for option symbol construction."""
    return {
        "day": expiry_date.strftime("%d"),
        "month_code": MONTH_CODES[expiry_date.month],
        "year_2digit": expiry_date.strftime("%y"),
    }


def is_expiry_day_today(expiry_str: str) -> bool:
    """Check if today is expiry day."""
    try:
        expiry_date = parse_expiry_date(expiry_str)
        return date.today() == expiry_date
    except ValueError as exc:
        logging.error("Error parsing expiry '%s': %s", expiry_str, exc)
        return False


# ============================================================================
# SYNTHETIC FUTURE FUNCTIONS
# ============================================================================


def calculate_atm_strike(nifty_price: float) -> int:
    """
    Calculate ATM (At-The-Money) strike.

    Args:
        nifty_price: Current Nifty price

    Returns:
        ATM strike (rounded to nearest 50)
    """
    atm_strike = round(nifty_price / STRIKE_INTERVAL) * STRIKE_INTERVAL
    return max(MIN_STRIKE, atm_strike)


def get_synthetic_future_symbols(
    nifty_price: float, signal: str, expiry_str: str
) -> Dict[str, Any]:
    """
    Generate symbols for synthetic future (ATM CE + ATM PE).

    LONG Signal (Bullish):
        - Buy ATM CE
        - Sell ATM PE

    SHORT Signal (Bearish):
        - Buy ATM PE
        - Sell ATM CE

    Args:
        nifty_price: Current Nifty price
        signal: "LONG" or "SHORT"
        expiry_str: Expiry date string (DD-MMM-YYYY)

    Returns:
        Dict with buy_symbol, sell_symbol, atm_strike
    """
    if nifty_price <= 0:
        raise ValueError(f"Invalid Nifty price: {nifty_price}")

    if signal not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid signal: {signal}. Must be 'LONG' or 'SHORT'")

    # Parse expiry
    try:
        expiry_date = parse_expiry_date(expiry_str)
        if expiry_date <= date.today():
            expiry_date = calculate_next_expiry()
            logging.info("Expiry rollover: %s -> %s", expiry_str, expiry_date)
    except ValueError:
        logging.warning("Invalid expiry '%s', using next expiry", expiry_str)
        expiry_date = calculate_next_expiry()

    # Calculate ATM strike
    atm_strike = calculate_atm_strike(nifty_price)

    # Format expiry for symbol: YY + M + DD
    year_2digit = expiry_date.strftime("%y")
    month_code = MONTH_CODES[expiry_date.month]
    day_2digit = expiry_date.strftime("%d")

    # Build CE and PE symbols
    ce_symbol = f"NSE:NIFTY{year_2digit}{month_code}{day_2digit}{atm_strike}CE"
    pe_symbol = f"NSE:NIFTY{year_2digit}{month_code}{day_2digit}{atm_strike}PE"

    if signal == "LONG":
        # Bullish: Buy CE + Sell PE
        buy_symbol = ce_symbol
        sell_symbol = pe_symbol
        buy_type = "CE"
        sell_type = "PE"
    else:
        # Bearish: Buy PE + Sell CE
        buy_symbol = pe_symbol
        sell_symbol = ce_symbol
        buy_type = "PE"
        sell_type = "CE"

    logging.info(
        "Synthetic %s: ATM=%d | Buy %s | Sell %s",
        signal,
        atm_strike,
        buy_type,
        sell_type,
    )

    return {
        "buy_symbol": buy_symbol,
        "sell_symbol": sell_symbol,
        "atm_strike": atm_strike,
        "buy_type": buy_type,
        "sell_type": sell_type,
    }


def parse_option_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Parse Fyers option symbol to extract components.

    Args:
        symbol: Option symbol (e.g., "NSE:NIFTY25116244500CE")

    Returns:
        Dict with strike, option_type, etc. or None if parsing fails
    """
    try:
        if not symbol or "NIFTY" not in symbol:
            return None

        parts = symbol.split("NIFTY")[1]

        if len(parts) < 7:
            return None

        option_type = parts[-2:]
        if option_type not in ("CE", "PE"):
            return None

        strike = int(parts[-7:-2])
        date_part = parts[:-7]

        if len(date_part) < 4:
            return None

        return {
            "strike": strike,
            "option_type": option_type,
            "year": f"20{date_part[:2]}",
            "month_code": date_part[2],
            "day": date_part[3:],
            "full_symbol": symbol,
        }

    except (IndexError, ValueError) as exc:
        logging.debug("Failed to parse symbol '%s': %s", symbol, exc)
        return None


def extract_strike_display(symbol: str) -> str:
    """Extract strike for display (e.g., "24450CE")."""
    parsed = parse_option_symbol(symbol)
    if parsed:
        return f"{parsed['strike']}{parsed['option_type']}"
    return "N/A"


# ============================================================================
# MARKET TIMING
# ============================================================================


def is_market_hours(
    current_time: Optional[dtime] = None,
    start: dtime = MARKET_OPEN,
    end: dtime = MARKET_CLOSE,
) -> bool:
    """Check if time is within market hours (09:15-15:30)."""
    if current_time is None:
        current_time = datetime.now().time()
    elif isinstance(current_time, datetime):
        current_time = current_time.time()

    return start <= current_time <= end


def is_market_about_to_close(threshold_time: dtime = MARKET_CLOSE_BUFFER) -> bool:
    """Check if market is about to close (default: after 15:15)."""
    return datetime.now().time() >= threshold_time


def get_current_time() -> datetime:
    """Get current datetime."""
    return datetime.now()


def get_current_date() -> date:
    """Get current date."""
    return date.today()


# ============================================================================
# SYNTHETIC FUTURE SYMBOLS FROM OPTIONCHAIN (v3 API)
# ============================================================================


def get_synthetic_future_symbols_from_optionchain_v3(
    nifty_price: float, signal: str, fyers_client, expiry_str: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Get synthetic future symbols from Fyers API v3 optionchain.

    Extracts the 'symbol' field directly from optionchain response.
    Handles both weekly and monthly expiries automatically.

    Based on Fyers API v3 documentation response structure.

    Args:
        nifty_price: Current Nifty price
        signal: "LONG" or "SHORT"
        fyers_client: Authenticated Fyers API v3 client
        expiry_str: Optional expiry date (DD-MMM-YYYY format)

    Returns:
        Dict with:
        - buy_symbol: Contract symbol to buy
        - sell_symbol: Contract symbol to sell
        - atm_strike: ATM strike price
        - buy_type: "CE" or "PE"
        - sell_type: "CE" or "PE"
        - buy_ltp: Buy leg LTP from optionchain
        - sell_ltp: Sell leg LTP from optionchain

        Returns None if fetch fails

    Example:
        symbols = get_synthetic_future_symbols_from_optionchain_v3(
            nifty_price=25900,
            signal="LONG",
            fyers_client=fyers_client,
            expiry_str="17-Feb-2026"
        )
        # Returns:
        # {
        #     "buy_symbol": "NSE:NIFTY26FEB25900CE",
        #     "sell_symbol": "NSE:NIFTY26FEB25900PE",
        #     "atm_strike": 25900,
        #     "buy_type": "CE",
        #     "sell_type": "PE",
        #     "buy_ltp": 97.2,
        #     "sell_ltp": 111.85
        # }
    """
    if nifty_price <= 0:
        raise ValueError(f"Invalid Nifty price: {nifty_price}")

    if signal not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid signal: {signal}. Must be 'LONG' or 'SHORT'")

    if not fyers_client:
        logging.error("❌ Fyers client not available")
        return None

    # Determine target expiry date
    if expiry_str:
        try:
            target_expiry_date = parse_expiry_date(expiry_str)
            if target_expiry_date <= date.today():
                target_expiry_date = calculate_next_expiry()
                logging.info("Expiry rolled over to: %s", target_expiry_date)
        except ValueError:
            logging.warning("Invalid expiry '%s', using next expiry", expiry_str)
            target_expiry_date = calculate_next_expiry()
    else:
        target_expiry_date = calculate_next_expiry()

    # Calculate ATM strike
    atm_strike = calculate_atm_strike(nifty_price)

    logging.info(
        "🎯 Fetching symbols: Expiry=%s, ATM Strike=%d, Signal=%s",
        target_expiry_date.strftime("%d-%b-%Y"),
        atm_strike,
        signal,
    )

    try:
        # Step 1: Fetch optionchain without timestamp to get available expiries
        logging.debug("📊 Fetching available expiries from optionchain...")

        response = fyers_client.optionchain(
            {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""}
        )

        if not response or response.get("code") != 200:
            logging.error("❌ Failed to fetch expiry data: %s", response)
            return None

        # Step 2: Find the timestamp for our target expiry
        expiry_data_list = response.get("data", {}).get("expiryData", [])

        if not expiry_data_list:
            logging.error("❌ No expiry data in response")
            return None

        target_timestamp = None

        for expiry_item in expiry_data_list:
            exp_date_str = expiry_item.get("date")  # Format: "25-04-2024"

            if not exp_date_str:
                continue

            try:
                # Parse date from Fyers format (DD-MM-YYYY)
                exp_date = datetime.strptime(exp_date_str, "%d-%m-%Y").date()

                if exp_date == target_expiry_date:
                    target_timestamp = expiry_item.get("expiry")
                    logging.info(
                        "✅ Found target expiry: %s (timestamp: %s)",
                        exp_date_str,
                        target_timestamp,
                    )
                    break
            except ValueError as e:
                logging.debug("Failed to parse expiry date '%s': %s", exp_date_str, e)
                continue

        if not target_timestamp:
            logging.error(
                "❌ Target expiry %s not found in available expiries",
                target_expiry_date.strftime("%d-%b-%Y"),
            )
            available = [e.get("date") for e in expiry_data_list]
            logging.info("Available expiries: %s", available)
            return None

        # Step 3: Fetch optionchain for the target expiry with timestamp
        logging.debug(
            "📊 Fetching optionchain for expiry timestamp: %s", target_timestamp
        )

        response = fyers_client.optionchain(
            {
                "symbol": "NSE:NIFTY50-INDEX",
                "strikecount": 10,
                "timestamp": str(target_timestamp),
            }
        )

        if not response or response.get("code") != 200:
            logging.error("❌ Failed to fetch optionchain: %s", response)
            return None

        # Step 4: Extract CE and PE symbols for ATM strike from optionchain
        options_chain = response.get("data", {}).get("optionsChain", [])

        if not options_chain:
            logging.error("❌ No options in chain for expiry")
            return None

        ce_symbol = None
        pe_symbol = None
        ce_ltp = None
        pe_ltp = None

        logging.debug(
            "🔍 Searching for ATM strike %d in %d options",
            atm_strike,
            len(options_chain),
        )

        for option in options_chain:
            strike = option.get("strike_price", 0)
            opt_type = option.get("option_type", "")
            symbol = option.get("symbol", "")
            ltp = option.get("ltp", 0)

            # Log nearby strikes for debugging
            if strike in [atm_strike - 50, atm_strike, atm_strike + 50]:
                logging.debug(
                    "  Strike: %d, Type: %s, Symbol: %s, LTP: %.2f",
                    strike,
                    opt_type,
                    symbol,
                    ltp,
                )

            if strike == atm_strike:
                if opt_type == "CE":
                    ce_symbol = symbol
                    ce_ltp = ltp
                    logging.debug("✅ Found CE: %s @ ₹%.2f", symbol, ltp)
                elif opt_type == "PE":
                    pe_symbol = symbol
                    pe_ltp = ltp
                    logging.debug("✅ Found PE: %s @ ₹%.2f", symbol, ltp)

            if ce_symbol and pe_symbol:
                break

        if not ce_symbol or not pe_symbol:
            available_strikes = sorted(
                set(o.get("strike_price") for o in options_chain)
            )
            logging.error(
                "❌ Could not find CE/PE symbols for strike %d",
                atm_strike,
            )
            logging.info("Available strikes: %s", available_strikes)
            return None

        # Step 5: Determine buy/sell based on signal
        if signal == "LONG":
            buy_symbol = ce_symbol
            sell_symbol = pe_symbol
            buy_type = "CE"
            sell_type = "PE"
            buy_ltp = ce_ltp
            sell_ltp = pe_ltp
        else:  # SHORT
            buy_symbol = pe_symbol
            sell_symbol = ce_symbol
            buy_type = "PE"
            sell_type = "CE"
            buy_ltp = pe_ltp
            sell_ltp = ce_ltp

        logging.info(
            "✅ Synthetic %s from optionchain: Buy %s @ ₹%.2f | Sell %s @ ₹%.2f",
            signal,
            buy_symbol,
            buy_ltp,
            sell_symbol,
            sell_ltp,
        )

        return {
            "buy_symbol": buy_symbol,
            "sell_symbol": sell_symbol,
            "atm_strike": atm_strike,
            "buy_type": buy_type,
            "sell_type": sell_type,
            "buy_ltp": buy_ltp,
            "sell_ltp": sell_ltp,
        }

    except Exception as exc:
        logging.error(
            "❌ Error fetching symbols from optionchain: %s",
            exc,
            exc_info=True,
        )
        return None


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Holidays (configuration-based)
    "load_holidays",
    "reload_holidays",
    "is_holiday",
    # Trading days
    "is_trading_day",
    "is_weekend",
    # Expiry
    "calculate_next_expiry",
    "parse_expiry_date",
    "is_expiry_day_today",
    "format_expiry_for_symbol",
    # Option symbols (LONG-ONLY)
    "parse_option_symbol",
    "extract_strike_display",
    # Market timing
    "is_market_hours",
    "is_market_about_to_close",
    "get_current_time",
    "get_current_date",
    # Constants
    "MARKET_OPEN",
    "MARKET_CLOSE",
    "MARKET_CLOSE_BUFFER",
    "DATE_FORMAT_EXPIRY",
    "STRIKE_INTERVAL",
    "DEFAULT_ITM_PCT",
    "HOLIDAY_FILE",
    # Synthetic futures
    "calculate_atm_strike",
    "get_synthetic_future_symbols",
    "get_synthetic_future_symbols_from_optionchain_v3",
]
