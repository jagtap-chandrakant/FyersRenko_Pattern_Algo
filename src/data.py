# src/data.py
"""
Data Engine Module

Handles data fetching from Fyers API including live data,
historical data, and option chain information.

Author: Chandrakant Jagtap
Version: 5.0.0 (Simplified)
"""

import logging
import time
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Optional

from fyers_apiv3 import fyersModel

from src.config import (
    get_current_time,
    get_current_date,
    is_trading_day,
    IST,
    NIFTY_INDEX_SYMBOL,
    FYERS_SUCCESS_CODE,
    FYERS_RATE_LIMIT_CODE,
    API_MAX_RETRIES,
    API_BASE_DELAY,
    RATE_LIMIT_SLEEP,
    MAX_HISTORICAL_BARS,
    DEFAULT_WARMUP_DAYS,
    MAX_WARMUP_DAYS,
    MARKET_OPEN,
    MARKET_CLOSE,
    get_trading_symbol,
    generate_futures_symbol,
)

from src.auth import FyersAuth


class DataFetchError(Exception):
    """Data fetching failure exception."""

    pass


class DataEngine:
    """
    Fetches market data from Fyers API.

    Handles historical data, live quotes, option chains with
    retry logic and circuit breaker protection.

    Usage:
        engine = DataEngine()
        bar = engine.get_latest_bar()
        warmup = engine.get_warmup_data(days=3)
    """

    # Circuit breaker settings
    MAX_FAILURES = 10
    CIRCUIT_RESET_TIME = 300  # 5 minutes

    # Validation limits
    MAX_PRICE_CHANGE_PCT = 10.0
    MIN_PRICE = 0.01

    def __init__(self) -> None:
        """Initialize data engine with authentication."""
        self._auth = FyersAuth()
        self._fyers: Optional[fyersModel.FyersModel] = None

        # Trading symbol (Futures or Index)
        self._trading_symbol = get_trading_symbol()

        # Circuit breaker state
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None

        # Validation state
        self._last_close: Optional[float] = None

        # Initialize client
        self._init_client()

        logging.info("DataEngine initialized with symbol: %s", self._trading_symbol)

    def _init_client(self) -> None:
        """Initialize Fyers client."""
        self._fyers = self._auth.get_client()
        if self._fyers:
            logging.info("DataEngine initialized successfully")
        else:
            logging.error("DataEngine: Failed to get Fyers client")

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def get_latest_bar(self) -> Optional[Dict[str, Any]]:
        """
        Fetch latest 1-minute bar with robust error handling.

        Returns:
            Bar dictionary with timestamp, open, high, low, close, volume
            or None if fetch fails
        """
        if not self._ensure_client():
            logging.error("❌ Fyers client not available")
            return None

        if self._is_circuit_open():
            logging.error("❌ Circuit breaker open - skipping data fetch")
            return None

        # Refresh symbol (in case rollover happened)
        self._trading_symbol = get_trading_symbol()

        # Use last 2 days to ensure we get data even at market open
        end_date = get_current_date()
        start_date = end_date - timedelta(days=2)

        date_from = start_date.strftime("%Y-%m-%d")
        date_to = end_date.strftime("%Y-%m-%d")

        try:
            logging.debug("📊 Fetching bars: %s to %s", date_from, date_to)

            response = self._fyers.history(
                {
                    "symbol": self._trading_symbol,
                    "resolution": "1",
                    "date_format": "1",
                    "range_from": date_from,
                    "range_to": date_to,
                    "cont_flag": "1",
                }
            )

            # Check response code
            if response.get("code") != FYERS_SUCCESS_CODE:
                if response.get("code") == FYERS_RATE_LIMIT_CODE:
                    logging.warning("⚠️ Rate limited - waiting %ds", RATE_LIMIT_SLEEP)
                    time.sleep(RATE_LIMIT_SLEEP)
                else:
                    logging.warning("⚠️ History API failed: %s", response)
                self._record_failure()
                return None

            # Get candles
            candles = response.get("candles", [])

            if not candles:
                logging.warning(
                    "⚠️ No candles in response (market might not be open yet)"
                )
                return None

            # Parse latest bar
            bar = self._parse_candle(candles[-1])

            if not bar:
                logging.warning("⚠️ Failed to parse latest candle")
                return None

            # Validate bar
            if not self._validate_bar(bar):
                logging.warning("⚠️ Bar validation failed")
                return None

            # Success
            self._record_success()
            self._last_close = bar["close"]

            # Log success
            logging.info(
                "✅ Bar fetched: %s | Close: %.2f | Volume: %.0f",
                bar["timestamp"].strftime("%H:%M:%S"),
                bar["close"],
                bar["volume"],
            )

            return bar

        except Exception as exc:
            self._record_failure()
            logging.error("❌ Failed to fetch latest bar: %s", exc, exc_info=True)
            return None

    def get_warmup_data(self, days: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetch warmup data for indicator calculation.

        Args:
            days: Number of days to fetch (default: 3, max: 5)

        Returns:
            List of bar dictionaries
        """
        if not self._ensure_client():
            return []

        days = min(days or DEFAULT_WARMUP_DAYS, MAX_WARMUP_DAYS)

        # ✅ FIX: Include today if market is open
        if self.is_market_open():
            end_date = get_current_date()  # Today
            logging.info("Market is open - including today's data in warmup")
        else:
            end_date = get_current_date() - timedelta(days=1)  # Yesterday
            logging.info("Market is closed - using yesterday as end date")

        start_date = end_date - timedelta(
            days=days + 10
        )  # Extra buffer for holidays/weekends

        # Get trading days
        trading_days = self._get_trading_days(start_date, end_date)
        trading_days = trading_days[-days:]  # Take last N trading days

        logging.info("Fetching warmup data for %d trading days", len(trading_days))

        all_bars = []
        for trading_day in trading_days:
            bars = self._fetch_day_data(trading_day)
            filtered = self._filter_trading_hours(bars)
            all_bars.extend(filtered)

            # Rate limiting
            time.sleep(0.05)

            if len(all_bars) >= MAX_HISTORICAL_BARS:
                break

        logging.info("Fetched %d warmup bars", len(all_bars))
        return all_bars

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Get real-time quotes for symbols.

        Args:
            symbols: List of symbols (e.g., ["NSE:NIFTY2511326300PE"])

        Returns:
            Dictionary mapping symbol to quote data
        """
        if not self._ensure_client() or not symbols:
            return {}

        try:
            symbol_string = ",".join(symbols)
            response = self._fyers.quotes({"symbols": symbol_string})

            if response.get("code") != FYERS_SUCCESS_CODE:
                logging.warning("Quotes API failed: %s", response)
                return {}

            quotes = {}
            for item in response.get("d", []):
                symbol = item.get("n", "")
                values = item.get("v", {})

                if not values:
                    continue

                ltp = values.get("lp", 0.0)
                if ltp <= 0:
                    continue

                quotes[symbol] = {
                    "ltp": ltp,
                    "bid": values.get("bid", 0.0),
                    "ask": values.get("ask", 0.0),
                    "volume": values.get("volume", 0),
                    "oi": values.get("oi", 0),
                    "change": values.get("ch", 0.0),
                    "change_pct": values.get("chp", 0.0),
                }

            return quotes

        except Exception as exc:
            logging.error("Failed to fetch quotes: %s", exc)
            return {}

    def get_option_chain(
        self, symbol: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch option chain data.

        Args:
            symbol: Index symbol (default: NIFTY)

        Returns:
            Option chain data or None
        """
        if not self._ensure_client():
            return None

        symbol = symbol or NIFTY_INDEX_SYMBOL

        try:
            response = self._fyers.optionchain(
                {
                    "symbol": symbol,
                    "strikecount": 10,
                    "timestamp": "",
                }
            )

            if response.get("code") == FYERS_SUCCESS_CODE:
                return response.get("data")

            logging.warning("Option chain API failed: %s", response)
            return None

        except Exception as exc:
            logging.error("Failed to fetch option chain: %s", exc)
            return None

    def get_fyers_client(self) -> Optional[fyersModel.FyersModel]:
        """
        Get underlying Fyers client for direct API access.

        Returns:
            FyersModel instance or None
        """
        return self._fyers

    def is_market_open(self) -> bool:
        """Check if market is currently open."""
        now = get_current_time()

        if not is_trading_day(now.date()):
            return False

        current_time = now.time()
        return MARKET_OPEN <= current_time <= MARKET_CLOSE

    def refresh_client(self) -> bool:
        """Refresh Fyers client (re-authenticate if needed)."""
        self._fyers = self._auth.get_client()
        return self._fyers is not None

    # =========================================================================
    # INTERNAL METHODS
    # =========================================================================

    def _ensure_client(self) -> bool:
        """Ensure Fyers client is available."""
        if self._fyers:
            return True

        self._fyers = self._auth.get_client()
        return self._fyers is not None

    def _fetch_day_data(self, fetch_date: date) -> List[Dict[str, Any]]:
        """Fetch data for a single day."""
        # Generate symbol for this specific date (handles historical rollover)
        # Always use the current active futures contract (do not roll back for historical dates)
        symbol = (
            self._trading_symbol
        )  # This is already set to current month in __init__

        date_str = fetch_date.strftime("%Y-%m-%d")

        for attempt in range(API_MAX_RETRIES):
            try:
                response = self._fyers.history(
                    {
                        "symbol": symbol,
                        "resolution": "1",
                        "date_format": "1",
                        "range_from": date_str,
                        "range_to": date_str,
                        "cont_flag": "1",
                    }
                )

                if response.get("code") == FYERS_SUCCESS_CODE:
                    candles = response.get("candles", [])
                    return [self._parse_candle(c) for c in candles if c]

                if response.get("code") == FYERS_RATE_LIMIT_CODE:
                    time.sleep(RATE_LIMIT_SLEEP)
                    continue

                logging.warning("Failed to fetch %s: %s", date_str, response)
                return []

            except Exception as exc:
                logging.error(
                    "Error fetching %s (attempt %d): %s", date_str, attempt + 1, exc
                )
                if attempt < API_MAX_RETRIES - 1:
                    time.sleep(API_BASE_DELAY ** (attempt + 1))

        return []

    def _parse_candle(self, candle: List[Any]) -> Optional[Dict[str, Any]]:
        """
        Parse candle array to dictionary with timezone-aware timestamp.

        Args:
            candle: Candle data from Fyers API [timestamp, o, h, l, c, v]

        Returns:
            Bar dictionary with timezone-aware timestamp or None
        """
        if not candle or len(candle) < 6:
            return None

        try:
            # Fyers returns epoch timestamp (seconds since 1970-01-01 UTC)
            timestamp = datetime.fromtimestamp(candle[0], tz=IST)

            return {
                "timestamp": timestamp,
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
            }
        except (ValueError, TypeError) as exc:
            logging.warning("Failed to parse candle: %s", exc)
            return None

    def _validate_bar(self, bar: Dict[str, Any]) -> bool:
        """Validate bar data integrity."""
        open_price, high_price, low_price, close_price = (
            bar["open"],
            bar["high"],
            bar["low"],
            bar["close"],
        )

        # OHLC relationship
        if not (
            low_price <= open_price <= high_price
            and low_price <= close_price <= high_price
            and low_price <= high_price
        ):
            logging.warning(
                "Invalid OHLC: O=%.2f H=%.2f L=%.2f C=%.2f",
                open_price,
                high_price,
                low_price,
                close_price,
            )
            return False

        # Positive prices
        if any(
            p <= self.MIN_PRICE
            for p in [open_price, high_price, low_price, close_price]
        ):
            logging.warning("Non-positive price detected")
            return False

        # Volume check
        if bar["volume"] < 0:
            logging.warning("Negative volume: %.0f", bar["volume"])
            return False

        # Price change check (if we have previous close)
        if self._last_close and self._last_close > 0:
            pct_change = abs((close_price - self._last_close) / self._last_close) * 100
            if pct_change > self.MAX_PRICE_CHANGE_PCT:
                logging.warning(
                    "Extreme price change: %.2f%% (%.2f -> %.2f)",
                    pct_change,
                    self._last_close,
                    close_price,
                )
                return False

        return True

    def _filter_trading_hours(self, bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter bars to trading hours only."""
        filtered = []
        for bar in bars:
            if bar is None:
                continue
            bar_time = bar["timestamp"].time()
            if MARKET_OPEN <= bar_time <= MARKET_CLOSE:
                filtered.append(bar)
        return filtered

    def _get_trading_days(self, start: date, end: date) -> List[date]:
        """Get trading days between two dates."""
        days = []
        current = start
        while current <= end:
            if is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    # =========================================================================
    # CIRCUIT BREAKER
    # =========================================================================

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open."""
        if self._failure_count < self.MAX_FAILURES:
            return False

        if self._last_failure_time is None:
            return False

        elapsed = time.time() - self._last_failure_time
        if elapsed >= self.CIRCUIT_RESET_TIME:
            logging.info("Data circuit breaker reset")
            self._failure_count = 0
            return False

        return True

    def _record_success(self) -> None:
        """Record successful fetch."""
        self._failure_count = 0
        self._last_failure_time = None

    def _record_failure(self) -> None:
        """Record failed fetch."""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._failure_count >= self.MAX_FAILURES:
            logging.warning(
                "Data circuit breaker OPEN after %d failures", self._failure_count
            )

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def cleanup(self) -> None:
        """Clean up resources."""
        self._fyers = None
        logging.info("DataEngine cleanup completed")


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_engine_instance: Optional[DataEngine] = None


def get_data_engine() -> DataEngine:
    """Get DataEngine singleton instance."""
    global _engine_instance

    if _engine_instance is None:
        _engine_instance = DataEngine()

    return _engine_instance


def get_latest_bar() -> Optional[Dict[str, Any]]:
    """Convenience function to get latest bar."""
    return get_data_engine().get_latest_bar()


def get_warmup_data(days: int = DEFAULT_WARMUP_DAYS) -> List[Dict[str, Any]]:
    """Convenience function to get warmup data."""
    return get_data_engine().get_warmup_data(days)


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "DataEngine",
    "DataFetchError",
    "get_data_engine",
    "get_latest_bar",
    "get_warmup_data",
]
