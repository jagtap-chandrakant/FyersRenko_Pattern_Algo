# src/alerts.py
"""
Alert System - Console and Telegram notifications.

Handles all system alerts including trade notifications,
daily summaries, risk alerts, and console status display.
"""

import logging
import os
import time
from datetime import datetime
from typing import Dict, Any

import requests
from dotenv import load_dotenv

load_dotenv("config/.env")

# ============================================================================
# CONFIGURATION
# ============================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
TELEGRAM_TIMEOUT = 30
TELEGRAM_MAX_RETRIES = 3

CAPITAL_PER_LOT = 100000  # For PnL percentage calculation

# Emoji mappings (with ASCII fallbacks)
EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "❌",
    "success": "✅",
    "trade": "📊",
    "green": "🟢",
    "red": "🔴",
    "white": "⚪",
    "fire": "🔥",
    "rocket": "🚀",
    "money": "💰",
    "chart": "📊",
    "exit": "🚪",
    "clock": "⏰",
    "target": "🎯",
    "pin": "📍",
}

# ============================================================================
# TELEGRAM SENDER
# ============================================================================


def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """
    Send message to Telegram with retry logic.

    Args:
        message: Message text (HTML formatted)
        parse_mode: Parse mode (HTML or Markdown)

    Returns:
        True if sent successfully, False otherwise
    """
    if not TELEGRAM_ENABLED:
        logging.debug("Telegram disabled - message not sent")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
    }

    for attempt in range(TELEGRAM_MAX_RETRIES):
        try:
            response = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)

            if response.status_code == 200:
                return True

            if response.status_code == 429:  # Rate limited
                retry_after = int(response.headers.get("Retry-After", 5))
                logging.warning("Telegram rate limited, waiting %ds", retry_after)
                time.sleep(retry_after)
                continue

            logging.warning(
                "Telegram send failed (attempt %d): %d - %s",
                attempt + 1,
                response.status_code,
                response.text[:100],
            )

        except requests.exceptions.Timeout:
            logging.warning("Telegram timeout (attempt %d)", attempt + 1)
        except requests.exceptions.RequestException as exc:
            logging.error("Telegram error (attempt %d): %s", attempt + 1, exc)

        if attempt < TELEGRAM_MAX_RETRIES - 1:
            time.sleep(2**attempt)  # Exponential backoff

    logging.error("All Telegram send attempts failed")
    return False


# ============================================================================
# ALERT FORMATTERS
# ============================================================================


def _extract_strike_from_symbol(symbol: str) -> str:
    """
    Extract strike price from option symbol.

    Args:
        symbol: Option symbol (e.g., "NSE:NIFTY2621025650PE")

    Returns:
        Strike price as string (e.g., "25650")
    """
    try:
        if not symbol or "NIFTY" not in symbol:
            return "N/A"

        # Split on NIFTY and get the part after it
        parts = symbol.split("NIFTY")[1]

        if len(parts) < 7:  # Minimum: YY + M + DD + STRIKE (5 digits) + TYPE (2 chars)
            return "N/A"

        # Extract strike (5 digits before CE/PE)
        option_type = parts[-2:]  # CE or PE
        strike = parts[-7:-2]  # 5 digits before type

        return strike

    except (IndexError, ValueError):
        return "N/A"


