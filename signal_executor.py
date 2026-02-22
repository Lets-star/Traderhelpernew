"""
Signal executor with thread-safe logging, validation, and st.secrets support.

FEATURES:
- Thread-safe CSV logging with threading.Lock
- Validation for processed signals
- st.secrets support for API keys with fallback to environment variables
- Threaded execution with ThreadPoolExecutor (replaces asyncio.run() approach)
- Comprehensive error handling (no bare excepts)
- Enhanced logging and monitoring
- Context manager support for ByBitClient
- UpdateBus integration for real-time execution updates
- Circuit breaker with proper state machine
- WebSocket for real-time order fills
- Idempotency key support
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

import requests
import websocket

from bybit_client import ByBitClient
from update_bus import UpdateBus
from logging_config import get_structured_logger
from config import AppSettings
from trader_types import SignalPayload, ExecutionResult, is_valid_signal
from trader_types.enums import ExecutionStatus, SignalDirection


# =============================================================================
# Circuit Breaker State Machine
# =============================================================================

class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation, requests allowed
    OPEN = "open"          # Failure threshold reached, requests blocked
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker with proper state machine.
    
    States:
    - CLOSED: Normal operation, requests go through
    - OPEN: Too many failures, requests are rejected
    - HALF_OPEN: Testing recovery, limited requests allowed
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_requests: int = 1,
        success_threshold: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_requests = half_open_max_requests
        self.success_threshold = success_threshold
        
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._half_open_requests = 0
        self._lock = threading.RLock()
    
    @property
    def state(self) -> CircuitBreakerState:
        return self._state
    
    @property
    def failure_count(self) -> int:
        return self._failure_count
    
    def can_execute(self) -> bool:
        """Check if request can be executed."""
        with self._lock:
            current_time = time.time()
            
            if self._state == CircuitBreakerState.CLOSED:
                return True
            
            if self._state == CircuitBreakerState.OPEN:
                # Check if recovery timeout has passed
                if current_time - self._last_failure_time >= self.recovery_timeout:
                    logger.info("Circuit breaker transitioning to half_open")
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._half_open_requests = 0
                    return True
                return False
            
            # HALF_OPEN state - allow limited requests
            if self._half_open_requests < self.half_open_max_requests:
                self._half_open_requests += 1
                return True
            return False
    
    def record_success(self) -> None:
        """Record successful execution."""
        with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    logger.info("Circuit breaker closing after successful recovery")
                    self._state = CircuitBreakerState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitBreakerState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0
    
    def record_failure(self) -> None:
        """Record failed execution."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                # Any failure in half_open goes back to open
                logger.warning("Circuit breaker reopening after failure in half_open state")
                self._state = CircuitBreakerState.OPEN
                self._success_count = 0
            
            elif self._state == CircuitBreakerState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    logger.warning(
                        f"Circuit breaker opened after {self._failure_count} failures"
                    )
                    self._state = CircuitBreakerState.OPEN
    
    def get_state_info(self) -> Dict[str, Any]:
        """Get circuit breaker state info."""
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "last_failure_time": self._last_failure_time,
                "half_open_requests": self._half_open_requests,
            }


# =============================================================================
# ByBit WebSocket Client for Real-time Order Fills
# =============================================================================

