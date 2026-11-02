from __future__ import annotations

import json
import ipaddress
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, unquote, urlparse

from exchange import IndodaxApiError, IndodaxCredentials, IndodaxTradeApiV2


ROOT = Path(__file__).resolve().parent
DATA_DIRECTORY = Path(os.environ.get("BOT_DATA_DIR", str(ROOT))).resolve()
DATABASE = DATA_DIRECTORY / "paper_trading.sqlite3"
INDEX_FILE = ROOT / "dashboard.html"
API_BASE = "https://indodax.com/api"
BOT_HOST = os.environ.get("BOT_HOST", "127.0.0.1").strip()
BOT_PORT = int(os.environ.get("BOT_PORT", "8000"))
BOT_ALLOWED_HOST = os.environ.get(
    "BOT_ALLOWED_HOST",
    f"{'127.0.0.1' if BOT_HOST in {'127.0.0.1', '::1', 'localhost'} else BOT_HOST}:{BOT_PORT}",
).strip()
SCAN_INTERVAL_SECONDS = 30
ENTRY_BUDGET_IDR = 1_500_000
POSITION_LIMIT = 2
POSITION_CAPITAL_IDR = 4_000_000
DCA_RESERVE_PER_POSITION_IDR = 2_500_000
MANUAL_RESERVE_IDR = 2_000_000
MAX_TRADING_CAPITAL_IDR = 10_000_000
TAKER_FEE_RATE = 0.002
MIN_ENTRY_PRICE_IDR = 50
EXCLUDED_ASSETS = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "DOT", "HYPE",
    "AAVE", "HBAR", "ALGO", "NEAR", "ONDO",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("indodax-paper-bot")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def is_loopback_client(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Market:
    pair_id: str
    symbol: str
    asset: str
    last: float
    bid: float
    ask: float
    volume_idr_24h: float
    high_24h: float
    low_24h: float
    active: bool

    @property
    def spread_percent(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / mid) * 100 if mid else None


@dataclass(frozen=True)
class Observation:
    timestamp: float
    price: float
    volume_idr_24h: float


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndodaxPublicClient:
    """Read-only public market data client. This client has no order endpoints."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{API_BASE}/{path}",
            headers={"User-Agent": "IndodaxPaperScanner/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"Indodax returned HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Indodax public data request failed: {exc}") from exc

    def fetch_markets(self) -> list[Market]:
        pairs = self._get_json("pairs")
        ticker_payload = self._get_json("ticker_all")
        tickers = ticker_payload.get("tickers")
        if not isinstance(pairs, list) or not isinstance(tickers, dict):
            raise RuntimeError("Indodax returned an unexpected markets/ticker response")

        markets: list[Market] = []
        for pair in pairs:
            if not isinstance(pair, dict) or str(pair.get("base_currency", "")).lower() != "idr":
                continue
            pair_id = str(pair.get("id", "")).lower()
            ticker_id = str(pair.get("ticker_id", "")).lower()
            ticker = tickers.get(ticker_id)
            if not pair_id or not isinstance(ticker, dict):
                continue
            asset = str(pair.get("traded_currency_unit") or "").upper()
            if not asset:
                asset = str(pair.get("traded_currency") or "").upper()
            markets.append(
                Market(
                    pair_id=pair_id,
                    symbol=str(pair.get("symbol") or f"{asset}IDR").upper(),
                    asset=asset,
                    last=number(ticker.get("last")),
                    bid=number(ticker.get("buy")),
                    ask=number(ticker.get("sell")),
                    volume_idr_24h=number(ticker.get("vol_idr")),
                    high_24h=number(ticker.get("high")),
                    low_24h=number(ticker.get("low")),
                    active=(
                        not bool(pair.get("is_maintenance"))
                        and not bool(pair.get("is_market_suspended"))
                    ),
                )
            )
        return markets

    def fetch_candles(
        self, pair_id: str, timeframe: str, limit: int = 100
    ) -> list[Candle]:
        supported_timeframes = {"1", "15", "60", "240", "1D"}
        if timeframe not in supported_timeframes:
            raise ValueError(f"Unsupported candle timeframe: {timeframe}")
        if not pair_id.isalnum() or not pair_id.islower():
            raise ValueError("Invalid Indodax pair id.")
        duration_seconds = 86_400 if timeframe == "1D" else int(timeframe) * 60
        end = int(time.time())
        query = urlencode(
            {
                "from": end - duration_seconds * limit,
                "symbol": pair_id,
                "tf": timeframe,
                "to": end,
            }
        )
        request = urllib.request.Request(
            f"https://indodax.com/tradingview/history_v2?{query}",
            headers={"User-Agent": "IndodaxPaperScanner/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"Indodax returned HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Indodax OHLC request failed: {exc}") from exc
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected OHLC response for {pair_id} {timeframe}.")

        candles: list[Candle] = []
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError(f"Malformed OHLC candle for {pair_id} {timeframe}.")
            candle = Candle(
                timestamp=int(number(item.get("Time"), -1)),
                open=number(item.get("Open"), -1),
                high=number(item.get("High"), -1),
                low=number(item.get("Low"), -1),
                close=number(item.get("Close"), -1),
                volume=number(item.get("Volume"), -1),
            )
            if (
                candle.timestamp < 0
                or candle.open <= 0
                or candle.high <= 0
                or candle.low <= 0
                or candle.close <= 0
                or candle.volume < 0
                or candle.high < max(candle.open, candle.close, candle.low)
                or candle.low > min(candle.open, candle.close, candle.high)
            ):
                raise RuntimeError(f"Invalid OHLC values for {pair_id} {timeframe}.")
            candles.append(candle)
        candles.sort(key=lambda candle: candle.timestamp)
        return candles[-limit:]


class PaperPortfolio:
    """SQLite-backed simulation ledger; it never sends exchange orders."""

    def __init__(self, database: Path = DATABASE) -> None:
        self.database = database
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    pair_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    average_price REAL NOT NULL,
                    original_quantity REAL NOT NULL,
                    quantity REAL NOT NULL,
                    cost_idr REAL NOT NULL,
                    entered_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tp_stage INTEGER NOT NULL DEFAULT 0,
                    current_price REAL NOT NULL,
                    under_review INTEGER NOT NULL DEFAULT 0,
                    last_drawdown_alert INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            position_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(positions)")
            }
            if "last_drawdown_alert" not in position_columns:
                connection.execute(
                    "ALTER TABLE positions ADD COLUMN last_drawdown_alert "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            defaults = {
                "trade_cash_idr": str(MAX_TRADING_CAPITAL_IDR - MANUAL_RESERVE_IDR),
                "realized_since_lock_idr": "0",
                "locked_profit_idr": "0",
                "locked_btc_idr": "0",
                "locked_eth_idr": "0",
                "auto_paper_entry": "1",
                "scanner_running": "0",
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )

    @staticmethod
    def _setting(connection: sqlite3.Connection, key: str) -> float:
        row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return number(row["value"]) if row else 0

    @staticmethod
    def _set_setting(connection: sqlite3.Connection, key: str, value: float) -> None:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def set_setting(self, key: str, value: float) -> None:
        with self._lock, self._connect() as connection:
            self._set_setting(connection, key, value)

    def get_setting(self, key: str) -> float:
        with self._connect() as connection:
            return self._setting(connection, key)

    def open_pair_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT pair_id FROM positions WHERE status='OPEN'"
            ).fetchall()
        return {str(row["pair_id"]) for row in rows}

    def _event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        symbol: str | None,
        detail: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events(created_at, event_type, symbol, detail) VALUES(?, ?, ?, ?)",
            (now_iso(), event_type, symbol, json.dumps(detail, ensure_ascii=True)),
        )

    def open_position(self, market: Market, score: float) -> dict[str, Any]:
        if not market.active or market.last <= MIN_ENTRY_PRICE_IDR:
            raise ValueError("Market tidak aktif atau harga tidak memenuhi batas minimum.")
        if market.spread_percent is None or market.spread_percent > 1.5:
            raise ValueError("Spread tidak dapat diverifikasi atau lebih dari 1,5%.")
        with self._lock, self._connect() as connection:
            open_count = connection.execute(
                "SELECT COUNT(*) FROM positions WHERE status='OPEN'"
            ).fetchone()[0]
            if open_count >= POSITION_LIMIT:
                raise ValueError("Batas maksimal dua posisi aktif telah tercapai.")
            if connection.execute(
                "SELECT 1 FROM positions WHERE status='OPEN' AND pair_id=?",
                (market.pair_id,),
            ).fetchone():
                raise ValueError("Posisi paper untuk aset ini sudah terbuka.")

            trade_cash = self._setting(connection, "trade_cash_idr")
            reserved_dca = POSITION_LIMIT * DCA_RESERVE_PER_POSITION_IDR
            required_entry_and_reserve = reserved_dca + ENTRY_BUDGET_IDR
            if trade_cash < required_entry_and_reserve:
                raise ValueError("Anggaran entry khusus tidak tersedia setelah dana terkunci.")

            gross_order_value = ENTRY_BUDGET_IDR / (1 + TAKER_FEE_RATE)
            entry_fee = ENTRY_BUDGET_IDR - gross_order_value
            quantity = gross_order_value / market.ask
            if quantity * market.ask + entry_fee > ENTRY_BUDGET_IDR + 0.01:
                raise ValueError("Biaya membuat debit melampaui anggaran entry.")

            position_id = uuid.uuid4().hex[:12]
            connection.execute(
                """
                INSERT INTO positions(
                    id, pair_id, symbol, asset, entry_price, average_price,
                    original_quantity, quantity, cost_idr, entered_at, status,
                    current_price, under_review
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, 0)
                """,
                (
                    position_id,
                    market.pair_id,
                    market.symbol,
                    market.asset,
                    market.ask,
                    market.ask,
                    quantity,
                    quantity,
                    ENTRY_BUDGET_IDR,
                    now_iso(),
                    market.last,
                ),
            )
            self._set_setting(connection, "trade_cash_idr", trade_cash - ENTRY_BUDGET_IDR)
            self._event(
                connection,
                "PAPER_ENTRY",
                market.symbol,
                {
                    "position_id": position_id,
                    "score": round(score, 2),
                    "budget_idr_including_fee": ENTRY_BUDGET_IDR,
                    "price": market.ask,
                    "quantity": quantity,
                    "fee_idr": entry_fee,
                },
            )
            return {"id": position_id, "quantity": quantity, "price": market.ask}

    def update_positions(self, markets_by_pair: dict[str, Market], weak_pairs: set[str]) -> None:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
            for row in rows:
                market = markets_by_pair.get(row["pair_id"])
                if market is None or market.last <= 0:
                    continue
                pnl_percent = (
                    ((market.last * row["quantity"] * (1 - TAKER_FEE_RATE))
                     / row["cost_idr"]) - 1
                ) * 100
                under_review = int(pnl_percent < 0)
                connection.execute(
                    "UPDATE positions SET current_price=?, under_review=? WHERE id=?",
                    (market.last, under_review, row["id"]),
                )
                drawdown_percent = max(0.0, -pnl_percent)
                alert_level = 0
                if drawdown_percent >= 10:
                    alert_level = 10
                if drawdown_percent >= 20:
                    alert_level = 20
                if drawdown_percent >= 40:
                    alert_level = 40 + int((drawdown_percent - 40) // 10) * 10
                if alert_level > row["last_drawdown_alert"]:
                    self._event(
                        connection,
                        "DRAWDOWN_WARNING",
                        row["symbol"],
                        {
                            "threshold_percent": alert_level,
                            "actual_drawdown_percent": round(drawdown_percent, 2),
                            "message": "Peringatan analisis saja; tidak menjual atau menambah posisi.",
                        },
                    )
                    connection.execute(
                        "UPDATE positions SET last_drawdown_alert=? WHERE id=?",
                        (alert_level, row["id"]),
                    )
                if under_review and not row["under_review"]:
                    self._event(
                        connection,
                        "UNDER_REVIEW",
                        row["symbol"],
                        {"unrealized_pnl_percent": round(pnl_percent, 2)},
                    )
                if market.pair_id in weak_pairs and pnl_percent > 0:
                    self._sell_stage(connection, row, market.last)
            self._lock_realized_profit_if_ready(connection)

    def _sell_stage(
        self, connection: sqlite3.Connection, row: sqlite3.Row, price: float
    ) -> None:
        stage = int(row["tp_stage"])
        if stage >= 3:
            return
        fractions = (0.4, 0.4, 0.2)
        target_quantity = row["original_quantity"] * fractions[stage]
        quantity = min(target_quantity, row["quantity"])
        if quantity <= 0:
            return
        gross = quantity * price
        fee = gross * TAKER_FEE_RATE
        net_proceeds = gross - fee
        allocated_cost = row["cost_idr"] * (quantity / row["quantity"])
        realized = net_proceeds - allocated_cost
        trade_cash = self._setting(connection, "trade_cash_idr")
        realized_total = self._setting(connection, "realized_since_lock_idr")
        connection.execute(
            """
            UPDATE positions
            SET quantity=?, cost_idr=?, tp_stage=?,
                status=CASE WHEN ? <= 1e-12 THEN 'CLOSED' ELSE status END
            WHERE id=?
            """,
            (
                max(0.0, row["quantity"] - quantity),
                max(0.0, row["cost_idr"] - allocated_cost),
                stage + 1,
                max(0.0, row["quantity"] - quantity),
                row["id"],
            ),
        )
        self._set_setting(connection, "trade_cash_idr", trade_cash + net_proceeds)
        self._set_setting(
            connection, "realized_since_lock_idr", realized_total + realized
        )
        self._event(
            connection,
            f"PAPER_TAKE_PROFIT_{stage + 1}",
            row["symbol"],
            {
                "quantity": quantity,
                "price": price,
                "fee_idr": fee,
                "realized_pnl_idr": realized,
                "remaining_quantity": max(0.0, row["quantity"] - quantity),
            },
        )

    def manual_paper_sell(self, position_id: str, price: float) -> None:
        if price <= 0:
            raise ValueError("Harga jual harus lebih besar dari nol.")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM positions WHERE id=? AND status='OPEN'", (position_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Posisi paper tidak ditemukan atau sudah ditutup.")
            gross = row["quantity"] * price
            fee = gross * TAKER_FEE_RATE
            proceeds = gross - fee
            realized = proceeds - row["cost_idr"]
            cash = self._setting(connection, "trade_cash_idr")
            realized_total = self._setting(connection, "realized_since_lock_idr")
            connection.execute(
                "UPDATE positions SET quantity=0, cost_idr=0, status='CLOSED' WHERE id=?",
                (position_id,),
            )
            self._set_setting(connection, "trade_cash_idr", cash + proceeds)
            self._set_setting(
                connection, "realized_since_lock_idr", realized_total + realized
            )
            self._event(
                connection,
                "MANUAL_PAPER_SELL",
                row["symbol"],
                {"price": price, "fee_idr": fee, "realized_pnl_idr": realized},
            )
            self._lock_realized_profit_if_ready(connection)

    def _lock_realized_profit_if_ready(self, connection: sqlite3.Connection) -> None:
        open_positions = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE status='OPEN'"
        ).fetchone()[0]
        if open_positions:
            return
        realized = self._setting(connection, "realized_since_lock_idr")
        if realized <= 0:
            return
        total_to_lock = realized
        cash = self._setting(connection, "trade_cash_idr")
        if cash - total_to_lock < 0:
            raise RuntimeError("Profit lock gagal: saldo paper tidak mencukupi.")
        half = total_to_lock / 2
        self._set_setting(connection, "trade_cash_idr", cash - total_to_lock)
        self._set_setting(
            connection,
            "locked_profit_idr",
            self._setting(connection, "locked_profit_idr") + total_to_lock,
        )
        self._set_setting(
            connection,
            "locked_btc_idr",
            self._setting(connection, "locked_btc_idr") + half,
        )
        self._set_setting(
            connection,
            "locked_eth_idr",
            self._setting(connection, "locked_eth_idr") + total_to_lock - half,
        )
        self._set_setting(connection, "realized_since_lock_idr", 0)
        self._event(
            connection,
            "PROFIT_LOCKED",
            None,
            {
                "total_idr": total_to_lock,
                "btc_allocation_idr": half,
                "eth_allocation_idr": total_to_lock - half,
                "status": "PROFIT WAITING ALLOCATION",
            },
        )

    @staticmethod
    def _position_dict(row: sqlite3.Row, market: Market | None) -> dict[str, Any]:
        price = market.last if market and market.last > 0 else row["current_price"]
        gross_value = price * row["quantity"]
        pnl = gross_value * (1 - TAKER_FEE_RATE) - row["cost_idr"]
        return {
            "id": row["id"],
            "pair_id": row["pair_id"],
            "symbol": row["symbol"],
            "asset": row["asset"],
            "entry_price": row["entry_price"],
            "average_price": row["average_price"],
            "current_price": price,
            "original_quantity": row["original_quantity"],
            "quantity": row["quantity"],
            "cost_idr": row["cost_idr"],
            "unrealized_pnl_idr": pnl,
            "unrealized_pnl_percent": (
                pnl / row["cost_idr"] * 100 if row["cost_idr"] else 0
            ),
            "status": "SCALPING — UNDER REVIEW" if row["under_review"] else row["status"],
            "tp_stage": row["tp_stage"],
            "entered_at": row["entered_at"],
        }

    def state(self, markets_by_pair: dict[str, Market]) -> dict[str, Any]:
        with self._connect() as connection:
            position_rows = connection.execute(
                "SELECT * FROM positions ORDER BY entered_at DESC"
            ).fetchall()
            events = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT 25"
            ).fetchall()
            cash = self._setting(connection, "trade_cash_idr")
            locked = self._setting(connection, "locked_profit_idr")
            open_positions = [
                row for row in position_rows if row["status"] == "OPEN"
            ]
            free_slots = max(0, POSITION_LIMIT - len(open_positions))
            entry_capacity = min(
                free_slots,
                int(
                    max(
                        0.0,
                        cash - POSITION_LIMIT * DCA_RESERVE_PER_POSITION_IDR,
                    )
                    // ENTRY_BUDGET_IDR
                ),
            )
            entry_capacity *= ENTRY_BUDGET_IDR
            invested_value = sum(
                row["current_price"] * row["quantity"] for row in open_positions
            )
            return {
                "mode": "PAPER ONLY — NO LIVE ORDERS",
                "balances": {
                    "virtual_total_idr": cash + invested_value + MANUAL_RESERVE_IDR + locked,
                    "trade_cash_idr": cash,
                    "available_entry_capacity_idr": entry_capacity,
                    "manual_reserve_locked_idr": MANUAL_RESERVE_IDR,
                    "dca_reserve_locked_idr": DCA_RESERVE_PER_POSITION_IDR * POSITION_LIMIT,
                    "entry_budget_per_position_idr": ENTRY_BUDGET_IDR,
                    "locked_profit_idr": locked,
                    "locked_btc_idr": self._setting(connection, "locked_btc_idr"),
                    "locked_eth_idr": self._setting(connection, "locked_eth_idr"),
                    "realized_since_lock_idr": self._setting(
                        connection, "realized_since_lock_idr"
                    ),
                },
                "positions": [
                    self._position_dict(row, markets_by_pair.get(row["pair_id"]))
                    for row in position_rows
                ],
                "events": [
                    {
                        "created_at": row["created_at"],
                        "event_type": row["event_type"],
                        "symbol": row["symbol"],
                        "detail": json.loads(row["detail"]),
                    }
                    for row in events
                ],
                "auto_paper_entry": bool(self._setting(connection, "auto_paper_entry")),
                "scanner_running": bool(self._setting(connection, "scanner_running")),
            }

    def simulate_dca(
        self, position_id: str, dca_amount: float, dca_price: float
    ) -> dict[str, float]:
        if dca_amount <= 0 or dca_price <= 0:
            raise ValueError("Nominal DCA dan harga harus lebih besar dari nol.")
        if dca_amount > DCA_RESERVE_PER_POSITION_IDR:
            raise ValueError("Nominal melebihi cadangan DCA maksimum per posisi.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM positions WHERE id=? AND status='OPEN'", (position_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Pilih posisi paper yang masih terbuka.")
        existing_cost = row["cost_idr"]
        new_quantity = dca_amount / (1 + TAKER_FEE_RATE) / dca_price
        fee = dca_amount - (new_quantity * dca_price)
        total_cost = existing_cost + dca_amount
        total_quantity = row["quantity"] + new_quantity
        average = total_cost / total_quantity if total_quantity else 0
        break_even = average / (1 - TAKER_FEE_RATE)
        return {
            "simulated_dca_amount_idr": dca_amount,
            "simulated_dca_fee_idr": fee,
            "new_average_price": average,
            "estimated_total_cost_idr": total_cost,
            "break_even_after_fees": break_even,
            "loss_if_price_falls_10_percent_idr": (
                total_quantity * break_even * 0.9 * (1 - TAKER_FEE_RATE)
            ) - total_cost,
            "dca_reserve_remaining_idr": DCA_RESERVE_PER_POSITION_IDR - dca_amount,
            "note": "SIMULASI SAJA — tidak mengubah posisi atau mengirim order.",
        }


def analyze_candles(candles: list[Candle]) -> dict[str, Any]:
    closes = [candle.close for candle in candles]
    if not candles:
        raise ValueError("Tidak ada candle untuk dianalisis.")

    def ema(period: int) -> float | None:
        if len(closes) < period:
            return None
        value = sum(closes[:period]) / period
        multiplier = 2 / (period + 1)
        for close in closes[period:]:
            value = (close - value) * multiplier + value
        return value

    def rsi(period: int = 14) -> float | None:
        if len(closes) <= period:
            return None
        changes = [current - previous for previous, current in zip(closes, closes[1:])]
        gains = [max(change, 0) for change in changes[:period]]
        losses = [max(-change, 0) for change in changes[:period]]
        average_gain = sum(gains) / period
        average_loss = sum(losses) / period
        for change in changes[period:]:
            average_gain = (average_gain * (period - 1) + max(change, 0)) / period
            average_loss = (
                average_loss * (period - 1) + max(-change, 0)
            ) / period
        if average_loss == 0:
            return 100.0 if average_gain else 50.0
        relative_strength = average_gain / average_loss
        return 100 - 100 / (1 + relative_strength)

    atr_percent: float | None = None
    if len(candles) >= 15:
        true_ranges: list[float] = []
        for previous, current in zip(candles[-15:-1], candles[-14:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        atr = sum(true_ranges) / len(true_ranges)
        atr_percent = atr / candles[-1].close * 100

    prior = candles[:-1][-20:]
    resistance = max((candle.high for candle in prior), default=None)
    support = min((candle.low for candle in prior), default=None)
    previous_volumes = [candle.volume for candle in candles[-21:-1]]
    median_volume = sorted(previous_volumes)[len(previous_volumes) // 2] if previous_volumes else 0
    relative_volume = (
        candles[-1].volume / median_volume if median_volume > 0 else None
    )
    previous_close = candles[-2].close if len(candles) >= 2 else None
    five_minute_close = candles[-6].close if len(candles) >= 6 else None
    close = candles[-1].close
    change_1m = (
        (close / previous_close - 1) * 100 if previous_close else None
    )
    change_5m = (
        (close / five_minute_close - 1) * 100 if five_minute_close else None
    )
    failed_breakout = bool(
        resistance is not None
        and candles[-1].high > resistance
        and close < resistance
    )
    room_to_resistance = (
        (resistance / close - 1) * 100
        if resistance is not None and close <= resistance
        else None
    )
    ema_9 = ema(9)
    ema_21 = ema(21)
    return {
        "candles": len(candles),
        "latest_timestamp": candles[-1].timestamp,
        "latest_close": close,
        "change_1m_percent": change_1m,
        "change_5m_percent": change_5m,
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_bullish": ema_9 > ema_21 if ema_9 is not None and ema_21 is not None else None,
        "rsi_14": rsi(),
        "atr_percent": atr_percent,
        "relative_volume": relative_volume,
        "support_20": support,
        "resistance_20": resistance,
        "room_to_resistance_percent": room_to_resistance,
        "failed_breakout": failed_breakout,
    }


class MarketScanner:
    def __init__(self) -> None:
        self._history: dict[str, deque[Observation]] = defaultdict(
            lambda: deque(maxlen=3000)
        )
        self._latest: dict[str, Market] = {}
        self._signals: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def analyze(self, markets: list[Market], timestamp: float | None = None) -> list[dict[str, Any]]:
        now = timestamp if timestamp is not None else time.time()
        signals: list[dict[str, Any]] = []
        with self._lock:
            for market in markets:
                if market.last <= 0:
                    continue
                history = self._history[market.pair_id]
                history.append(Observation(now, market.last, market.volume_idr_24h))
                while history and now - history[0].timestamp > 86_400:
                    history.popleft()
                self._latest[market.pair_id] = market

                def change(window_seconds: int) -> float | None:
                    old = next(
                        (item for item in reversed(history)
                         if now - item.timestamp >= window_seconds),
                        None,
                    )
                    if old is None or old.price <= 0:
                        return None
                    return (market.last / old.price - 1) * 100

                m1 = change(60)
                m5 = change(300)
                m15 = change(900)
                h1 = change(3600)
                h4 = change(14_400)
                d1 = change(86_400)
                score = 0.0
                if m1 is not None:
                    score += max(-2, min(2, m1 * 1.5))
                if m5 is not None:
                    score += max(-3, min(3, m5))
                if m15 is not None:
                    score += max(-2, min(2, m15 * 0.4))
                if market.volume_idr_24h >= 25_000_000:
                    score += 1
                if market.spread_percent is not None and market.spread_percent <= 0.5:
                    score += 0.5
                daily_range_percent = (
                    (market.high_24h / market.low_24h - 1) * 100
                    if market.low_24h > 0 and market.high_24h >= market.low_24h
                    else None
                )
                if daily_range_percent is not None:
                    score += max(0, min(2, daily_range_percent * 0.05))

                eligible = (
                    market.active
                    and market.asset not in EXCLUDED_ASSETS
                    and market.last > MIN_ENTRY_PRICE_IDR
                    and market.volume_idr_24h >= 10_000_000
                    and market.spread_percent is not None
                    and market.spread_percent <= 1.5
                )
                early_momentum = (
                    (m1 is not None and m1 >= 0.35)
                    or (m5 is not None and m5 >= 0.8)
                )
                if not eligible:
                    status = "FILTERED"
                elif not early_momentum:
                    status = "MONITORING"
                else:
                    status = "EARLY MOMENTUM"
                record = {
                    "pair_id": market.pair_id,
                    "symbol": market.symbol,
                    "asset": market.asset,
                    "price_idr": market.last,
                    "bid_idr": market.bid,
                    "ask_idr": market.ask,
                    "spread_percent": market.spread_percent,
                    "volume_idr_24h": market.volume_idr_24h,
                    "daily_range_percent": daily_range_percent,
                    "changes_percent": {
                        "1m": m1, "5m": m5, "15m": m15,
                        "1h": h1, "4h": h4, "1d": d1,
                    },
                    "score": round(score, 2),
                    "status": status,
                    "eligible": eligible,
                    "early_momentum": early_momentum,
                    "observations": len(history),
                    "technical": None,
                    "technical_error": None,
                    "technical_fresh": False,
                    "entry_ready": False,
                }
                signals.append(record)
                self._signals[market.pair_id] = record

            signals.sort(key=lambda item: (item["early_momentum"], item["score"]), reverse=True)
            return signals

    def attach_technicals(
        self,
        signals: list[dict[str, Any]],
        technical_by_pair: dict[str, dict[str, Any]],
        errors_by_pair: dict[str, str],
    ) -> None:
        with self._lock:
            for signal in signals:
                context = technical_by_pair.get(signal["pair_id"])
                if context is None:
                    if signal["pair_id"] in errors_by_pair:
                        signal["technical_error"] = errors_by_pair[signal["pair_id"]]
                        signal["status"] = "TECHNICAL DATA ERROR"
                    elif signal["eligible"]:
                        signal["early_momentum"] = False
                        signal["status"] = "WAITING TECHNICAL CHECK"
                    continue
                signal["technical"] = context
                one_minute = context["1m"]
                change_1m = one_minute["change_1m_percent"]
                change_5m = one_minute["change_5m_percent"]
                early_momentum = (
                    (change_1m is not None and change_1m >= 0.35)
                    or (change_5m is not None and change_5m >= 0.8)
                )
                structure = context["15m"]
                room = structure["room_to_resistance_percent"]
                blocked_by_resistance = (
                    room is not None and 0 <= room < 1.0
                )
                enough_data = (
                    one_minute["candles"] >= 22
                    and structure["candles"] >= 22
                    and context["1h"]["candles"] >= 22
                    and context["4h"]["candles"] >= 22
                    and context["1d"]["candles"] >= 22
                )
                now = time.time()
                freshness_limits = {
                    "1m": 180,
                    "15m": 1_800,
                    "1h": 7_200,
                    "4h": 28_800,
                    "1d": 172_800,
                }
                fresh_data = all(
                    0 <= now - context[timeframe]["latest_timestamp"] <= max_age
                    for timeframe, max_age in freshness_limits.items()
                )
                price_deviation = (
                    abs(signal["price_idr"] / one_minute["latest_close"] - 1) * 100
                    if one_minute["latest_close"] > 0
                    else float("inf")
                )
                price_data_coherent = price_deviation <= 1.5
                signal["technical_fresh"] = fresh_data and price_data_coherent
                signal["early_momentum"] = early_momentum
                signal["entry_ready"] = (
                    signal["eligible"]
                    and early_momentum
                    and enough_data
                    and fresh_data
                    and price_data_coherent
                    and not structure["failed_breakout"]
                    and not blocked_by_resistance
                )
                if signal["entry_ready"]:
                    signal["status"] = "EARLY MOMENTUM"
                elif not enough_data:
                    signal["status"] = "INSUFFICIENT OHLC HISTORY"
                elif not fresh_data or not price_data_coherent:
                    signal["status"] = "STALE OR INCONSISTENT DATA"
                elif structure["failed_breakout"]:
                    signal["status"] = "POSSIBLE FALSE BREAKOUT"
                elif blocked_by_resistance:
                    signal["status"] = "RESISTANCE TOO CLOSE"
                else:
                    signal["status"] = "MONITORING"
            signals.sort(
                key=lambda item: (item["entry_ready"], item["score"]),
                reverse=True,
            )

    def weak_pairs(self) -> set[str]:
        with self._lock:
            weak: set[str] = set()
            for pair_id, signal in self._signals.items():
                technical = signal["technical"]
                if technical is None or not signal["technical_fresh"]:
                    continue
                short = technical["1m"]["change_5m_percent"]
                minute = technical["1m"]["change_1m_percent"]
                structure = technical["15m"]
                sustained_weakness = (
                    short is not None
                    and short <= -0.5
                    and minute is not None
                    and minute <= -0.2
                )
                failed_breakout = (
                    structure["failed_breakout"]
                    and short is not None
                    and short < 0
                )
                trend_reversal = (
                    structure["ema_bullish"] is False
                    and short is not None
                    and short < 0
                    and minute is not None
                    and minute < 0
                )
                if sustained_weakness or failed_breakout or trend_reversal:
                    weak.add(pair_id)
            return weak


class BotService:
    def __init__(
        self,
        client: IndodaxPublicClient | None = None,
        portfolio: PaperPortfolio | None = None,
        scanner: MarketScanner | None = None,
    ) -> None:
        self.client = client or IndodaxPublicClient()
        self.portfolio = portfolio or PaperPortfolio()
        self.scanner = scanner or MarketScanner()
        self._lock = threading.RLock()
        self._markets: dict[str, Market] = {}
        self._signals: list[dict[str, Any]] = []
        self._error: str | None = None
        self._last_scan: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.portfolio.set_setting("scanner_running", 0)

    def scan_once(self) -> list[dict[str, Any]]:
        markets = self.client.fetch_markets()
        signals = self.scanner.analyze(markets)
        market_map = {market.pair_id: market for market in markets}
        open_pairs = self.portfolio.open_pair_ids()
        candidate_by_pair = {
            signal["pair_id"]: signal
            for signal in signals
            if signal["pair_id"] in open_pairs
        }
        for signal in sorted(
            (signal for signal in signals if signal["eligible"]),
            key=lambda signal: (
                signal["score"],
                signal["volume_idr_24h"],
            ),
            reverse=True,
        )[:6]:
            candidate_by_pair.setdefault(signal["pair_id"], signal)
        candidates = list(candidate_by_pair.values())
        technical_by_pair: dict[str, dict[str, Any]] = {}
        errors_by_pair: dict[str, str] = {}
        timeframe_map = {
            "1m": "1",
            "15m": "15",
            "1h": "60",
            "4h": "240",
            "1d": "1D",
        }
        for signal in candidates:
            try:
                candles = {
                    label: self.client.fetch_candles(
                        signal["pair_id"], timeframe, limit=100
                    )
                    for label, timeframe in timeframe_map.items()
                }
                technical_by_pair[signal["pair_id"]] = {
                    timeframe: analyze_candles(values)
                    for timeframe, values in candles.items()
                }
            except (RuntimeError, ValueError) as exc:
                LOGGER.warning(
                    "Technical data unavailable for %s: %s",
                    signal["symbol"],
                    exc,
                )
                errors_by_pair[signal["pair_id"]] = str(exc)
        self.scanner.attach_technicals(signals, technical_by_pair, errors_by_pair)
        self.portfolio.update_positions(market_map, self.scanner.weak_pairs())
        if self.portfolio.get_setting("auto_paper_entry"):
            for signal in signals:
                if not signal["entry_ready"]:
                    continue
                market = market_map[signal["pair_id"]]
                try:
                    self.portfolio.open_position(market, signal["score"])
                except ValueError as exc:
                    LOGGER.info("Paper entry skipped for %s: %s", market.symbol, exc)
                    continue
                break
        with self._lock:
            self._markets = market_map
            self._signals = signals
            self._error = None
            self._last_scan = now_iso()
        return signals

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:
                LOGGER.exception("Market scan failed")
                with self._lock:
                    self._error = str(exc)
            self._stop.wait(SCAN_INTERVAL_SECONDS)
        self.portfolio.set_setting("scanner_running", 0)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.portfolio.set_setting("scanner_running", 1)
            self._thread = threading.Thread(
                target=self._run, name="indodax-paper-scanner", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.portfolio.set_setting("scanner_running", 0)

    def state(self) -> dict[str, Any]:
        with self._lock:
            market_map = dict(self._markets)
            signals = list(self._signals)
            error = self._error
            last_scan = self._last_scan
        portfolio = self.portfolio.state(market_map)
        positions = [position for position in portfolio["positions"] if position["status"] != "CLOSED"]
        return {
            **portfolio,
            "positions": positions,
            "markets_scanned": len(market_map),
            "signals": signals[:40],
            "last_scan": last_scan,
            "error": error,
            "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        }

    def dca_simulation(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.portfolio.simulate_dca(
            str(data.get("position_id", "")),
            number(data.get("amount_idr")),
            number(data.get("price_idr")),
        )

    def private_api_preflight(self) -> dict[str, Any]:
        credentials = IndodaxCredentials.from_environment()
        client = IndodaxTradeApiV2(credentials)
        account = client.account()
        orders = client.open_orders()
        balances = account.get("balances")
        if not isinstance(balances, list):
            raise RuntimeError("Indodax account response has no valid balances list.")
        idr_balance = next(
            (
                item for item in balances
                if isinstance(item, dict) and str(item.get("asset", "")).upper() == "IDR"
            ),
            {},
        )
        safe_permissions = (
            account.get("canTrade") is True
            and account.get("canWithdraw") is False
            and account.get("canWithdrawFiat") is False
        )
        safe_orders = [
            {
                key: item.get(key)
                for key in (
                    "symbol", "orderId", "clientOrderId", "side",
                    "origQty", "executedQty", "status", "type",
                )
            }
            for item in orders
        ]
        return {
            "connected": True,
            "trade_permission": account.get("canTrade") is True,
            "crypto_withdrawals_disabled": account.get("canWithdraw") is False,
            "idr_withdrawals_disabled": account.get("canWithdrawFiat") is False,
            "permissions_safe_for_trading": safe_permissions,
            "live_order_actions_enabled": credentials.live_trading_enabled,
            "idr_available": number(idr_balance.get("free")),
            "idr_locked": number(idr_balance.get("locked")),
            "open_order_count": len(safe_orders),
            "open_orders": safe_orders,
            "message": (
                "Read-only preflight berhasil. Bot tetap tidak mengirim order dari dashboard."
                if safe_permissions
                else "Koneksi berhasil, tetapi izin API tidak sesuai prinsip least privilege."
            ),
        }

    def manual_paper_sell(self, position_id: str) -> None:
        with self._lock:
            market = next(
                (item for item in self._markets.values()
                 if item.pair_id == self._position_pair(position_id)),
                None,
            )
        if market is None:
            raise ValueError("Harga pasar terbaru tidak tersedia.")
        self.portfolio.manual_paper_sell(position_id, market.last)

    def _position_pair(self, position_id: str) -> str | None:
        with self.portfolio._connect() as connection:
            row = connection.execute(
                "SELECT pair_id FROM positions WHERE id=?", (position_id,)
            ).fetchone()
        return row["pair_id"] if row else None


SERVICE = BotService()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "IndodaxPaperDashboard/1.0"
    sys_version = ""

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            try:
                self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            except OSError as exc:
                self._json(500, {"error": f"Dashboard file unavailable: {exc}"})
            return
        if parsed.path == "/api/state":
            self._json(200, SERVICE.state())
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            if host != BOT_ALLOWED_HOST or origin != f"http://{BOT_ALLOWED_HOST}":
                self._json(403, {"error": "Permintaan lintas-origin ditolak."})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                self._json(413, {"error": "Request too large"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Request body harus berupa object JSON.")
            if parsed.path == "/api/scanner/start":
                SERVICE.start()
                self._json(200, {"ok": True, "message": "Scanner dimulai."})
                return
            if parsed.path == "/api/scanner/stop":
                SERVICE.stop()
                self._json(200, {"ok": True, "message": "Scanner dihentikan."})
                return
            if parsed.path == "/api/paper/auto-entry":
                enabled = bool(data.get("enabled"))
                SERVICE.portfolio.set_setting("auto_paper_entry", int(enabled))
                self._json(200, {"ok": True, "enabled": enabled})
                return
            if parsed.path == "/api/dca/simulate":
                self._json(200, SERVICE.dca_simulation(data))
                return
            if parsed.path == "/api/exchange/preflight":
                if not is_loopback_client(self.client_address[0]):
                    self._json(
                        403,
                        {"error": "Private API preflight hanya tersedia dari mesin lokal."},
                    )
                    return
                self._json(200, SERVICE.private_api_preflight())
                return
            prefix = "/api/paper/positions/"
            suffix = "/sell"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                position_id = unquote(parsed.path[len(prefix):-len(suffix)])
                SERVICE.manual_paper_sell(position_id)
                self._json(200, {"ok": True, "message": "Penjualan paper dicatat."})
                return
            self._json(404, {"error": "Not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except IndodaxApiError as exc:
            self._json(503, {"error": str(exc)})
        except Exception as exc:
            LOGGER.exception("Dashboard API request failed")
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("Dashboard %s - %s", self.address_string(), fmt % args)


def main() -> None:
    if not BOT_ALLOWED_HOST:
        raise RuntimeError("BOT_ALLOWED_HOST must be set to the expected host and port.")
    if not 1 <= BOT_PORT <= 65_535:
        raise RuntimeError("BOT_PORT must be between 1 and 65535.")
    server = ThreadingHTTPServer((BOT_HOST, BOT_PORT), DashboardHandler)
    LOGGER.info(
        "Dashboard ready at http://%s:%s (paper only; allowed Host: %s)",
        BOT_HOST,
        BOT_PORT,
        BOT_ALLOWED_HOST,
    )
    if BOT_HOST not in {"127.0.0.1", "::1", "localhost"}:
        LOGGER.warning(
            "Direct HTTP is unencrypted. Use only temporarily for paper mode; "
            "private API preflight is blocked for remote clients."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down dashboard")
    finally:
        SERVICE.stop()
        server.server_close()


if __name__ == "__main__":
    main()