def format_trade_entry(trade: Dict[str, Any]) -> str:
    """
    Format synthetic future entry alert for Telegram.

    Shows both legs with clear buy/sell indication and net premium.

    Args:
        trade: Trade dictionary with entry details

    Returns:
        HTML-formatted message string
    """
    action = trade.get("action", "UNKNOWN")
    emoji = "🟢" if action == "LONG" else "🔴"

    pattern_name = trade.get("pattern_name", "Unknown")
    pattern_type = trade.get("pattern_type", "")

    timestamp = trade.get("timestamp")
    time_str = (
        timestamp.strftime("%H:%M:%S")
        if hasattr(timestamp, "strftime")
        else str(timestamp)[-8:]
    )

    buy_symbol = trade.get("buy_symbol", "")
    sell_symbol = trade.get("sell_symbol", "")
    buy_price = trade.get("buy_entry_price", 0)
    sell_price = trade.get("sell_entry_price", 0)
    net_premium = trade.get("net_premium", 0)
    atm_strike = trade.get("atm_strike", 0)
    lots = trade.get("lots", 0)
    nifty_price = trade.get("price", 0)

    # Extract option types and strikes
    buy_type = "CE" if "CE" in buy_symbol else "PE"
    sell_type = "CE" if "CE" in sell_symbol else "PE"

    # Extract strikes from symbols (e.g., "NSE:NIFTY2621025650PE" -> "25650")
    buy_strike = _extract_strike_from_symbol(buy_symbol)
    sell_strike = _extract_strike_from_symbol(sell_symbol)

    # Calculate net cost per lot
    net_cost_per_lot = net_premium * 65  # Assuming lot size 65

    # Strategy description
    if action == "LONG":
        strategy_desc = f"Buy {buy_strike}{buy_type} + Sell {sell_strike}{sell_type}"
    else:
        strategy_desc = f"Buy {buy_strike}{buy_type} + Sell {sell_strike}{sell_type}"

    return (
        f"<b>#{trade.get('trade_number', 0)} | {emoji} {action} SYNTHETIC | {time_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Pattern: <b>{pattern_name}</b> ({pattern_type})\n"
        f"📍 Nifty: <b>{nifty_price:,.2f}</b> | ATM: {atm_strike} | {lots}L\n"
        f"\n"
        f"<b>LEGS:</b>\n"
        f"🟢 BUY  {buy_strike}{buy_type}: ₹{buy_price:.2f}\n"
        f"🔴 SELL {sell_strike}{sell_type}: ₹{sell_price:.2f}\n"
        f"\n"
        f"<b>NET:</b>\n"
        f"💰 Premium: ₹{net_premium:.2f} per qty\n"
        f"💰 Cost: ₹{net_cost_per_lot:,.0f} per lot\n"
        f"💰 Total: ₹{net_cost_per_lot * lots:,.0f} ({lots}L)"
    )


def format_trade_exit(trade: Dict[str, Any]) -> str:
    """
    Format synthetic future exit alert for Telegram.

    Shows both legs with entry/exit prices, individual PnL, and net result.

    Args:
        trade: Trade dictionary with exit details

    Returns:
        HTML-formatted message string
    """
    pnl = trade.get("pnl", 0)
    emoji = "✅" if pnl >= 0 else "❌"

    exit_reason = trade.get("exit_reason", "trailing_stop")

    buy_leg_pnl = trade.get("buy_leg_pnl", 0)
    sell_leg_pnl = trade.get("sell_leg_pnl", 0)

    timestamp = trade.get("timestamp")
    time_str = (
        timestamp.strftime("%H:%M:%S")
        if hasattr(timestamp, "strftime")
        else str(timestamp)[-8:]
    )

    lots = trade.get("lots", 1)
    pnl_pct = (pnl / (lots * CAPITAL_PER_LOT)) * 100 if lots > 0 else 0

    buy_entry = trade.get("buy_entry_price", 0)
    buy_exit = trade.get("buy_exit_price", 0)
    sell_entry = trade.get("sell_entry_price", 0)
    sell_exit = trade.get("sell_exit_price", 0)

    holding_hours = trade.get("holding_hours", 0)
    costs = trade.get("costs", 0)

    # Extract symbols and strikes
    buy_symbol = trade.get("buy_symbol", "")
    sell_symbol = trade.get("sell_symbol", "")

    buy_type = "CE" if "CE" in buy_symbol else "PE"
    sell_type = "CE" if "CE" in sell_symbol else "PE"

    buy_strike = _extract_strike_from_symbol(buy_symbol)
    sell_strike = _extract_strike_from_symbol(sell_symbol)

    # Calculate gross PnL (before costs)
    gross_pnl = pnl + costs

    # Format exit reason
    exit_reason_formatted = exit_reason.replace("_", " ").title()

    return (
        f"<b>#{trade.get('trade_number', 0)} | {emoji} EXIT SYNTHETIC | {time_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Exit: <b>{exit_reason_formatted}</b>\n"
        f"⏱️ Hold: {holding_hours:.1f}h | {lots}L\n"
        f"\n"
        f"<b>BUY LEG ({buy_strike}{buy_type}):</b>\n"
        f"Entry: ₹{buy_entry:.2f} → Exit: ₹{buy_exit:.2f}\n"
        f"PnL: ₹{buy_leg_pnl:,.0f} ({_format_pnl_sign(buy_leg_pnl)})\n"
        f"\n"
        f"<b>SELL LEG ({sell_strike}{sell_type}):</b>\n"
        f"Entry: ₹{sell_entry:.2f} → Exit: ₹{sell_exit:.2f}\n"
        f"PnL: ₹{sell_leg_pnl:,.0f} ({_format_pnl_sign(sell_leg_pnl)})\n"
        f"\n"
        f"<b>NET RESULT:</b>\n"
        f"Gross: ₹{gross_pnl:,.0f}\n"
        f"Costs: ₹{costs:,.0f}\n"
        f"<b>Net: ₹{pnl:,.0f} ({pnl_pct:+.2f}%)</b>"
    )


