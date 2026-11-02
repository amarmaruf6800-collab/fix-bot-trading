import hashlib
import hmac
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from exchange import (
    IndodaxApiError,
    IndodaxCredentials,
    IndodaxTradeApiV2,
    OrderStatusUnknown,
    load_local_environment,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class IndodaxTradeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = IndodaxCredentials(
            api_key="local-test-key",
            secret_key="local-test-secret",
            live_trading_enabled=True,
        )
        self.client = IndodaxTradeApiV2(
            self.credentials,
            order_authorizer=lambda action, params: None,
        )

    def test_limit_order_is_signed_and_contains_a_client_order_id(self) -> None:
        account = {
            "canTrade": True,
            "canWithdraw": False,
            "canWithdrawFiat": False,
            "balances": [],
        }
        with patch(
            "exchange.urllib.request.urlopen",
            side_effect=[FakeResponse(account), FakeResponse({"orderId": 123})],
        ) as urlopen:
            response = self.client.create_limit_order(
                "testidr", "BUY", "101.25", "12.5",
                client_order_id="bot-test-order-1",
            )

        self.assertEqual(response["orderId"], 123)
        self.assertEqual(urlopen.call_count, 2)
        request = urlopen.call_args_list[-1].args[0]
        body = request.data.decode("ascii")
        signature = hmac.new(
            self.credentials.secret_key.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(request.get_header("Sign"), signature)
        self.assertEqual(request.get_header("X-apikey"), "local-test-key")
        self.assertIn("newClientOrderId=bot-test-order-1", body)
        self.assertIn("type=LIMIT", body)

    def test_order_actions_need_both_local_live_gates(self) -> None:
        disabled = IndodaxTradeApiV2(
            IndodaxCredentials("key", "secret", live_trading_enabled=False)
        )
        with patch("exchange.urllib.request.urlopen") as urlopen:
            with self.assertRaises(IndodaxApiError):
                disabled.create_limit_order("TESTIDR", "BUY", 10, 1)
        urlopen.assert_not_called()

    def test_order_actions_require_a_capital_lock_executor(self) -> None:
        client = IndodaxTradeApiV2(self.credentials)
        with patch("exchange.urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(IndodaxApiError, "capital-lock executor"):
                client.create_limit_order("TESTIDR", "BUY", 10, 1)
        urlopen.assert_not_called()

    def test_invalid_or_prohibited_order_is_rejected_before_network(self) -> None:
        with patch("exchange.urllib.request.urlopen") as urlopen:
            with self.assertRaises(ValueError):
                self.client.create_limit_order("BTCIDR", "SELL", 10, 1)
            with self.assertRaises(ValueError):
                self.client.create_limit_order("TESTUSDT", "BUY", 10, 1)
            with self.assertRaises(ValueError):
                self.client.create_limit_order("TESTIDR", "BUY", 0, 1)
        urlopen.assert_not_called()

    def test_api_key_with_withdrawal_permission_cannot_trade(self) -> None:
        unsafe_account = FakeResponse(
            {
                "canTrade": True,
                "canWithdraw": True,
                "canWithdrawFiat": False,
                "balances": [],
            }
        )
        with patch(
            "exchange.urllib.request.urlopen", return_value=unsafe_account
        ) as urlopen:
            with self.assertRaises(IndodaxApiError):
                self.client.create_limit_order("TESTIDR", "BUY", 10, 1)
        self.assertEqual(urlopen.call_count, 1)

    def test_uncertain_create_order_response_is_not_retried(self) -> None:
        with patch(
            "exchange.urllib.request.urlopen",
            side_effect=[
                FakeResponse(
                    {
                        "canTrade": True,
                        "canWithdraw": False,
                        "canWithdrawFiat": False,
                        "balances": [],
                    }
                ),
                TimeoutError("timeout"),
            ],
        ) as urlopen:
            with self.assertRaises(OrderStatusUnknown) as error:
                self.client.create_limit_order("TESTIDR", "BUY", 10, 1)
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("Client order ID:", str(error.exception))
        self.assertIsNotNone(error.exception.client_order_id)

    def test_credentials_are_redacted_and_local_env_does_not_override_process_env(self) -> None:
        self.assertNotIn("local-test-secret", repr(self.credentials))
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("INDODAX_API_KEY=file-key\nINDODAX_API_SECRET=file-secret\n", encoding="utf-8")
            with patch.dict("os.environ", {"INDODAX_API_KEY": "process-key"}, clear=True):
                load_local_environment(env_file)
                import os

                self.assertEqual(os.environ["INDODAX_API_KEY"], "process-key")
                self.assertEqual(os.environ["INDODAX_API_SECRET"], "file-secret")

    def test_failed_create_response_raises_reconciliation_required(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.indodax.com/api/v2/order",
            503,
            "unavailable",
            {},
            None,
        )
        error.read = lambda: b'{"msg":"unavailable"}'  # type: ignore[method-assign]
        account = FakeResponse(
            {
                "canTrade": True,
                "canWithdraw": False,
                "canWithdrawFiat": False,
                "balances": [],
            }
        )
        with patch("exchange.urllib.request.urlopen", side_effect=[account, error]):
            with self.assertRaises(OrderStatusUnknown):
                self.client.create_limit_order("TESTIDR", "BUY", 10, 1)


if __name__ == "__main__":
    unittest.main()
