import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from exchange import IndodaxCredentials

from bot import (
    DCA_RESERVE_PER_POSITION_IDR,
    ENTRY_BUDGET_IDR,
    EXCLUDED_ASSETS,
    Candle,
    BotService,
    IndodaxPublicClient,
    Market,
    MarketScanner,
    PaperPortfolio,
    analyze_candles,
)


def market(
    asset: str = "TEST",
    pair_id: str = "testidr",
    price: float = 100,
    bid: float = 99.8,
    ask: float = 100,
    volume: float = 100_000_000,
) -> Market:
    return Market(
        pair_id=pair_id,
        symbol=f"{asset}IDR",
        asset=asset,
        last=price,
        bid=bid,
        ask=ask,
        volume_idr_24h=volume,
        high_24h=price * 1.1,
        low_24h=price * 0.9,
        active=True,
    )


class MarketScannerTests(unittest.TestCase):
    def test_detects_early_momentum_and_excludes_blocked_assets(self) -> None:
        scanner = MarketScanner()
        scanner.analyze([market()], timestamp=0)
        results = scanner.analyze([market(price=101, bid=100.8, ask=101)], timestamp=61)
        self.assertEqual(results[0]["status"], "EARLY MOMENTUM")

        excluded = scanner.analyze(
            [market(asset="BTC", pair_id="btcidr", price=101, bid=100.8, ask=101)],
            timestamp=122,
        )
        self.assertIn("BTC", EXCLUDED_ASSETS)
        self.assertFalse(excluded[0]["eligible"])

    def test_ignores_invalid_market_price_and_spread(self) -> None:
        scanner = MarketScanner()
        low = market(price=50, bid=49, ask=50)
        wide = market(pair_id="wideidr", price=100, bid=90, ask=100)
        results = scanner.analyze([low, wide], timestamp=1)
        self.assertFalse(next(x for x in results if x["pair_id"] == "testidr")["eligible"])
        self.assertFalse(next(x for x in results if x["pair_id"] == "wideidr")["eligible"])

    def test_multi_timeframe_candle_metrics_are_computed(self) -> None:
        candles = [
            Candle(
                timestamp=index * 60,
                open=99.5 + index,
                high=100.1 + index,
                low=99.0 + index,
                close=100 + index,
                volume=200 if index == 29 else 100,
            )
            for index in range(30)
        ]
        result = analyze_candles(candles)
        self.assertTrue(result["ema_bullish"])
        self.assertEqual(result["relative_volume"], 2)
        self.assertEqual(result["candles"], 30)
        self.assertIsNotNone(result["rsi_14"])
        self.assertIsNotNone(result["atr_percent"])

    def test_possible_false_breakout_is_flagged(self) -> None:
        candles = [
            Candle(index * 60, 99, 101, 98, 100, 100)
            for index in range(25)
        ]
        candles[-1] = Candle(24 * 60, 100, 102, 99, 100, 100)
        self.assertTrue(analyze_candles(candles)["failed_breakout"])

    def test_candidate_without_verified_ohlc_is_not_entry_ready(self) -> None:
        scanner = MarketScanner()
        signals = scanner.analyze([market()], timestamp=0)
        scanner.attach_technicals(signals, {}, {})
        self.assertFalse(signals[0]["entry_ready"])
        self.assertEqual(signals[0]["status"], "WAITING TECHNICAL CHECK")

    def test_exit_weakness_needs_more_than_one_short_red_candle(self) -> None:
        scanner = MarketScanner()
        signals = scanner.analyze([market()], timestamp=0)
        signal = signals[0]
        signal["technical_fresh"] = True
        signal["technical"] = {
            "1m": {"change_1m_percent": -0.3, "change_5m_percent": None},
            "15m": {"failed_breakout": False, "ema_bullish": True},
        }
        self.assertEqual(scanner.weak_pairs(), set())

        signal["technical"]["1m"]["change_5m_percent"] = -0.8
        self.assertEqual(scanner.weak_pairs(), {"testidr"})

    def test_candidate_requires_fresh_coherent_multi_timeframe_data(self) -> None:
        now = int(time.time())
        scanner = MarketScanner()
        signals = scanner.analyze(
            [market(price=129, bid=128.8, ask=129)],
            timestamp=now,
        )
        rising = [
            Candle(
                timestamp=now - (29 - index) * 60,
                open=99.5 + index,
                high=100.1 + index,
                low=99.0 + index,
                close=100 + index,
                volume=100,
            )
            for index in range(30)
        ]
        context = {
            timeframe: analyze_candles(rising)
            for timeframe in ("1m", "15m", "1h", "4h", "1d")
        }
        scanner.attach_technicals(signals, {"testidr": context}, {})
        self.assertTrue(signals[0]["entry_ready"])

        stale = [
            Candle(
                candle.timestamp - 3_600,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
            )
            for candle in rising
        ]
        stale_context = {
            timeframe: analyze_candles(stale)
            for timeframe in ("1m", "15m", "1h", "4h", "1d")
        }
        scanner.attach_technicals(signals, {"testidr": stale_context}, {})
        self.assertFalse(signals[0]["entry_ready"])
        self.assertEqual(signals[0]["status"], "STALE OR INCONSISTENT DATA")

    def test_public_ohlc_parser_uses_supported_documented_endpoint(self) -> None:
        payload = [
            {
                "Time": 1_700_000_000,
                "Open": 100,
                "High": 102,
                "Low": 99,
                "Close": 101,
                "Volume": "1200",
            }
        ]

        class FakeResponse:
            status = 200

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                import json

                return json.dumps(payload).encode()

        with patch("bot.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            candles = IndodaxPublicClient().fetch_candles("testidr", "15", limit=20)
        self.assertEqual(candles[0].close, 101)
        request = urlopen.call_args.args[0]
        self.assertIn("/tradingview/history_v2?", request.full_url)
        self.assertIn("tf=15", request.full_url)


class PaperPortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.portfolio = PaperPortfolio(Path(self.temp.name) / "paper.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_private_api_preflight_is_read_only_and_redacts_account_identity(self) -> None:
        service = BotService(
            portfolio=self.portfolio,
            client=object(),  # The preflight uses its own authenticated client.
        )

        class FakePrivateClient:
            def __init__(self, credentials: object) -> None:
                pass

            def account(self) -> dict[str, object]:
                return {
                    "canTrade": True,
                    "canWithdraw": False,
                    "canWithdrawFiat": False,
                    "uid": 123456,
                    "balances": [
                        {"asset": "IDR", "free": "7000000", "locked": "0"}
                    ],
                }

            def open_orders(self) -> list[dict[str, object]]:
                return [
                    {
                        "symbol": "TESTIDR",
                        "orderId": 10,
                        "clientOrderId": "user-order",
                        "side": "BUY",
                        "origQty": "10",
                        "executedQty": "0",
                        "status": "NEW",
                        "type": "LIMIT",
                        "internalField": "not-returned",
                    }
                ]

        with (
            patch(
                "bot.IndodaxCredentials.from_environment",
                return_value=IndodaxCredentials("key", "secret", False),
            ),
            patch("bot.IndodaxTradeApiV2", FakePrivateClient),
        ):
            result = service.private_api_preflight()

        self.assertTrue(result["permissions_safe_for_trading"])
        self.assertEqual(result["idr_available"], 7_000_000)
        self.assertNotIn("uid", result)
        self.assertNotIn("internalField", result["open_orders"][0])

    def test_scanner_restart_does_not_report_persisted_stale_running_state(self) -> None:
        self.portfolio.set_setting("scanner_running", 1)
        service = BotService(portfolio=self.portfolio, client=object())
        self.assertFalse(service.state()["scanner_running"])

    def test_entry_fee_included_and_position_limit_enforced(self) -> None:
        first = self.portfolio.open_position(market(), score=3)
        self.assertGreater(first["quantity"], 0)
        self.assertEqual(
            self.portfolio.get_setting("trade_cash_idr"),
            8_000_000 - ENTRY_BUDGET_IDR,
        )
        second_market = market(asset="NEXT", pair_id="nextidr")
        self.portfolio.open_position(second_market, score=2)
        with self.assertRaises(ValueError):
            self.portfolio.open_position(market(asset="THIRD", pair_id="thirdidr"), score=4)

    def test_fixed_entry_remains_available_when_cash_covers_entry_and_locked_dca(self) -> None:
        self.portfolio.open_position(market(), score=3)
        position_id = self.portfolio.state({})["positions"][0]["id"]
        self.portfolio.manual_paper_sell(position_id, price=90)
        self.assertLess(self.portfolio.get_setting("trade_cash_idr"), 8_000_000)
        self.portfolio.open_position(market(asset="NEXT", pair_id="nextidr"), score=2)

    def test_locked_profit_splits_evenly_after_manual_paper_sell(self) -> None:
        self.portfolio.open_position(market(), score=3)
        state = self.portfolio.state({"testidr": market(price=120)})
        position_id = state["positions"][0]["id"]
        self.portfolio.manual_paper_sell(position_id, price=120)
        self.assertGreater(self.portfolio.get_setting("locked_profit_idr"), 0)
        self.assertAlmostEqual(
            self.portfolio.get_setting("locked_btc_idr"),
            self.portfolio.get_setting("locked_eth_idr"),
        )

    def test_dca_is_simulation_only_and_is_capped(self) -> None:
        self.portfolio.open_position(market(), score=3)
        position_id = self.portfolio.state({})["positions"][0]["id"]
        before_cash = self.portfolio.get_setting("trade_cash_idr")
        result = self.portfolio.simulate_dca(position_id, 500_000, 80)
        self.assertIn("new_average_price", result)
        self.assertEqual(self.portfolio.get_setting("trade_cash_idr"), before_cash)
        with self.assertRaises(ValueError):
            self.portfolio.simulate_dca(
                position_id, DCA_RESERVE_PER_POSITION_IDR + 1, 80
            )

    def test_negative_position_is_reviewed_not_auto_sold(self) -> None:
        self.portfolio.open_position(market(), score=3)
        before_cash = self.portfolio.get_setting("trade_cash_idr")
        self.portfolio.update_positions(
            {"testidr": market(price=88, bid=87.8, ask=88)},
            weak_pairs={"testidr"},
        )
        state = self.portfolio.state({"testidr": market(price=88)})
        self.assertEqual(state["positions"][0]["status"], "SCALPING — UNDER REVIEW")
        self.assertEqual(self.portfolio.get_setting("trade_cash_idr"), before_cash)
        alert = next(
            event for event in state["events"]
            if event["event_type"] == "DRAWDOWN_WARNING"
        )
        self.assertEqual(alert["detail"]["threshold_percent"], 10)

    def test_take_profit_sells_40_40_20_percent_of_original_quantity(self) -> None:
        self.portfolio.open_position(market(), score=3)
        position = self.portfolio.state({})["positions"][0]
        original_quantity = position["original_quantity"]
        profitable_market = market(price=120, bid=119.8, ask=120)
        for _ in range(3):
            self.portfolio.update_positions(
                {"testidr": profitable_market}, weak_pairs={"testidr"}
            )
        state = self.portfolio.state({"testidr": profitable_market})
        position_after = next(p for p in state["positions"] if p["id"] == position["id"])
        self.assertEqual(position_after["status"], "CLOSED")
        self.assertAlmostEqual(position_after["quantity"], 0)
        events = [event for event in state["events"] if event["event_type"].startswith("PAPER_TAKE_PROFIT")]
        sold_quantities = [event["detail"]["quantity"] for event in reversed(events)]
        self.assertEqual(len(sold_quantities), 3)
        self.assertAlmostEqual(sold_quantities[0], original_quantity * 0.4)
        self.assertAlmostEqual(sold_quantities[1], original_quantity * 0.4)
        self.assertAlmostEqual(sold_quantities[2], original_quantity * 0.2)


if __name__ == "__main__":
    unittest.main()