def format_daily_summary(report: Dict[str, Any]) -> str:
    """Format daily summary report with open position support."""
    initial_capital = report.get("initial_capital", CAPITAL_PER_LOT * 2)
    final_equity = report.get("final_equity", initial_capital)
    realized_pnl = report.get("realized_pnl", report.get("total_pnL", 0))
    unrealized_pnl = report.get("unrealized_pnl", 0)
    total_pnl = report.get("total_pnl", realized_pnl + unrealized_pnl)

    return_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0

    # Build pattern breakdown
    pattern_section = _format_pattern_breakdown(report)

    # Build capital section with broker data
    capital_section = _format_capital_section(report)

    # Build open position section
    open_position_section = _format_open_position_section(report)

    return (
        f"<b>📊 DAILY SUMMARY | {report.get('date', 'Today')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>📈 PERFORMANCE</b>\n"
        f"Completed: {report.get('completed_trades', report.get('total_trades', 0))} trades | "
        f"Win: {report.get('win_rate', 0) * 100:.1f}%\n"
        f"Realized PnL: ₹{realized_pnl:,.0f}\n"
        f"{open_position_section}"
        f"<b>💰 TOTAL: ₹{total_pnl:,.0f}</b> ({return_pct:+.2f}%)\n"
        f"Max DD: ₹{report.get('max_drawdown', 0):,.0f} "
        f"({report.get('max_drawdown_pct', 0):.2f}%)\n\n"
        f"{pattern_section}\n\n"
        f"{capital_section}"
    )


def _format_open_position_section(report: Dict[str, Any]) -> str:
    """Format open position section for Telegram."""
    if not report.get("has_open_position"):
        return ""

    open_pos = report.get("open_position", {})
    if not open_pos:
        return ""

    direction = open_pos.get("direction", "UNKNOWN")
    direction_emoji = "🟢" if direction == "LONG" else "🔴"
    unrealized = report.get("unrealized_pnl", 0)
    unrealized_sign = "+" if unrealized >= 0 else ""

    lines = [
        f"\n<b>⚠️ OPEN POSITION</b>",
        f"{direction_emoji} {direction} Synthetic | {open_pos.get('lots', 1)} lot(s)",
    ]

    if open_pos.get("entry_price"):
        lines.append(f"Entry: {open_pos['entry_price']:,.2f}")

    if open_pos.get("current_price"):
        lines.append(f"Current: {open_pos['current_price']:,.2f}")

    lines.append(f"Unrealized: ₹{unrealized_sign}{unrealized:,.0f}")

    if open_pos.get("hedge_active"):
        lines.append(f"🛡️ Hedge: Active (exit 09:16 tomorrow)")

    lines.append("")  # Empty line before total

    return "\n".join(lines)