class ByBitOrderWebSocketClient:
    """
    WebSocket client for real-time ByBit order updates.
    
    Handles order fill notifications via WebSocket to avoid polling.
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        on_order_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.on_order_update = on_order_update
        
        self.ws = None
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._reconnect_count = 0
        self.max_reconnects = 5
        self._lock = threading.Lock()
        
        # Track pending orders: order_link_id -> callback
        self._pending_orders: Dict[str, Callable[[Dict], None]] = {}
        
        self.base_url = "wss://stream-testnet.bybit.com/v5/private" if testnet else "wss://stream.bybit.com/v5/private"
    
    def start(self) -> bool:
        """Start WebSocket connection."""
        if self._connected.is_set():
            return True
        
        try:
            # Generate auth params for WebSocket
            timestamp = str(int(time.time() * 1000))
            signature = self._generate_wss_signature(timestamp)
            
            ws_url = f"{self.base_url}?api_key={self.api_key}&timestamp={timestamp}&signature={signature}&perp_private"
            
            self.ws = websocket.WebSocketApp(
                ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            
            # Wait for connection
            if self._connected.wait(timeout=10):
                self._reconnect_count = 0
                logger.info("ByBit WebSocket connected successfully")
                return True
            else:
                logger.error("ByBit WebSocket connection timeout")
                return False
                
        except Exception as e:
            logger.error(f"Failed to start ByBit WebSocket: {e}")
            return False
    
    def _generate_wss_signature(self, timestamp: str) -> str:
        """Generate signature for WebSocket authentication."""
        param_str = timestamp + self.api_key
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
    
    def _run(self) -> None:
        """Run WebSocket in background thread."""
        while not self.stop_event.is_set():
            try:
                if self.ws:
                    self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket run error: {e}")
            
            if not self.stop_event.is_set():
                self._reconnect_count += 1
                if self._reconnect_count >= self.max_reconnects:
                    logger.error("Max reconnects reached, stopping WebSocket")
                    break
                
                # Exponential backoff
                wait_time = min(2 ** self._reconnect_count, 30)
                logger.info(f"Reconnecting in {wait_time} seconds...")
                self._connected.clear()
                time.sleep(wait_time)
    
    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        """WebSocket connection opened."""
        logger.info("ByBit WebSocket connection opened")
        self._connected.set()
        
        # Subscribe to order updates
        ws.send(json.dumps({
            "op": "subscribe",
            "args": ["order"],
        }))
    
    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        """Handle WebSocket message."""
        try:
            data = json.loads(message)
            
            # Handle subscription response
            if "op" in data:
                if data.get("op") == "subscribe":
                    logger.info(f"Subscribed to: {data.get('args', [])}")
                elif data.get("op") == "pong":
                    pass  # Heartbeat response
            
            # Handle order updates
            if "data" in data:
                order_data = data["data"]
                self._handle_order_update(order_data)
                
        except Exception as e:
            logger.error(f"Error parsing WebSocket message: {e}")
    
    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        """WebSocket error."""
        logger.error(f"ByBit WebSocket error: {error}")
        self._connected.clear()
    
    def _on_close(self, ws: websocket.WebSocketApp, close_status_code: int, close_msg: str) -> None:
        """WebSocket closed."""
        logger.info(f"ByBit WebSocket closed: {close_status_code} - {close_msg}")
        self._connected.clear()
    
    def _handle_order_update(self, order_data: Dict[str, Any]) -> None:
        """Handle order update from WebSocket."""
        order_link_id = order_data.get("orderLinkId")
        order_status = order_data.get("orderStatus")
        
        # Notify callback if registered
        if self.on_order_update:
            try:
                self.on_order_update(order_data)
            except Exception as e:
                logger.error(f"Order update callback error: {e}")
        
        # Check pending orders
        if order_link_id and order_link_id in self._pending_orders:
            callback = self._pending_orders[order_link_id]
            
            if order_status in ["Filled", "PartiallyFilled", "Cancelled", "Rejected"]:
                # Order finished, remove from pending
                del self._pending_orders[order_link_id]
                callback(order_data)
    
    def register_order(self, order_link_id: str, callback: Callable[[Dict], None]) -> None:
        """Register order for fill tracking."""
        with self._lock:
            self._pending_orders[order_link_id] = callback
    
    def unregister_order(self, order_link_id: str) -> None:
        """Unregister order from fill tracking."""
        with self._lock:
            self._pending_orders.pop(order_link_id, None)
    
    def stop(self) -> None:
        """Stop WebSocket connection."""
        self.stop_event.set()
        if self.ws:
            self.ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("ByBit WebSocket stopped")
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected.is_set()


# Import for signature generation
import hmac
import hashlib

# Optional metrics import
try:
    from metrics import signal_executions, signal_execution_latency, signal_validation_errors, active_signals
    from metrics.collectors import get_signal_collector
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False

logger = get_structured_logger(__name__)


class SignalExecutor:
    """
    Thread-safe signal executor for ByBit with enhanced validation and logging.

    This class provides:
    - Thread-safe signal execution using ThreadPoolExecutor
    - Comprehensive signal validation
    - CSV logging with file locking
    - st.secrets integration with environment variable fallbacks
    - UpdateBus integration for real-time execution updates
    - Dry run mode for testing
    - Circuit breaker for failure protection
    - Leverage caching to reduce API calls
    """

    LOG_FILE = "trade_execution_log.csv"
    MAX_WORKER_THREADS = 3
    
    # Circuit breaker settings
    CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT = 60  # seconds
    CIRCUIT_BREAKER_HALF_OPEN_REQUESTS = 1

    def __init__(self, update_bus: Optional[UpdateBus] = None) -> None:
        """
        Initialize signal executor.

        Args:
            update_bus: Optional UpdateBus for publishing execution updates
        """
        self.client: Optional[ByBitClient] = None
        self.update_bus = update_bus
        self.enabled = False
        self.api_key = ""
        self.api_secret = ""
        self.testnet = True
        self.default_leverage = 5
        self.pos_size_multiplier = 1.0
        self.dry_run = False

        # Thread safety
        self._lock = threading.RLock()
        self._csv_lock = threading.Lock()

        # Thread pool for concurrent execution
        self._executor = ThreadPoolExecutor(max_workers=self.MAX_WORKER_THREADS)

        # Statistics tracking
        self._total_executions = 0
        self._successful_executions = 0
        self._failed_executions = 0
        self._validation_errors = 0
        
        # Circuit breaker with state machine
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=self.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout=self.CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
            half_open_max_requests=self.CIRCUIT_BREAKER_HALF_OPEN_REQUESTS,
        )
        
        # WebSocket client for real-time order fills
        self._ws_client: Optional[ByBitOrderWebSocketClient] = None
        self._ws_enabled = False
        
        # Leverage cache: symbol -> (leverage, timestamp)
        self._leverage_cache: Dict[str, Tuple[str, float]] = {}
        self._leverage_cache_ttl = 300  # 5 minutes

        # Idempotency key tracking: order_id -> (status, timestamp)
        self._idempotency_keys: Dict[str, Tuple[str, float]] = {}
        self._idempotency_ttl = 3600  # 1 hour
        
        # Ensure log file exists with header
        self._ensure_log_file()

    # API Error codes that should trigger circuit breaker
    API_ERRORS_CIRCUIT_BREAKER = {
        -1,           # General error
        10001,        # API key error
        10002,        # Request timestamp expired
        10003,        # Sign error
        10004,        # Invalid request frequency
        10005,        # IP address not whitelisted
        10006,        # No permission
        10007,        # Api key expired
        10008,        # Start time is greater than end time
        10009,        # Number of orders exceeds the limit
        10010,        # Order quantity exceeds the limit
        10016,        # Bybit internal error
        10017,        # Invalid client_id parameter
        10018,        # Invalid parameter
        10019,        # Unknown enum value
        10020,        # Request payload too large
        110001,       # Cannot close position
        110002,       # Position is not exist
        110003,       # Position mode is not allowed
        110004,       # Cannot set leverage
        110005,       # Position is in liquidation ordelivering
        110006,       # Position is in ADL
        110007,       # Cannot modify position
        110008,       # No position found
        110009,       # Position already has an order
        110010,       # Cannot cover
        110011,       # Inconsistent leverage
        110012,       # Cannot reduce-only
        110013,       # Order already cancelled
        110014,       # Order already closed
        110015,       # Cannot set TP/SL
        110016,       # Cannot set trigger price
        110017,       # Order does not exist
        110018,       # Liquidation order rejected
        110019,       # Partial liquidation order rejected
        110020,       # Delivering order rejected
        110021,       # Replaced order rejected
        110022,       # Unknown symbol
        110023,       # Symbol stopped
        110024,       # Symbol closed
        110025,       # Cannot trade
        110026,       # Price is out of acceptable range
        110027,       # Exceeds max leverage
        110028,       # No trading allowed
        110029,       # Violates price limit
        110030,       # Violates tick size
        110031,       # Invalid order price
        110032,       # Invalid trigger price
        110033,       # Order amount too large
        110034,       # Risk limit exceeded
        130001,       # Insufficient wallet balance
        130002,       # Insufficient available balance
        130003,       # Order would trigger immediate loss
        130004,       # Order would trigger liquidation
        130005,       # Trigger price and price has crossed
        130006,       # Duplicate order
        130007,       # Position already has pending order
    }
    
    # API Error codes that are retryable
    API_ERRORS_RETRYABLE = {
        -1,           # General error (transient)
        10004,        # Invalid request frequency (rate limit)
        10016,        # Bybit internal error
        130006,       # Duplicate order (can retry with same idempotency key)
    }
    
    # API Error codes for order status
    ORDER_STATUS_FILLED = "Filled"
    ORDER_STATUS_PARTIALLY_FILLED = "PartiallyFilled"
    ORDER_STATUS_CANCELLED = "Cancelled"
    ORDER_STATUS_PENDING = "Pending"

    def _ensure_log_file(self) -> None:
        """Ensure CSV log file exists with proper headers."""
        with self._csv_lock:
            if not os.path.exists(self.LOG_FILE):
                with open(self.LOG_FILE, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "timestamp", "signal_id", "symbol", "direction", "qty",
                        "entry_price", "take_profit", "stop_loss", "leverage",
                        "status", "response_code", "latency_ms", "error_msg",
                        "validation_errors", "thread_id"
                    ])

    def _get_api_credentials(self) -> tuple[str, str]:
        """
        Securely get API credentials from Pydantic Settings with st.secrets fallback.

        Returns:
            Tuple of (api_key, api_secret)
        """
        # Use Pydantic Settings (handles both st.secrets and environment variables)
        settings = AppSettings.from_secrets()
        
        api_key, api_secret = settings.get_bybit_credentials()
        
        if api_key and api_secret:
            logger.info("Using ByBit credentials from settings")
            return api_key, api_secret

        raise ValueError("API credentials not found in st.secrets or environment variables")

    def _log_trade(self, trade_data: Dict[str, Any]) -> None:
        """Thread-safe CSV logging for trades."""
        with self._csv_lock:
            try:
                with open(self.LOG_FILE, 'a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        datetime.utcnow().isoformat(),
                        trade_data.get("signal_id", ""),
                        trade_data.get("symbol", ""),
                        trade_data.get("direction", ""),
                        trade_data.get("qty", ""),
                        trade_data.get("entry_price", ""),
                        trade_data.get("take_profit", ""),
                        trade_data.get("stop_loss", ""),
                        trade_data.get("leverage", ""),
                        trade_data.get("status", ""),
                        trade_data.get("response_code", ""),
                        trade_data.get("latency_ms", ""),
                        trade_data.get("error_msg", ""),
                        trade_data.get("validation_errors", ""),
                        trade_data.get("thread_id", "")
                    ])
            except IOError as e:
                logger.error(f"Failed to write to trade log: {e}")
            except Exception as e:
                logger.error(f"Unexpected error logging trade: {e}")

    def _is_circuit_breaker_open(self) -> bool:
        """
        Check if circuit breaker is open and should prevent requests.
        
        Returns:
            True if circuit breaker is open and requests should be blocked
        """
        return not self._circuit_breaker.can_execute()
    
    def _record_circuit_breaker_success(self) -> None:
        """Record successful request for circuit breaker."""
        self._circuit_breaker.record_success()
    
    def _record_circuit_breaker_failure(self) -> None:
        """Record failed request for circuit breaker."""
        self._circuit_breaker.record_failure()
    
    def get_circuit_breaker_state(self) -> Dict[str, Any]:
        """Get circuit breaker state info."""
        return self._circuit_breaker.get_state_info()
    
    def _get_cached_leverage(self, symbol: str) -> Optional[str]:
        """
        Get cached leverage for symbol if not expired.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Cached leverage string or None if expired/not cached
        """
        with self._lock:
            if symbol in self._leverage_cache:
                leverage, timestamp = self._leverage_cache[symbol]
                if time.time() - timestamp < self._leverage_cache_ttl:
                    return leverage
                else:
                    del self._leverage_cache[symbol]
            return None
    
    def _set_cached_leverage(self, symbol: str, leverage: str) -> None:
        """
        Cache leverage for symbol.
        
        Args:
            symbol: Trading symbol
            leverage: Leverage value
        """
        with self._lock:
            self._leverage_cache[symbol] = (leverage, time.time())

    def _check_idempotency(self, order_id: str) -> Optional[str]:
        """
        Check if order with this idempotency key was already processed.
        
        Args:
            order_id: The idempotency key (order_link_id)
            
        Returns:
            Previous order status if found, None otherwise
        """
        with self._lock:
            if order_id in self._idempotency_keys:
                status, timestamp = self._idempotency_keys[order_id]
                if time.time() - timestamp < self._idempotency_ttl:
                    logger.info(f"Duplicate order detected: {order_id}, previous status: {status}")
                    return status
                else:
                    del self._idempotency_keys[order_id]
            return None
    
    def _set_idempotency(self, order_id: str, status: str) -> None:
        """
        Track order idempotency key.
        
        Args:
            order_id: The idempotency key
            status: Order status
        """
        with self._lock:
            self._idempotency_keys[order_id] = (status, time.time())
    
    def _poll_order_fill(
        self,
        client: ByBitClient,
        symbol: str,
        order_id: str,
        client_order_id: str,
        timeout: float = 10.0,
        poll_interval: float = 0.5
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Poll for order fill status.
        
        Args:
            client: ByBitClient instance
            symbol: Trading symbol
            order_id: Server order ID
            client_order_id: Client order ID (for idempotency)
            timeout: Maximum time to wait for fill
            poll_interval: Time between polls
            
        Returns:
            Tuple of (final_status, order_info)
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # Try to get order status by client_order_id first
                order_result = client.get_order_status(
                    symbol=symbol,
                    client_order_id=client_order_id
                )
                
                if order_result.get("retCode") != 0:
                    logger.warning(f"Failed to get order status: {order_result.get('retMsg')}")
                    time.sleep(poll_interval)
                    continue
                
                order_list = order_result.get("result", {}).get("list", [])
                if not order_list:
                    time.sleep(poll_interval)
                    continue
                
                order_info = order_list[0]
                order_status = order_info.get("orderStatus", "")
                
                if order_status == self.ORDER_STATUS_FILLED:
                    logger.info(f"Order filled: {client_order_id}")
                    return ("filled", order_info)
                
                elif order_status == self.ORDER_STATUS_CANCELLED:
                    logger.warning(f"Order cancelled: {client_order_id}")
                    return ("cancelled", order_info)
                
                elif order_status == self.ORDER_STATUS_PARTIALLY_FILLED:
                    logger.info(f"Order partially filled: {client_order_id}, qty: {order_info.get('cumExecQty')}")
                    # Continue waiting for full fill or cancel
                
                # Still pending, continue polling
                time.sleep(poll_interval)
                
            except Exception as e:
                logger.warning(f"Error polling order: {e}")
                time.sleep(poll_interval)
        
        # Timeout - try one more time to get final status
        try:
            order_result = client.get_order_status(symbol=symbol, client_order_id=client_order_id)
            if order_result.get("retCode") == 0:
                order_list = order_result.get("result", {}).get("list", [])
                if order_list:
                    order_info = order_list[0]
                    order_status = order_info.get("orderStatus", "")
                    logger.warning(f"Order poll timeout, final status: {order_status}")
                    return (order_status.lower(), order_info)
        except Exception as e:
            logger.error(f"Failed to get final order status: {e}")
        
        return ("pending", None)

    def _handle_api_error(self, error_code: int, error_msg: str) -> Tuple[bool, bool]:
        """
        Handle API error code and determine if should trigger circuit breaker.
        
        Args:
            error_code: ByBit API error code
            error_msg: Error message
            
        Returns:
            Tuple of (should_retry, circuit_breaker_triggered)
        """
        if error_code in self.API_ERRORS_CIRCUIT_BREAKER:
            logger.warning(f"API error {error_code} triggers circuit breaker: {error_msg}")
            return (False, True)
        
        if error_code in self.API_ERRORS_RETRYABLE:
            logger.info(f"API error {error_code} is retryable: {error_msg}")
            return (True, False)
        
        # Unknown error - treat as transient
        logger.warning(f"Unknown API error {error_code}: {error_msg}")
        return (False, False)

    def _validate_signal(self, signal: Dict[str, Any]) -> List[str]:
        """
        Validate processed signal structure and content.

        Args:
            signal: Signal dictionary to validate (SignalPayload structure)

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if not signal or not isinstance(signal, dict):
            errors.append("Signal must be a non-empty dictionary")
            return errors

        # Use TypeGuard for initial validation
        if not is_valid_signal(signal):
            errors.append("Signal does not match SignalPayload structure")

        # Required fields (from SignalPayload)
        required_fields = ["signal_id", "symbol", "direction", "entry_price"]
        for field in required_fields:
            if field not in signal:
                errors.append(f"Missing required field: {field}")

        # Validate signal type
        signal_type = signal.get("signal")
        if signal_type not in [None, "BUY", "SELL", "HOLD"]:
            errors.append(f"Invalid signal type: {signal_type}. Must be BUY, SELL, or HOLD")

        # Validate symbol format
        symbol = signal.get("symbol")
        if symbol:
            if not isinstance(symbol, str):
                errors.append(f"Symbol must be a string: {type(symbol)}")
            elif len(symbol) < 3:
                errors.append(f"Symbol must be at least 3 characters: {symbol}")
            else:
                # Convert to uppercase
                signal["symbol"] = symbol.upper()

        # Validate direction using Enum
        direction = signal.get("direction")
        if direction:
            if not isinstance(direction, str):
                errors.append(f"Direction must be a string: {type(direction)}")
            else:
                try:
                    SignalDirection(direction.upper())
                except ValueError:
                    errors.append(f"Invalid direction: {direction}. Must be LONG or SHORT")

        # Validate numeric fields
        numeric_fields = ["entry_price", "take_profit", "stop_loss", "leverage", "quantity"]
        for field in numeric_fields:
            value = signal.get(field)
            if value is not None:
                try:
                    float_value = float(value)
                    if field in ["leverage"] and (float_value <= 0 or float_value > 125):
                        errors.append(f"{field} must be between 0 and 125: {float_value}")
                    elif field in ["entry_price", "take_profit", "stop_loss"] and float_value <= 0:
                        errors.append(f"{field} must be positive: {float_value}")
                except (ValueError, TypeError):
                    errors.append(f"{field} must be numeric: {value}")

        # Validate entries if present
        entries = signal.get("entries")
        if entries is not None:
            if not isinstance(entries, list):
                errors.append("Entries must be a list")
            elif len(entries) == 0:
                errors.append("Entries list cannot be empty")
            else:
                for i, entry in enumerate(entries):
                    try:
                        float(entry)
                        if float(entry) <= 0:
                            errors.append(f"Entry at index {i} must be positive: {entry}")
                    except (ValueError, TypeError):
                        errors.append(f"Entry at index {i} must be numeric: {entry}")

        # Validate take profits structure
        take_profits = signal.get("take_profits")
        if take_profits is not None:
            if not isinstance(take_profits, dict):
                errors.append("Take profits must be a dictionary")
            else:
                for key, value in take_profits.items():
                    try:
                        tp_value = float(value)
                        if tp_value <= 0:
                            errors.append(f"Take profit value for {key} must be positive: {value}")
                    except (ValueError, TypeError):
                        errors.append(f"Take profit value for {key} must be numeric: {value}")

        # Validate signal_id format
        signal_id = signal.get("signal_id")
        if signal_id and not isinstance(signal_id, str):
            errors.append(f"Signal ID must be a string: {type(signal_id)}")

        return errors

    def configure(
        self,
        enabled: bool,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
        leverage: int = 5,
        pos_size_multiplier: float = 1.0,
        dry_run: bool = False,
    ) -> None:
        """
        Configure executor with optional st.secrets integration.

        Args:
            enabled: Enable signal execution
            api_key: API key (will be ignored if using st.secrets and both are empty)
            api_secret: API secret (will be ignored if using st.secrets and both are empty)
            testnet: Use testnet or mainnet
            leverage: Default leverage
            pos_size_multiplier: Position size multiplier
            dry_run: Dry run mode (no actual trades)
        """
        self.enabled = enabled
        self.testnet = testnet
        self.default_leverage = leverage
        self.pos_size_multiplier = pos_size_multiplier
        self.dry_run = dry_run

        if enabled and not dry_run:
            try:
                # Try to get credentials from st.secrets first if direct credentials not provided
                if not api_key or not api_secret:
                    self.api_key, self.api_secret = self._get_api_credentials()
                else:
                    self.api_key = api_key
                    self.api_secret = api_secret

                logger.info(f"ByBit client configured for {'testnet' if testnet else 'mainnet'}")

            except ValueError as e:
                logger.error(f"Failed to configure API credentials: {e}")
                self.enabled = False  # Disable if credentials invalid
            except Exception as e:
                logger.error(f"Unexpected error configuring executor: {e}", exc_info=True)
                self.enabled = False
    
    def enable_websocket(self, on_order_update: Optional[Callable[[Dict[str, Any]], None]] = None) -> bool:
        """
        Enable WebSocket for real-time order fills.
        
        Args:
            on_order_update: Optional callback for order updates
            
        Returns:
            True if WebSocket enabled successfully
        """
        if not self.api_key or not self.api_secret:
            logger.warning("Cannot enable WebSocket without API credentials")
            return False
        
        try:
            self._ws_client = ByBitOrderWebSocketClient(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet,
                on_order_update=on_order_update,
            )
            
            if self._ws_client.start():
                self._ws_enabled = True
                logger.info("ByBit WebSocket enabled for real-time fills")
                return True
            else:
                logger.error("Failed to start ByBit WebSocket")
                self._ws_enabled = False
                return False
                
        except Exception as e:
            logger.error(f"Error enabling WebSocket: {e}")
            self._ws_enabled = False
            return False
    
    def disable_websocket(self) -> None:
        """Disable WebSocket for real-time order fills."""
        if self._ws_client:
            self._ws_client.stop()
            self._ws_client = None
        self._ws_enabled = False
        logger.info("ByBit WebSocket disabled")
    
    def is_websocket_enabled(self) -> bool:
        """Check if WebSocket is enabled and connected."""
        if not self._ws_enabled or not self._ws_client:
            return False
        return self._ws_client.is_connected()
    
    def register_order_for_tracking(
        self,
        order_link_id: str,
        callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """Register an order for WebSocket fill tracking."""
        if self._ws_client and self._ws_enabled:
            self._ws_client.register_order(order_link_id, callback)
    
    def get_circuit_breaker_info(self) -> Dict[str, Any]:
        """Get circuit breaker state information."""
        info = self.get_circuit_breaker_state()
        info["websocket_enabled"] = self._ws_enabled
        info["websocket_connected"] = self.is_websocket_enabled()
        return info

    def _execute_signal_sync(self, signal: Dict[str, Any]) -> ExecutionResult:
        """
        Execute signal synchronously with thread safety and validation.

        Args:
            signal: Validated signal dictionary (SignalPayload structure)

        Returns:
            Execution result dictionary (ExecutionResult structure)
        """
        thread_id = threading.current_thread().ident

        # Validate signal before execution
        validation_errors = self._validate_signal(signal)
        if validation_errors:
            with self._lock:
                self._total_executions += 1
                self._validation_errors += 1
            
            # Track validation error metrics
            if METRICS_AVAILABLE:
                signal_validation_errors.labels(
                    symbol=signal.get("symbol", "unknown"),
                    error_field=validation_errors[0].split(":")[0] if validation_errors else "unknown"
                ).inc()
                
                collector = get_signal_collector()
                collector.record_validation_error(
                    signal.get("symbol", "unknown"),
                    validation_errors[0].split(":")[0] if validation_errors else "unknown"
                )

            error_msg = f"Signal validation failed: {', '.join(validation_errors)}"
            logger.error(
                "Signal validation failed",
                signal_id=signal.get("signal_id"),
                errors=validation_errors
            )

            # Log validation failure
            self._log_trade({
                "signal_id": signal.get("signal_id", ""),
                "symbol": signal.get("symbol", ""),
                "direction": signal.get("direction", ""),
                "qty": signal.get("quantity", ""),
                "entry_price": signal.get("entry_price", ""),
                "status": "validation_error",
                "response_code": -1,
                "error_msg": error_msg,
                "validation_errors": "; ".join(validation_errors),
                "thread_id": str(thread_id)
            })

            return {
                "status": "validation_error",
                "error": error_msg,
                "validation_errors": validation_errors
            }

        signal_id = signal.get("signal_id", f"sig_{int(time.time()*1000)}")
        symbol = signal.get("symbol", "")
        direction = signal.get("direction", "")
        entry_price = float(signal.get("entry_price", 0))
        tp = float(signal.get("take_profit", 0))
        sl = float(signal.get("stop_loss", 0))

        # Calculate quantity
        qty = float(signal.get("quantity", 0.001)) * self.pos_size_multiplier

        # Get leverage from signal or default
        leverage = signal.get("leverage", self.default_leverage)

        logger.info(
            "Processing signal",
            signal_id=signal_id,
            symbol=symbol,
            direction=direction,
            quantity=qty,
            thread_id=thread_id
        )
        
        # Track active signals
        if METRICS_AVAILABLE:
            active_signals.labels(symbol=symbol).inc()

        start_time = time.time()

        status = "pending"
        response_code = 0
        error_msg = ""

        # Publish initial update
        if self.update_bus:
            try:
                self.update_bus.publish({
                    "type": "EXECUTION_UPDATE",
                    "signal_id": signal_id,
                    "status": "pending",
                    "timestamp": start_time,
                    "thread_id": thread_id
                })
            except Exception as e:
                logger.warning(f"Failed to publish initial execution update: {e}")

        # Check circuit breaker before execution
        if self._is_circuit_breaker_open():
            error_msg = "Circuit breaker is open - too many recent failures"
            logger.warning(error_msg)
            status = "rejected"
            response_code = -1
            return {
                "status": status,
                "error": error_msg,
                "signal_id": signal_id
            }

        if self.dry_run:
            logger.info(f"[DRY RUN] Would execute: {direction} {qty} {symbol} @ {entry_price}")
            status = "filled (dry_run)"
            time.sleep(0.1)  # Simulate network delay
        else:
            try:
                # Use context manager for client
                with ByBitClient(self.api_key, self.api_secret, self.testnet) as client:
                    # Check balance before placing order
                    balance_result = client.get_wallet_balance(account_type="UNIFIED")
                    if balance_result.get("retCode") == 0:
                        try:
                            balance = float(balance_result["result"]["list"][0]["coin"][0]["walletBalance"])
                            required_balance = qty * entry_price * 1.1  # 10% buffer
                            if balance < required_balance:
                                error_msg = f"Insufficient balance: {balance} USDT required {required_balance} USDT"
                                logger.error(error_msg)
                                self._record_circuit_breaker_failure()
                                status = "rejected"
                                response_code = -1
                                return {
                                    "status": status,
                                    "error": error_msg,
                                    "signal_id": signal_id
                                }
                        except (KeyError, IndexError, ValueError) as e:
                            logger.warning(f"Could not parse balance: {e}")
                    else:
                        logger.warning(f"Could not fetch balance: {balance_result.get('retMsg')}")

                    # Set leverage (use cache if available)
                    cached_leverage = self._get_cached_leverage(symbol)
                    if cached_leverage == str(leverage):
                        logger.debug(f"Using cached leverage for {symbol}: {leverage}")
                    else:
                        lev_res = client.set_leverage(symbol, str(leverage))
                        lev_code = lev_res.get("retCode", -1)
                        if lev_code not in [0, 110043]:  # 0 success, 110043 leverage not modified
                            logger.warning(f"Set leverage failed: {lev_res}")
                        else:
                            self._set_cached_leverage(symbol, str(leverage))

                    # Check idempotency key before placing order
                    idempotency_key = f"{signal_id}"
                    previous_status = self._check_idempotency(idempotency_key)
                    if previous_status:
                        if previous_status == "filled":
                            logger.info(f"Order already filled (idempotency): {signal_id}")
                            status = "filled"
                            response_code = 0
                        elif previous_status == "pending":
                            # Check current status of pending order
                            check_result = client.get_order_status(symbol=symbol, client_order_id=idempotency_key)
                            if check_result.get("retCode") == 0:
                                order_list = check_result.get("result", {}).get("list", [])
                                if order_list:
                                    order_status = order_list[0].get("orderStatus", "")
                                    if order_status == self.ORDER_STATUS_FILLED:
                                        self._set_idempotency(idempotency_key, "filled")
                                        status = "filled"
                                        response_code = 0
                                    elif order_status == self.ORDER_STATUS_CANCELLED:
                                        # Can retry
                                        logger.info(f"Previous order was cancelled, placing new order")
                                    else:
                                        # Still pending, return current status
                                        self._set_idempotency(idempotency_key, order_status.lower())
                                        status = order_status.lower()
                                        response_code = -1
                                        error_msg = f"Order still pending: {order_status}"
                        else:
                            # Previous failed/cancelled, can retry
                            logger.info(f"Previous order was {previous_status}, placing new order")

                    if status == "pending":
                        # Prepare order side
                        side = "Buy" if direction.upper() == "LONG" else "Sell"

                        # Place order
                        res = client.place_order(
                            symbol=symbol,
                            side=side,
                            qty=str(qty),
                            order_type="Market",
                            take_profit=str(tp) if tp > 0 else None,
                            stop_loss=str(sl) if sl > 0 else None,
                            client_order_id=idempotency_key
                        )

                        response_code = res.get("retCode", -1)
                        if response_code == 0:
                            # Track idempotency key
                            self._set_idempotency(idempotency_key, "pending")
                            
                            # Get server order ID if available
                            server_order_id = res.get("result", {}).get("orderId")
                            
                            # Poll for fill status (for market orders)
                            if server_order_id or idempotency_key:
                                fill_status, order_info = self._poll_order_fill(
                                    client=client,
                                    symbol=symbol,
                                    order_id=server_order_id or "",
                                    client_order_id=idempotency_key,
                                    timeout=10.0,
                                    poll_interval=0.5
                                )
                                
                                if fill_status == "filled":
                                    status = "filled"
                                    self._set_idempotency(idempotency_key, "filled")
                                    self._record_circuit_breaker_success()
                                    logger.info(f"Order filled successfully: {signal_id}")
                                elif fill_status == "cancelled":
                                    status = "cancelled"
                                    self._set_idempotency(idempotency_key, "cancelled")
                                    error_msg = "Order was cancelled"
                                    logger.warning(error_msg)
                                else:
                                    # Still pending - could be partial fill or slow fill
                                    status = "pending"
                                    logger.warning(f"Order poll timeout, status: {fill_status}")
                                    if order_info:
                                        cum_qty = order_info.get("cumExecQty", "0")
                                        if cum_qty and float(cum_qty) > 0:
                                            status = "partially_filled"
                                            error_msg = f"Partially filled: {cum_qty}/{qty}"
                                            logger.info(error_msg)
                            else:
                                # No way to poll, assume filled for market orders
                                status = "filled"
                                self._set_idempotency(idempotency_key, "filled")
                                self._record_circuit_breaker_success()
                                logger.info(f"Order submitted (no poll): {signal_id}")
                        else:
                            # Handle API errors
                            error_msg = res.get("retMsg", "Unknown error")
                            should_retry, cb_triggered = self._handle_api_error(response_code, error_msg)
                            
                            if cb_triggered:
                                self._record_circuit_breaker_failure()
                            elif response_code == 130006:  # Duplicate order
                                # Check if it's really a duplicate or can proceed
                                logger.warning(f"Duplicate order response: {error_msg}")
                                # Mark as handled to avoid retries
                                status = "rejected"
                                response_code = -1
                            else:
                                self._record_circuit_breaker_failure()
                            
                            status = "error"
                            logger.error(f"Order failed: {error_msg} (code: {response_code})")

            except ValueError as e:
                logger.error(f"Validation error during execution: {e}")
                status = "error"
                error_msg = str(e)
                response_code = -1
                self._record_circuit_breaker_failure()

            except requests.exceptions.RequestException as e:
                logger.error(f"Network error during execution: {e}")
                status = "error"
                error_msg = f"Network error: {str(e)}"
                response_code = -1
                self._record_circuit_breaker_failure()

            except Exception as e:
                logger.error(f"Execution error: {e}", exc_info=True)
                status = "error"
                error_msg = str(e)
                response_code = -1
                self._record_circuit_breaker_failure()

        end_time = time.time()
        latency_ms = (end_time - start_time) * 1000
        latency_sec = latency_ms / 1000

        # Update statistics
        with self._lock:
            self._total_executions += 1
            if response_code == 0:
                self._successful_executions += 1
            else:
                self._failed_executions += 1
        
        # Track metrics
        if METRICS_AVAILABLE:
            active_signals.labels(symbol=symbol).dec()
            
            success = response_code == 0
            status_label = "success" if success else "error"
            error_type = ""
            if not success:
                if "validation" in error_msg.lower():
                    error_type = "validation"
                elif "network" in error_msg.lower():
                    error_type = "network"
                elif "timeout" in error_msg.lower():
                    error_type = "timeout"
                else:
                    error_type = "other"
            
            signal_executions.labels(
                symbol=symbol,
                status=status_label,
                error_type=error_type or "none"
            ).inc()
            signal_execution_latency.labels(
                symbol=symbol,
                status=status_label
            ).observe(latency_sec)
            
            # Record to collector
            collector = get_signal_collector()
            collector.record(
                signal_id=signal_id,
                symbol=symbol,
                status=status,
                latency_ms=latency_ms,
                error_msg=error_msg
            )

        # Log execution
        self._log_trade({
            "signal_id": signal_id,
            "symbol": symbol,
            "direction": direction,
            "qty": str(qty),
            "entry_price": str(entry_price),
            "take_profit": str(tp),
            "stop_loss": str(sl),
            "leverage": str(leverage),
            "status": status,
            "response_code": str(response_code),
            "latency_ms": f"{latency_ms:.2f}",
            "error_msg": error_msg,
            "validation_errors": "",
            "thread_id": str(thread_id)
        })

        # Publish final update
        if self.update_bus:
            try:
                self.update_bus.publish({
                    "type": "EXECUTION_UPDATE",
                    "signal_id": signal_id,
                    "status": status,
                    "latency_ms": latency_ms,
                    "error": error_msg,
                    "timestamp": end_time,
                    "thread_id": thread_id,
                    "response_code": response_code
                })
            except Exception as e:
                logger.warning(f"Failed to publish final execution update: {e}")

        result: ExecutionResult = {
            "status": status,
        }
        if error_msg:
            result["error"] = error_msg
        if response_code != 0:
            result["response_code"] = response_code
        if latency_ms > 0:
            result["latency_ms"] = latency_ms

        return result

    def execute_signal(self, signal: Dict[str, Any]) -> None:
        """
        Execute signal asynchronously using thread pool for better concurrency.

        Args:
            signal: Signal dictionary to execute
        """
        if not self.enabled:
            logger.info("Signal execution disabled")
            return

        try:
            # Submit to thread pool for execution
            future = self._executor.submit(self._execute_signal_sync, signal)

            # Add callback for error handling
            def handle_execution(fut):
                try:
                    result = fut.result(timeout=60)  # 60 second timeout
                    if result.get("status") == "error":
                        logger.error(f"Signal execution failed: {result}")
                except TimeoutError:
                    logger.error(f"Signal execution timed out")
                except Exception as e:
                    logger.error(f"Signal execution exception: {e}")

            future.add_done_callback(handle_execution)

        except Exception as e:
            logger.error(f"Failed to start execution thread: {e}")

    def execute_signal_sync(self, signal: Dict[str, Any]) -> ExecutionResult:
        """
        Synchronous execution that waits for completion.

        Args:
            signal: Signal dictionary to execute (SignalPayload structure)

        Returns:
            Execution result (ExecutionResult structure)
        """
        if not self.enabled:
            return {
                "status": ExecutionStatus.DISABLED.value,
                "error": "Signal execution is disabled"
            }

        return self._execute_signal_sync(signal)

    def get_position(self, symbol: str) -> Dict[str, Any]:
        """Get current position with validation."""
        if not self.api_key or not self.api_secret:
            return {"error": "API credentials not configured"}

        try:
            with ByBitClient(self.api_key, self.api_secret, self.testnet) as client:
                return client.get_position(symbol)
        except Exception as e:
            logger.error(f"Error getting position: {e}")
            return {"error": str(e)}

    def is_position_open(self, symbol: str) -> bool:
        """
        Check if position is open for symbol.

        Args:
            symbol: Trading symbol

        Returns:
            True if position is open, False otherwise
        """
        result = self.get_position(symbol)

        # Check for error response (retCode is not 0 or is None)
        ret_code = result.get("retCode")
        if ret_code is None or ret_code != 0:
            return False

        positions = result.get("result", {}).get("list", [])
        for pos in positions:
            size = float(pos.get("size", 0))
            if size > 0:
                return True
        return False

    def cleanup(self) -> None:
        """Clean up resources."""
        if self._executor:
            self._executor.shutdown(wait=True)
        logger.info("Signal executor cleaned up")

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get execution statistics.

        Returns:
            Dictionary with execution statistics
        """
        with self._lock:
            total = self._total_executions
            success_rate = (
                (self._successful_executions / total * 100)
                if total > 0
                else 0.0
            )

            return {
                "total_executions": total,
                "successful_executions": self._successful_executions,
                "failed_executions": self._failed_executions,
                "validation_errors": self._validation_errors,
                "success_rate_percent": round(success_rate, 2),
                "enabled": self.enabled,
                "dry_run": self.dry_run,
            }

    def reset_statistics(self) -> None:
        """Reset execution statistics."""
        with self._lock:
            self._total_executions = 0
            self._successful_executions = 0
            self._failed_executions = 0
            self._validation_errors = 0

    def __del__(self) -> None:
        """Destructor to ensure cleanup."""
        try:
            self.cleanup()
        except Exception:
            pass  # Ignore errors in destructor
