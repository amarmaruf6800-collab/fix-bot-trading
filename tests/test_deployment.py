import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from bot import DashboardHandler, is_loopback_client


class DeploymentTests(unittest.TestCase):
    def test_loopback_address_check(self) -> None:
        self.assertTrue(is_loopback_client("127.0.0.1"))
        self.assertTrue(is_loopback_client("::1"))
        self.assertFalse(is_loopback_client("203.0.113.10"))
        self.assertFalse(is_loopback_client("not-an-ip"))

    def test_private_api_preflight_is_available_only_to_loopback_client(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        host, port = server.server_address
        allowed_host = f"{host}:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch("bot.BOT_ALLOWED_HOST", allowed_host),
                patch.object(
                    __import__("bot").SERVICE,
                    "private_api_preflight",
                    return_value={"connected": True},
                ) as preflight,
            ):
                request = urllib.request.Request(
                    f"http://{allowed_host}/api/exchange/preflight",
                    data=b"{}",
                    headers={
                        "Origin": f"http://{allowed_host}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(json.loads(response.read()), {"connected": True})
                preflight.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wrong_origin_is_rejected(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        host, port = server.server_address
        allowed_host = f"{host}:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("bot.BOT_ALLOWED_HOST", allowed_host):
                request = urllib.request.Request(
                    f"http://{allowed_host}/api/scanner/start",
                    data=b"{}",
                    headers={
                        "Origin": "http://attacker.invalid",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=2)
                error.exception.close()
                self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