def _format_pattern_breakdown(report: Dict[str, Any]) -> str:
    """
    Format pattern-level breakdown for LONG and SHORT trades.

    Args:
        report: Daily report dictionary

    Returns:
        Formatted pattern breakdown string (HTML)
    """
    lines = ["<b>📊 PATTERN BREAKDOWN</b>"]

    # LONG trades
    long_total = report.get("long_trades", 0)
    long_wins = report.get("long_wins", 0)
    long_losses = report.get("long_losses", 0)
    long_patterns = report.get("long_pattern_stats", {})

    if long_total > 0:
        lines.append(
            f"\n<b>🟢 LONG:</b> Total: {long_total} | Win: {long_wins} | Loss: {long_losses}"
        )

        if long_patterns:
            for pattern_name, stats in sorted(long_patterns.items()):
                total = stats.get("total", 0)
                wins = stats.get("wins", 0)
                losses = stats.get("losses", 0)
                win_rate = (wins / total * 100) if total > 0 else 0
                lines.append(
                    f"  • {pattern_name}: {total} ({wins}W/{losses}L) - {win_rate:.0f}%"
                )
        else:
            lines.append("  • No pattern data")
    else:
        lines.append("\n<b>🟢 LONG:</b> No trades")

    # SHORT trades
    short_total = report.get("short_trades", 0)
    short_wins = report.get("short_wins", 0)
    short_losses = report.get("short_losses", 0)
    short_patterns = report.get("short_pattern_stats", {})

    if short_total > 0:
        lines.append(
            f"\n<b>🔴 SHORT:</b> Total: {short_total} | Win: {short_wins} | Loss: {short_losses}"
        )

        if short_patterns:
            for pattern_name, stats in sorted(short_patterns.items()):
                total = stats.get("total", 0)
                wins = stats.get("wins", 0)
                losses = stats.get("losses", 0)
                win_rate = (wins / total * 100) if total > 0 else 0
                lines.append(
                    f"  • {pattern_name}: {total} ({wins}W/{losses}L) - {win_rate:.0f}%"
                )
        else:
            lines.append("  • No pattern data")
    else:
        lines.append("\n<b>🔴 SHORT:</b> No trades")

    return "\n".join(lines)


def _format_capital_section(report: Dict[str, Any]) -> str:
    """
    Format capital section with broker data comparison.

    Shows:
    - Broker capital (actual from broker API)
    - System calculation (for comparison)
    - Variance between broker and system
    - Brokerage charges breakdown

    Args:
        report: Daily report dictionary

    Returns:
        Formatted capital section string (HTML)
    """
    broker_start = report.get("broker_capital_start")
    broker_end = report.get("broker_capital_end")
    broker_diff = report.get("broker_capital_diff")
    broker_brokerage = report.get("broker_brokerage", 0.0)

    system_pnl = report.get("system_pnl", 0.0)
    broker_pnl = report.get("broker_pnl")
    variance = report.get("variance")

    lines = ["<b>💼 CAPITAL</b>"]

    # If broker data available, show broker as primary
    if broker_start is not None and broker_end is not None:
        lines.append(f"\n<b>Broker (Actual):</b>")
        lines.append(f"  Start: ₹{broker_start:,.2f}")
        lines.append(f"  EOD: ₹{broker_end:,.2f}")
        lines.append(f"  Day PnL: ₹{broker_diff:+,.2f}")

        # Brokerage charges breakdown
        if broker_brokerage > 0:
            lines.append(f"  Charges: ₹{broker_brokerage:,.2f}")
            net_after_charges = broker_diff
            lines.append(f"  Net: ₹{net_after_charges:+,.2f}")

        # System calculation (for comparison)
        lines.append(f"\n<b>System (Calculated):</b>")
        lines.append(f"  PnL: ₹{system_pnl:+,.2f}")

        # Variance analysis
        if variance is not None and abs(variance) > 1:
            variance_pct = (variance / abs(system_pnl) * 100) if system_pnl != 0 else 0
            lines.append(f"\n<b>Variance:</b>")
            lines.append(f"  Diff: ₹{variance:+,.2f} ({variance_pct:+.1f}%)")

            # Explain variance
            if abs(variance - broker_brokerage) < 10:
                lines.append(f"  Reason: Brokerage charges")
            elif abs(variance) > 100:
                lines.append(f"  ⚠️ Significant variance - review trades")

    else:
        # No broker data - show system calculation only
        initial_capital = report.get("initial_capital", CAPITAL_PER_LOT)
        final_equity = report.get("final_equity", CAPITAL_PER_LOT)
        system_diff = final_equity - initial_capital

        lines.append(f"\n<b>System (Broker data unavailable):</b>")
        lines.append(f"  Start: ₹{initial_capital:,.2f}")
        lines.append(f"  EOD: ₹{final_equity:,.2f}")
        lines.append(f"  PnL: ₹{system_diff:+,.2f}")

    # Position info
    lines.append(
        f"\nMax Lots: {report.get('max_lots_used', 0)} | "
        f"Open: {report.get('open_positions', 0)}"
    )

    return "\n".join(lines)


