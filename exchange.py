from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


API_BASE = "https://api.indodax.com"
LIVE_ACKNOWLEDGEMENT = "I_ACCEPT_REAL_ORDERS"
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,36}$")


class IndodaxApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OrderStatusUnknown(IndodaxApiError):
    """A mutating request may have reached Indodax; reconcile before retrying."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        client_order_id: str | None = None,
    ) -> None:
        super().__init__(message, status=status)
        self.client_order_id = client_order_id


def load_local_environment(env_file: Path | None = None) -> None:
    """Load simple KEY=VALUE pairs without overriding existing environment values."""
    path = env_file or Path(__file__).resolve().parent / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IndodaxApiError(f"Unable to read local environment file: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise IndodaxApiError(f"Invalid .env entry on line {line_number}.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class IndodaxCredentials:
    api_key: str
    secret_key: str
    live_trading_enabled: bool = False

    @classmethod
    def from_environment(cls) -> IndodaxCredentials:
        load_local_environment()
        api_key = os.environ.get("INDODAX_API_KEY", "").strip()
        secret_key = os.environ.get("INDODAX_API_SECRET", "").strip()
        if not api_key or not secret_key:
            raise IndodaxApiError(
                "API key belum dikonfigurasi. Isi INDODAX_API_KEY dan "
                "INDODAX_API_SECRET di environment lokal."
            )
        enabled = (
            os.environ.get("INDODAX_LIVE_TRADING", "").strip() == "YES"
            and os.environ.get("INDODAX_LIVE_ACK", "").strip()
            == LIVE_ACKNOWLEDGEMENT
        )
        return cls(api_key, secret_key, enabled)

    def __repr__(self) -> str:
        return (
            "IndodaxCredentials(api_key=<redacted>, secret_key=<redacted>, "
            f"live_trading_enabled={self.live_trading_enabled})"
        )


class IndodaxTradeApiV2:
    """Official TAPI v2 client with no withdrawal operations or network retries."""

    def __init__(
        self,
        credentials: IndodaxCredentials,
        *,
        timeout: float = 10.0,
        base_url: str = API_BASE,
        order_authorizer: Callable[[str, dict[str, str]], None] | None = None,
    ) -> None:
        if not credentials.api_key or not credentials.secret_key:
            raise ValueError("Both Indodax API credentials are required.")
        if timeout <= 0:
            raise ValueError("timeout must be positive.")
        self._credentials = credentials
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._order_authorizer = order_authorizer

    @staticmethod
    def _canonical_params(params: dict[str, str]) -> str:
        return urllib.parse.urlencode(params)

    def _signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        method = method.upper()
        if method not in {"GET", "POST", "DELETE"}:
            raise ValueError("Unsupported Indodax API method.")
        request_params = dict(params or {})
        request_params["timestamp"] = str(time.time_ns() // 1_000_000)
        request_params["recvWindow"] = "5000"
        payload = self._canonical_params(request_params)
        signature = hmac.new(
            self._credentials.secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Accept": "application/json",
            "X-APIKEY": self._credentials.api_key,
            "Sign": signature,
            "User-Agent": "IndodaxTradingBot/1.0",
        }
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request = urllib.request.Request(
                f"{self._base_url}{path}",
                data=payload.encode("ascii"),
                headers=headers,
                method=method,
            )
        else:
            request = urllib.request.Request(
                f"{self._base_url}{path}?{payload}",
                headers=headers,
                method=method,
            )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if method in {"POST", "DELETE"}:
                raise OrderStatusUnknown(
                    "The exchange did not confirm the requested order action. "
                    "Reconcile the order status before taking further action.",
                    status=exc.code,
                ) from exc
            raise IndodaxApiError(
                self._safe_api_error(body, exc.code), status=exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if method in {"POST", "DELETE"}:
                raise OrderStatusUnknown(
                    "Order action status is unknown after a network error. "
                    "Reconcile the order before retrying."
                ) from exc
            raise IndodaxApiError(
                f"Indodax request failed: {type(exc).__name__}."
            ) from exc

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            if method in {"POST", "DELETE"}:
                raise OrderStatusUnknown(
                    "Indodax returned an unreadable order-action response. "
                    "Reconcile the order before retrying."
                ) from exc
            raise IndodaxApiError("Indodax returned invalid JSON.") from exc
        if isinstance(result, dict) and "code" in result and "msg" in result:
            message = str(result.get("msg") or "Indodax API error")
            raise IndodaxApiError(message)
        return result

    @staticmethod
    def _safe_api_error(body: str, status: int) -> str:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return f"Indodax request failed with HTTP {status}."
        if isinstance(payload, dict):
            message = payload.get("msg")
            code = payload.get("code")
            if message:
                return f"Indodax error {code}: {message}"
        return f"Indodax request failed with HTTP {status}."

    def account(self) -> dict[str, Any]:
        result = self._signed_request("GET", "/api/v2/account")
        if not isinstance(result, dict):
            raise IndodaxApiError("Unexpected response from account endpoint.")
        return result

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        result = self._signed_request("GET", "/api/v2/openOrders", params)
        if not isinstance(result, list) or any(
            not isinstance(order, dict) for order in result
        ):
            raise IndodaxApiError("Unexpected response from open-orders endpoint.")
        return result

    def order(
        self,
        symbol: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id is required.")
        params = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        result = self._signed_request("GET", "/api/v2/order", params)
        if not isinstance(result, dict):
            raise IndodaxApiError("Unexpected response from order endpoint.")
        return result

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        price: str | int | float | Decimal,
        quantity: str | int | float | Decimal,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_live_trading()
        normalized_symbol = symbol.upper()
        normalized_side = side.upper()
        if not normalized_symbol.endswith("IDR"):
            raise ValueError("Only IDR spot pairs are allowed.")
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL.")
        if normalized_symbol in {"BTCIDR", "ETHIDR"} and normalized_side == "SELL":
            raise ValueError("Automatic BTC/ETH selling is prohibited.")
        order_price = self._positive_decimal(price, "price")
        order_quantity = self._positive_decimal(quantity, "quantity")
        order_id = client_order_id or f"bot-{uuid.uuid4().hex[:24]}"
        if not ORDER_ID_PATTERN.fullmatch(order_id):
            raise ValueError("client_order_id must be 1-36 alphanumeric, '_' or '-'.")

        self._verify_trade_permissions()
        params = {
            "symbol": normalized_symbol,
            "side": normalized_side,
            "type": "LIMIT",
            "price": format(order_price, "f"),
            "quantity": format(order_quantity, "f"),
            "newClientOrderId": order_id,
            "timeInForce": "GTC",
        }
        self._authorize_order_action("CREATE_ORDER", params)
        try:
            result = self._signed_request("POST", "/api/v2/order", params)
        except OrderStatusUnknown as exc:
            raise OrderStatusUnknown(
                f"{exc} Client order ID: {order_id}.",
                status=exc.status,
                client_order_id=order_id,
            ) from exc
        if not isinstance(result, dict):
            raise OrderStatusUnknown(
                "Order response was unexpected. Reconcile by client order ID; do not resend.",
                client_order_id=order_id,
            )
        return result

    def cancel_order(
        self,
        symbol: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_live_trading()
        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id is required.")
        self._verify_trade_permissions()
        params = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        self._authorize_order_action("CANCEL_ORDER", params)
        result = self._signed_request("DELETE", "/api/v2/order", params)
        if not isinstance(result, dict):
            raise IndodaxApiError("Unexpected response from cancel-order endpoint.")
        return result

    def _require_live_trading(self) -> None:
        if not self._credentials.live_trading_enabled:
            raise IndodaxApiError(
                "Live order actions are disabled. They require both "
                "INDODAX_LIVE_TRADING=YES and the exact local acknowledgement."
            )
        if self._order_authorizer is None:
            raise IndodaxApiError(
                "Order actions are unavailable until a capital-lock executor "
                "is connected."
            )

    def _authorize_order_action(self, action: str, params: dict[str, str]) -> None:
        if self._order_authorizer is None:
            raise RuntimeError("Capital-lock executor disappeared unexpectedly.")
        self._order_authorizer(action, dict(params))

    def _verify_trade_permissions(self) -> None:
        account = self.account()
        if account.get("canTrade") is not True:
            raise IndodaxApiError("API key does not have spot-trading permission.")
        if account.get("canWithdraw") is not False:
            raise IndodaxApiError(
                "API key must have crypto withdrawals disabled before live trading."
            )
        if account.get("canWithdrawFiat") is not False:
            raise IndodaxApiError(
                "API key must have IDR withdrawals disabled before live trading."
            )

    @staticmethod
    def _positive_decimal(value: str | int | float | Decimal, name: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be a valid decimal number.") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError(f"{name} must be greater than zero.")
        return parsed
