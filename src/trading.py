# src/trading.py
"""
Order Management Module - Simplified & Robust

Handles order placement, monitoring, and position tracking with proper
fill detection and circuit breaker protection.

Author: Chandrakant Jagtap
Version: 5.1.0 (Fixed Basket Orders + Sequential Fallback)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class OrderStatus(Enum):
    """Order status enumeration."""

    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderSide(Enum):
    """Order side enumeration."""

    BUY = 1
    SELL = -1


@dataclass
class Order:
    """Order representation with status tracking."""

    symbol: str
    side: OrderSide
    quantity: int
    price: Optional[float] = None
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    filled_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def is_complete(self) -> bool:
        """Check if order is fully filled."""
        return (
            self.status == OrderStatus.FILLED and self.filled_quantity >= self.quantity
        )

    @property
    def is_partial(self) -> bool:
        """Check if order is partially filled."""
        return 0 < self.filled_quantity < self.quantity

    @property
    def unfilled_quantity(self) -> int:
        """Get remaining quantity to fill."""
        return max(0, self.quantity - self.filled_quantity)

    @property
    def age_seconds(self) -> float:
        """Get order age in seconds."""
        return (datetime.now() - self.created_at).total_seconds()


class OrderManager:
    """
    Order management with basket orders and sequential fallback.

    Features:
    - Market and limit orders
    - Basket orders for synthetic futures
    - Sequential fallback when basket fails
    - Circuit breaker protection
    - Position reconciliation
    """

    # Configuration
    MAX_ORDER_AGE_SECONDS = 300
    MONITOR_INTERVAL_SECONDS = 30
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 5

    # Circuit breaker
    MAX_CONSECUTIVE_FAILURES = 5
    CIRCUIT_BREAKER_COOLDOWN = 300

    # Fyers status mappings
    FILLED_STATUSES = {"COMPLETE", "FILLED", "TRADED", "2"}
    PARTIAL_STATUSES = {"PARTIAL", "PARTIALLY_FILLED", "1"}
    FAILED_STATUSES = {"CANCELLED", "REJECTED", "EXPIRED", "CANCELED", "5", "6"}

    def __init__(self, fyers_client, lot_size: int = 65) -> None:
        """
        Initialize order manager.

        Args:
            fyers_client: Authenticated Fyers API client
            lot_size: Lot size for position calculations
        """
        if fyers_client is None:
            raise ValueError("fyers_client cannot be None")

        self.fyers = fyers_client
        self.lot_size = lot_size
        self.active_orders: Dict[str, Order] = {}
        self.positions: Dict[str, int] = {}
        self._last_monitor_time: float = 0

        # Circuit breaker state
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0

        logging.info("OrderManager initialized with lot_size=%d", lot_size)

    # =========================================================================
    # CIRCUIT BREAKER
    # =========================================================================

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open."""
        if time.time() < self._circuit_open_until:
            remaining = int(self._circuit_open_until - time.time())
            logging.warning("Circuit breaker OPEN (%ds remaining)", remaining)
            return True

        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            logging.info(
                "✅ Circuit breaker RESET after cooldown (%d failures cleared)",
                self._consecutive_failures,
            )
            self._consecutive_failures = 0

        return False

    def _record_success(self) -> None:
        """Record successful operation, reset circuit breaker."""
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """Record failed operation, potentially open circuit breaker."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            self._circuit_open_until = time.time() + self.CIRCUIT_BREAKER_COOLDOWN
            logging.error(
                "🚨 Circuit breaker OPENED after %d failures (cooldown: %ds)",
                self._consecutive_failures,
                self.CIRCUIT_BREAKER_COOLDOWN,
            )

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    @staticmethod
    def _round_to_tick(price: float, tick_size: float = 0.05) -> float:
        """Round price to nearest tick size."""
        if price <= 0:
            return 0.0
        return round(price / tick_size) * tick_size

    @staticmethod
    def _get_side_value(side: OrderSide) -> int:
        """Get numeric side value."""
        if isinstance(side, OrderSide):
            return side.value
        return int(side)

    def _create_rejected_order(
        self, symbol: str, side: OrderSide, quantity: int, price: Optional[float] = None
    ) -> Order:
        """Create a rejected order object."""
        return Order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status=OrderStatus.REJECTED,
        )

    # =========================================================================
    # MARKET ORDERS
    # =========================================================================

    def place_market_order(self, symbol: str, side: OrderSide, quantity: int) -> Order:
        """
        Place MARKET order for immediate execution.

        Args:
            symbol: Trading symbol
            side: BUY or SELL
            quantity: Order quantity

        Returns:
            Order object with status
        """
        if self._is_circuit_open():
            return self._create_rejected_order(symbol, side, quantity)

        order = Order(symbol=symbol, side=side, quantity=quantity)

        order_params = {
            "symbol": symbol,
            "qty": quantity,
            "type": 2,  # MARKET
            "side": self._get_side_value(side),
            "productType": "MARGIN",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }

        for attempt in range(self.MAX_RETRIES):
            try:
                logging.info(
                    "📤 MARKET order (attempt %d): %s %s qty=%d",
                    attempt + 1,
                    side.name,
                    symbol,
                    quantity,
                )

                response = self.fyers.place_order(order_params)

                if not response:
                    logging.warning("Empty response (attempt %d)", attempt + 1)
                    continue

                code = response.get("code")
                status = response.get("s", "")
                order_id = response.get("id")
                message = response.get("message", "")

                logging.debug(
                    "Response: code=%s, status=%s, id=%s, msg=%s",
                    code,
                    status,
                    order_id,
                    message,
                )

                if code in (200, 201, 1101) and status == "ok" and order_id:
                    order.order_id = str(order_id)
                    order.status = OrderStatus.PENDING
                    self.active_orders[order.order_id] = order
                    self._record_success()

                    logging.info("✅ MARKET order placed: ID=%s", order_id)

                    # Check fill status
                    self._wait_and_update_status(order, checks=3, interval=1)

                    if order.is_complete or order.is_partial:
                        self._update_position(order)

                    return order

                logging.warning(
                    "Order failed (attempt %d): code=%s, msg=%s",
                    attempt + 1,
                    code,
                    message,
                )

            except Exception as exc:
                logging.error("Order exception (attempt %d): %s", attempt + 1, exc)

            if attempt < self.MAX_RETRIES - 1:
                time.sleep(self.RETRY_DELAY_SECONDS)

        order.status = OrderStatus.REJECTED
        self._record_failure()
        logging.error("❌ MARKET order REJECTED after %d attempts", self.MAX_RETRIES)
        return order

    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: float,
        slippage_pct: float = 0.02,
    ) -> Order:
        """
        Place LIMIT order with slippage buffer.

        Args:
            symbol: Trading symbol
            side: BUY or SELL
            quantity: Order quantity
            price: Reference price (slippage applied)
            slippage_pct: Slippage percentage (default 2%)

        Returns:
            Order object with status
        """
        if self._is_circuit_open():
            return self._create_rejected_order(symbol, side, quantity, price)

        # Calculate limit price with slippage
        if side == OrderSide.BUY:
            limit_price = price * (1 + slippage_pct)
        else:
            limit_price = price * (1 - slippage_pct)

        limit_price = self._round_to_tick(limit_price)
        order = Order(symbol=symbol, side=side, quantity=quantity, price=limit_price)

        order_params = {
            "symbol": symbol,
            "qty": quantity,
            "type": 1,  # LIMIT
            "side": self._get_side_value(side),
            "productType": "MARGIN",
            "limitPrice": limit_price,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }

        for attempt in range(self.MAX_RETRIES):
            try:
                logging.info(
                    "📤 LIMIT order (attempt %d): %s %s qty=%d @ ₹%.2f",
                    attempt + 1,
                    side.name,
                    symbol,
                    quantity,
                    limit_price,
                )

                response = self.fyers.place_order(order_params)

                if not response:
                    logging.warning("Empty response (attempt %d)", attempt + 1)
                    continue

                code = response.get("code")
                status = response.get("s", "")
                order_id = response.get("id")
                message = response.get("message", "")

                if code in (200, 201, 1101) and status == "ok" and order_id:
                    order.order_id = str(order_id)
                    order.status = OrderStatus.PENDING
                    self.active_orders[order.order_id] = order
                    self._record_success()

                    logging.info(
                        "✅ LIMIT order placed: ID=%s @ ₹%.2f", order_id, limit_price
                    )

                    self._wait_and_update_status(order, checks=3, interval=1)

                    if order.is_complete or order.is_partial:
                        self._update_position(order)

                    return order

                logging.warning(
                    "Order failed (attempt %d): code=%s, msg=%s",
                    attempt + 1,
                    code,
                    message,
                )

            except Exception as exc:
                logging.error("Order exception (attempt %d): %s", attempt + 1, exc)

            if attempt < self.MAX_RETRIES - 1:
                time.sleep(self.RETRY_DELAY_SECONDS)

        order.status = OrderStatus.REJECTED
        self._record_failure()
        logging.error("❌ LIMIT order REJECTED after %d attempts", self.MAX_RETRIES)
        return order

    # =========================================================================
    # BASKET ORDERS
    # =========================================================================

    def place_basket_orders(self, orders: List[Dict[str, Any]]) -> List[Order]:
        """
        Place multiple orders using Fyers multi-order API.

        Args:
            orders: List of order dicts with symbol, side, quantity, type

        Returns:
            List of Order objects
        """
        if self._is_circuit_open():
            return self._create_rejected_orders(orders)

        if not orders:
            logging.warning("Empty orders list")
            return []

        # Build order params for Fyers
        basket_params = []
        for o in orders:
            order_type = o.get("type", 2)  # Default MARKET
            side_value = self._get_side_value(o["side"])

            param = {
                "symbol": o["symbol"],
                "qty": o["quantity"],
                "type": order_type,
                "side": side_value,
                "productType": "MARGIN",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
            }
            basket_params.append(param)

        logging.info("📤 Placing basket with %d orders", len(orders))
        logging.debug("Basket params: %s", basket_params)

        try:
            # Pass list directly to Fyers API
            response = self.fyers.place_basket_orders(basket_params)

            logging.info("📥 Basket response: %s", response)

            if not response:
                logging.error("❌ Empty basket response")
                self._record_failure()
                return self._create_rejected_orders(orders)

            return self._process_basket_response(response, orders)

        except Exception as exc:
            logging.error("❌ Basket exception: %s", exc, exc_info=True)
            self._record_failure()
            return self._create_rejected_orders(orders)

    def _process_basket_response(
        self, response: Dict[str, Any], orders: List[Dict[str, Any]]
    ) -> List[Order]:
        """
        Process basket order response from Fyers with correct parsing.

        Args:
            response: Fyers basket API response
            orders: Original order requests

        Returns:
            List of Order objects with correct status
        """
        code = response.get("code")
        status = response.get("s", "")
        message = response.get("message", "")

        if code not in (200, 201, 1101) or status != "ok":
            logging.error(
                "❌ Basket failed: code=%s, status=%s, msg=%s", code, status, message
            )
            self._record_failure()
            return self._create_rejected_orders(orders)

        order_data_list = self._extract_order_data(response)

        if not order_data_list:
            logging.error("❌ No order data in response")
            self._record_failure()
            return self._create_rejected_orders(orders)

        result_orders = []

        for i, o in enumerate(orders):
            order_obj = Order(
                symbol=o["symbol"],
                side=o["side"],
                quantity=o["quantity"],
                price=o.get("price"),
            )

            if i < len(order_data_list):
                order_data = order_data_list[i]

                # Check if order succeeded using new method
                if self._is_order_successful(order_data):
                    order_id = self._extract_order_id(order_data)

                    if order_id:
                        order_obj.order_id = str(order_id)
                        order_obj.status = OrderStatus.PENDING
                        self.active_orders[order_obj.order_id] = order_obj

                        logging.info(
                            "✅ Basket leg %d: %s %s | ID=%s",
                            i + 1,
                            o["side"].name if hasattr(o["side"], "name") else o["side"],
                            o["symbol"],
                            order_id,
                        )
                    else:
                        order_obj.status = OrderStatus.REJECTED
                        logging.error(
                            "❌ Basket leg %d: no order ID in response", i + 1
                        )
                else:
                    order_obj.status = OrderStatus.REJECTED

                    # Extract error message from body
                    body = order_data.get("body", {})
                    error_msg = body.get("message", "Unknown error")
                    error_code = body.get("code", "N/A")

                    logging.error(
                        "❌ Basket leg %d failed: code=%s, msg=%s",
                        i + 1,
                        error_code,
                        error_msg,
                    )
            else:
                order_obj.status = OrderStatus.REJECTED
                logging.error("❌ Basket leg %d: no response data", i + 1)

            result_orders.append(order_obj)

        self._record_success()

        # Wait briefly and update status for all orders
        time.sleep(1)
        for order_obj in result_orders:
            if order_obj.order_id:
                self._update_order_status(order_obj)
                if order_obj.is_complete or order_obj.is_partial:
                    self._update_position(order_obj)

        return result_orders

    def _extract_order_data(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract order data list from Fyers basket response.

        Fyers basket format:
        {
            "code": 200,
            "s": "ok",
            "data": [
                {
                    "statusCode": 200,
                    "body": {"code": 1101, "id": "123", "s": "ok"},
                    "statusDescription": "OK"
                }
            ]
        }

        Args:
            response: Fyers API response

        Returns:
            List of order data dictionaries
        """
        if "data" in response and isinstance(response["data"], list):
            return response["data"]
        if "orderBook" in response:
            return response["orderBook"]
        if "orders" in response:
            return response["orders"]
        if "id" in response:
            return [response]
        return []

    @staticmethod
    def _extract_order_id(order_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract order ID from Fyers basket order data.

        Handles nested structure where ID is in body.id, not top-level.

        Args:
            order_data: Order data from basket response

        Returns:
            Order ID string or None
        """
        # Check if this is a basket response with nested body
        if "body" in order_data and isinstance(order_data["body"], dict):
            body = order_data["body"]

            # Check body.id first (Fyers basket format)
            if "id" in body and body["id"]:
                return str(body["id"])

        # Fallback: check top-level keys (single order format)
        for key in ["id", "orderId", "order_id", "orderID"]:
            if key in order_data and order_data[key]:
                return str(order_data[key])

        return None

    @staticmethod
    def _is_order_successful(order_data: Dict[str, Any]) -> bool:
        """
        Check if basket order was successful.

        Success criteria:
        - statusCode == 200
        - body.code in (200, 201, 1101)
        - body.s == "ok"

        Args:
            order_data: Order data from basket response

        Returns:
            True if order succeeded, False otherwise
        """
        # Check top-level statusCode
        status_code = order_data.get("statusCode", 0)
        if status_code != 200:
            return False

        # Check body
        body = order_data.get("body", {})
        if not isinstance(body, dict):
            return False

        # Check body.code (Fyers success codes)
        body_code = body.get("code")
        if body_code not in (200, 201, 1101):
            return False

        # Check body.s (status string)
        body_status = body.get("s", "")
        if body_status != "ok":
            return False

        return True

    def _create_rejected_orders(self, orders: List[Dict[str, Any]]) -> List[Order]:
        """Create rejected Order objects."""
        return [
            Order(
                symbol=o["symbol"],
                side=o["side"],
                quantity=o["quantity"],
                price=o.get("price"),
                status=OrderStatus.REJECTED,
            )
            for o in orders
        ]

    # =========================================================================
    # ORDER MONITORING
    # =========================================================================

    def wait_for_fill(
        self,
        order: Order,
        timeout_seconds: float = 60,
        check_interval: float = 2,
    ) -> Order:
        """
        Wait for order to fill with timeout.

        Args:
            order: Order to monitor
            timeout_seconds: Maximum wait time
            check_interval: Time between status checks

        Returns:
            Updated order
        """
        if not order.order_id or order.status != OrderStatus.PENDING:
            return order

        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            self._update_order_status(order)

            if order.status in (
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
            ):
                break

            if order.status == OrderStatus.PARTIAL:
                logging.info(
                    "Partial fill: %d/%d (waiting...)",
                    order.filled_quantity,
                    order.quantity,
                )

            time.sleep(check_interval)

        if order.status == OrderStatus.PENDING:
            logging.warning(
                "Order timeout after %.0fs: %s", timeout_seconds, order.order_id
            )

        return order

    def wait_for_basket_fill(
        self, orders: List[Order], timeout_seconds: float = 60
    ) -> List[Order]:
        """
        Wait for all basket orders to fill.

        Args:
            orders: List of orders to monitor
            timeout_seconds: Maximum wait time

        Returns:
            Updated orders list
        """
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            all_done = True

            for order in orders:
                if order.order_id and order.status == OrderStatus.PENDING:
                    self._update_order_status(order)
                    if order.status == OrderStatus.PENDING:
                        all_done = False

            if all_done:
                break

            time.sleep(2)

        for i, order in enumerate(orders):
            if order.is_complete:
                logging.info(
                    "✅ Leg %d FILLED: %s @ ₹%.2f",
                    i + 1,
                    order.symbol,
                    order.filled_price,
                )
            elif order.is_partial:
                logging.warning(
                    "⚠️ Leg %d PARTIAL: %d/%d @ ₹%.2f",
                    i + 1,
                    order.filled_quantity,
                    order.quantity,
                    order.filled_price,
                )
            else:
                logging.error(
                    "❌ Leg %d %s: %s", i + 1, order.status.value, order.symbol
                )

        return orders

    def _wait_and_update_status(
        self, order: Order, checks: int = 3, interval: float = 1
    ) -> None:
        """Quick status check after order placement."""
        for _ in range(checks):
            time.sleep(interval)
            self._update_order_status(order)
            if order.is_complete or order.is_partial:
                break

    def _update_order_status(self, order: Order) -> None:
        """Fetch and update single order status from broker."""
        if not order.order_id:
            return

        try:
            response = self.fyers.orderbook()

            if not response or response.get("code") != 200:
                logging.debug("Orderbook fetch failed: %s", response)
                return

            for order_data in response.get("orderBook", []):
                if str(order_data.get("id")) == order.order_id:
                    self._update_order_from_data(order, order_data)
                    break

        except Exception as exc:
            logging.debug("Order status check failed: %s", exc)

    def _update_order_from_data(self, order: Order, data: Dict[str, Any]) -> None:
        """Update order from broker data."""
        status_raw = str(data.get("status", "")).upper()

        order.filled_quantity = data.get("filledQty", data.get("tradedQty", 0))
        order.filled_price = data.get(
            "tradedPrice", data.get("avgPrice", order.price or 0)
        )

        if status_raw in self.FILLED_STATUSES:
            order.status = OrderStatus.FILLED
        elif status_raw in self.PARTIAL_STATUSES:
            if order.filled_quantity >= order.quantity:
                order.status = OrderStatus.FILLED
            elif order.filled_quantity > 0:
                order.status = OrderStatus.PARTIAL
        elif status_raw in self.FAILED_STATUSES:
            if "CANCEL" in status_raw or status_raw == "5":
                order.status = OrderStatus.CANCELLED
            elif "REJECT" in status_raw or status_raw == "6":
                order.status = OrderStatus.REJECTED
            else:
                order.status = OrderStatus.EXPIRED

        logging.debug(
            "Order %s: status=%s, filled=%d/%d @ ₹%.2f",
            order.order_id,
            order.status.value,
            order.filled_quantity,
            order.quantity,
            order.filled_price,
        )

    def _update_position(self, order: Order) -> None:
        """Update internal position tracking after fill."""
        if order.filled_quantity <= 0:
            return

        current_qty = self.positions.get(order.symbol, 0)
        qty_change = (
            order.filled_quantity
            if order.side == OrderSide.BUY
            else -order.filled_quantity
        )
        new_qty = current_qty + qty_change

        if new_qty == 0:
            self.positions.pop(order.symbol, None)
        else:
            self.positions[order.symbol] = new_qty

        logging.info("Position: %s %d → %d", order.symbol, current_qty, new_qty)

    # =========================================================================
    # ORDER CANCELLATION
    # =========================================================================

    def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order."""
        if not order_id:
            return False

        try:
            response = self.fyers.cancel_order({"id": order_id})

            if not response:
                logging.warning("Empty cancel response for %s", order_id)
                return False

            code = response.get("code")
            status = response.get("s", "")

            if code in (200, 1103) and status == "ok":
                if order_id in self.active_orders:
                    self.active_orders[order_id].status = OrderStatus.CANCELLED
                logging.info("✅ Order cancelled: %s", order_id)
                return True

            logging.warning("Cancel failed for %s: %s", order_id, response)
            return False

        except Exception as exc:
            logging.error("Cancel exception for %s: %s", order_id, exc)
            return False

    def cancel_all_pending(self) -> int:
        """Cancel all pending orders. Returns count cancelled."""
        cancelled = 0
        for order_id, order in list(self.active_orders.items()):
            if order.status == OrderStatus.PENDING:
                if self.cancel_order(order_id):
                    cancelled += 1
        return cancelled

    # =========================================================================
    # POSITION RECONCILIATION
    # =========================================================================

    def reconcile_with_broker(self) -> Dict[str, Tuple[int, int]]:
        """
        Reconcile internal positions with broker.

        Returns:
            Dict of symbol -> (internal_qty, broker_qty) for mismatches
        """
        mismatches: Dict[str, Tuple[int, int]] = {}

        try:
            response = self.fyers.positions()

            if not response or response.get("code") != 200:
                logging.warning("Failed to fetch broker positions")
                return mismatches

            broker_positions: Dict[str, int] = {}
            for pos in response.get("netPositions", []):
                symbol = pos.get("symbol", "")
                net_qty = pos.get("netQty", 0)
                if net_qty != 0:
                    broker_positions[symbol] = net_qty

            all_symbols = set(self.positions.keys()) | set(broker_positions.keys())

            for symbol in all_symbols:
                internal = self.positions.get(symbol, 0)
                broker = broker_positions.get(symbol, 0)

                if internal != broker:
                    mismatches[symbol] = (internal, broker)
                    logging.warning(
                        "Position mismatch: %s internal=%d, broker=%d",
                        symbol,
                        internal,
                        broker,
                    )
                    if broker == 0:
                        self.positions.pop(symbol, None)
                    else:
                        self.positions[symbol] = broker

            if mismatches:
                logging.warning("Found %d position mismatches", len(mismatches))
            else:
                logging.debug("Positions reconciled - no mismatches")

            return mismatches

        except Exception as exc:
            logging.error("Reconciliation error: %s", exc)
            return mismatches

    def get_position(self, symbol: str) -> int:
        """Get current position for symbol."""
        return self.positions.get(symbol, 0)

    def has_pending_orders(self) -> bool:
        """Check if any orders are pending."""
        return any(o.status == OrderStatus.PENDING for o in self.active_orders.values())

    def cleanup(self) -> None:
        """Cleanup - cancel all pending orders."""
        cancelled = self.cancel_all_pending()
        if cancelled:
            logging.info("Cleanup: cancelled %d pending orders", cancelled)
        self.active_orders.clear()


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "OrderManager",
    "Order",
    "OrderStatus",
    "OrderSide",
]