def format_risk_alert(risk_type: str, details: str) -> str:
    """Format risk management alert."""
    templates = {
        "consecutive_losses": (
            "<b>⚠️ RISK ALERT | CONSECUTIVE LOSSES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Limit: {details} consecutive losses\n"
            "Status: <b>TRADING STOPPED</b>\n"
            "Action: Review strategy parameters"
        ),
        "daily_loss": (
            "<b>⚠️ RISK ALERT | DAILY LOSS LIMIT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Loss: ₹{details}\n"
            "Status: <b>TRADING STOPPED</b>\n"
            "Action: Review risk management"
        ),
        "volatility": (
            "<b>⚠️ RISK ALERT | HIGH VOLATILITY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"ADX: {details}\n"
            "Status: <b>ENTRIES PAUSED</b>"
        ),
    }
    return templates.get(risk_type, f"<b>⚠️ RISK ALERT</b>\n{risk_type}: {details}")


def format_system_alert(system_type: str, details: str = "") -> str:
    """Format system status alert."""
    templates = {
        "startup": f"<b>🚀 SYSTEM STARTUP</b>\n━━━━━━━━━━━━━━━━━━━━━\nMode: {details}\nStatus: <b>READY</b>",
        "shutdown": "<b>💤 SYSTEM SHUTDOWN</b>\n━━━━━━━━━━━━━━━━━━━━━\nStatus: <b>COMPLETE</b>",
        "auth_success": "<b>✅ AUTHENTICATION</b>\n━━━━━━━━━━━━━━━━━━━━━\nFyers API: <b>CONNECTED</b>",
        "auth_failed": "<b>❌ AUTHENTICATION</b>\n━━━━━━━━━━━━━━━━━━━━━\nFyers API: <b>FAILED</b>\nAction: Check credentials",
        "info": f"<b>ℹ️ SYSTEM</b>\n{details}",
    }
    return templates.get(system_type, f"<b>ℹ️ SYSTEM</b>\n{system_type}: {details}")


def _extract_strike(symbol: str) -> str:
    """
    Extract strike price with option type from symbol.

    Args:
        symbol: Option symbol (e.g., "NSE:NIFTY2621025650PE")

    Returns:
        Strike with type (e.g., "25650PE")
    """
    try:
        if not symbol or "NIFTY" not in symbol:
            return "N/A"

        parts = symbol.split("NIFTY")[1]

        if len(parts) < 7:  # Minimum: YY M DD STRIKE TYPE
            return "N/A"

        option_type = parts[-2:]  # CE or PE
        strike = parts[-7:-2]  # 5 digits before type

        return f"{strike}{option_type}"

    except (IndexError, ValueError):
        return "N/A"


# ============================================================================
# PUBLIC ALERT FUNCTIONS
# ============================================================================


def send_trade_alert(trade: Dict[str, Any]) -> None:
    """Send trade alert (entry or exit) to console and Telegram."""
    action = trade.get("action", "")

    if action == "EXIT":
        message = format_trade_exit(trade)
    elif action in ("LONG", "SHORT"):
        message = format_trade_entry(trade)
    else:
        message = f"📊 <b>TRADE</b>\n{trade}"

    # Console
    logging.info("TRADE ALERT:\n%s", _strip_html(message))

    # Telegram
    send_telegram(message)


def send_daily_summary(report: Dict[str, Any]) -> None:
    """Send daily summary to console and Telegram."""
    message = format_daily_summary(report)
    logging.info("DAILY SUMMARY:\n%s", _strip_html(message))
    send_telegram(message)


def send_risk_alert(risk_type: str, details: str = "") -> None:
    """Send risk alert to console and Telegram."""
    message = format_risk_alert(risk_type, details)
    logging.warning("RISK ALERT:\n%s", _strip_html(message))
    send_telegram(message)


def send_system_alert(system_type: str, details: str = "") -> None:
    """Send system alert to console and Telegram."""
    message = format_system_alert(system_type, details)
    logging.info("SYSTEM ALERT:\n%s", _strip_html(message))
    send_telegram(message)


def send_alert(message: str, alert_type: str = "info") -> None:
    """Send generic alert."""
    emoji = EMOJI.get(alert_type, "ℹ️")
    full_message = f"{emoji} {message}"
    logging.info(full_message)
    send_telegram(full_message)


def _strip_html(text: str) -> str:
    """Strip HTML tags for console output."""
    import re

    return re.sub(r"<[^>]+>", "", text)


# ============================================================================
# CONSOLE DISPLAY FUNCTIONS
# ============================================================================


def print_market_status(state: Dict[str, Any], current_time: datetime) -> None:
    """Print market status to console."""
    if not state:
        return

    # Build Renko visual
    bricks = state.get("last_10_bricks", [])
    renko_visual = "".join(
        "🟢" if b == 1 else "🔴" if b == -1 else "⚪" for b in bricks[-10:]
    )

    # Get pattern info
    pattern_name = state.get("last_pattern_name", "None")
    pattern_type = state.get("last_pattern_type", "")

    print(f"\n📊 MARKET STATUS | {current_time.strftime('%H:%M:%S')}")
    print(
        f"   Nifty: {state.get('nifty_price', 0):,.2f} | "
        f"Renko: {renko_visual} | "
        f"Position: {state.get('position', 0)}"
    )
    print(f"   Last Pattern: {pattern_name} ({pattern_type})")


def print_brick_formation(
    count: int, direction: int, price: float, timestamp: datetime
) -> None:
    """Print brick formation event."""
    emoji = "🟢" if direction == 1 else "🔴"
    direction_str = "GREEN" if direction == 1 else "RED"
    time_str = timestamp.strftime("%H:%M:%S")

    print(f"\n{'=' * 50}")
    if count > 1:
        print(
            f"{emoji} {count} {direction_str} BRICKS FORMED @ {price:,.2f} ({time_str})"
        )
    else:
        print(f"{emoji} {direction_str} BRICK FORMED @ {price:,.2f} ({time_str})")
    print(f"{'=' * 50}")


def print_signal(signal: Dict[str, Any], trade_number: int = 0) -> None:
    """Print signal generation to console."""
    if not signal:
        return

    action = signal.get("action", "")
    timestamp = signal.get("timestamp")
    time_str = timestamp.strftime("%H:%M:%S") if hasattr(timestamp, "strftime") else ""

    if action == "LONG":
        pattern_name = signal.get("pattern_name", "Unknown")
        pattern_type = signal.get("pattern_type", "")

        print(
            f"\n🔥 #{trade_number} | {time_str} | 🟢 LONG SIGNAL | "
            f"Pattern: {pattern_name} ({pattern_type})\n"
            f"   Nifty: {signal.get('price', 0):,.2f}"
        )

    elif action == "SHORT":
        pattern_name = signal.get("pattern_name", "Unknown")
        pattern_type = signal.get("pattern_type", "")

        print(
            f"\n🔥 #{trade_number} | {time_str} | 🔴 SHORT SIGNAL | "
            f"Pattern: {pattern_name} ({pattern_type})\n"
            f"   Nifty: {signal.get('price', 0):,.2f}"
        )

    elif action == "EXIT":
        exit_reason = signal.get("exit_reason", "trailing_stop")
        pnl = signal.get("pnl", 0)
        emoji = "✅" if pnl >= 0 else "❌"

        print(
            f"\n{emoji} #{trade_number} EXIT | Reason: {exit_reason.replace('_', ' ').title()} | "
            f"PnL: ₹{pnl:,.0f} | "
            f"Hold: {signal.get('holding_hours', 0):.1f}h"
        )


def print_trade_execution(trade: Dict[str, Any], trade_number: int = 0) -> None:
    """Print trade execution to console."""
    if not trade or not isinstance(trade, dict):
        return

    action = trade.get("action", "")
    expiry = trade.get("expiry", "N/A")

    if action == "EXIT":
        pnl = trade.get("pnl", 0)
        emoji = "✅" if pnl >= 0 else "❌"

        # ✅ NEW: Get exit reason
        exit_reason = trade.get("exit_reason", "trailing_stop")

        lots = trade.get("lots", 1)
        pnl_pct = (pnl / (lots * CAPITAL_PER_LOT)) * 100 if lots > 0 else 0

        entry_opt = trade.get("option_entry_price") or 0.0
        exit_opt = trade.get("option_exit_price") or 0.0
        entry_nifty = trade.get("entry_price") or 0.0
        exit_nifty = trade.get("nifty_price") or 0.0

        print(
            f"{emoji} #{trade_number} EXIT | Reason: {exit_reason.replace('_', ' ').title()} | "
            f"PnL: ₹{pnl:,.0f} ({pnl_pct:+.2f}%) | "
            f"Hold: {trade.get('holding_hours', 0):.1f}h | {lots}L | Exp: {expiry}"
        )
        print(
            f"   Nifty: {entry_nifty:,.2f}→{exit_nifty:,.2f} | "
            f"Opt: ₹{entry_opt:.2f}→₹{exit_opt:.2f}"
        )

    elif action in ("LONG", "SHORT"):
        emoji = "🟢" if action == "LONG" else "🔴"

        # ✅ NEW: Get pattern info
        pattern_name = trade.get("pattern_name", "Unknown")
        pattern_type = trade.get("pattern_type", "")

        strike = _extract_strike(trade.get("symbol", ""))

        print(
            f"{emoji} #{trade_number} {action} | Pattern: {pattern_name} ({pattern_type}) | "
            f"{strike} | Opt: ₹{trade.get('option_entry_price', 0):.2f} | "
            f"{trade.get('lots', 0)}L | Exp: {expiry}"
        )


def print_position_info(
    symbol: str, current_price: float, entry_price: float, lots: int, lot_size: int = 75
) -> None:
    """Print current position information."""
    if not symbol:
        return

    pnl_estimate = (current_price - entry_price) * lot_size * lots

    print(
        f"   💼 Position: {symbol} | "
        f"LTP: ₹{current_price:.2f} | "
        f"Entry: ₹{entry_price:.2f} | "
        f"Unrealized: ₹{pnl_estimate:,.0f}"
    )


def print_startup(
    startup_time: datetime, trading_mode: str, config: Dict[str, Any]
) -> None:
    """Print startup information."""
    print(f"\n{'=' * 60}")
    print(
        f"📊 Nifty Renko Trader Started at {startup_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(f"   Mode: {trading_mode}")
    print(
        f"   Trading Hours: {config.get('trading_start', '09:15')} - {config.get('trading_end', '15:30')}"
    )
    print(
        f"    Brick Size: {config.get('brick_size', '0.04%')} | Reversal Bricks: {config.get('reversal_bricks', 2)}"
    )

    print(f"{'=' * 60}")


def print_waiting(message: str, current_time: datetime) -> None:
    """Print waiting message."""
    print(f"⏳ {message} ({current_time.strftime('%H:%M:%S')})")


def print_error(message: str) -> None:
    """Print error message."""
    print(f"❌ ERROR: {message}")
    logging.error(message)


def print_warning(message: str) -> None:
    """Print warning message."""
    print(f"⚠️ WARNING: {message}")
    logging.warning(message)


def print_info(message: str) -> None:
    """Print info message."""
    print(f"ℹ️ {message}")
    logging.info(message)


def _format_pnl_sign(pnl: float) -> str:
    """
    Format PnL with + or - sign.

    Args:
        pnl: PnL amount

    Returns:
        Formatted string with sign (e.g., "+1,234" or "-567")
    """
    if pnl > 0:
        return f"+{pnl:,.0f}"
    elif pnl < 0:
        return f"{pnl:,.0f}"  # Already has minus sign
    else:
        return "0"
