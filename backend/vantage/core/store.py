"""SQLite persistence.

* WAL mode; one dedicated writer thread consumes a queue so writes never block the event loop.
* Reads use a separate connection via ``asyncio.to_thread`` (WAL allows concurrent readers).
* Every table carries ``mode`` so sim/paper/live never mix.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vantage.core.util import dumps, loads

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  mode TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (mode, key));
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, mode TEXT NOT NULL, t INTEGER NOT NULL, strategy_id TEXT, symbol TEXT,
  action TEXT, reason TEXT, edge_score REAL, llm TEXT, order_id TEXT, idea TEXT);
CREATE INDEX IF NOT EXISTS ix_decisions_t ON decisions(mode, t);
CREATE TABLE IF NOT EXISTS orders_meta (
  client_order_id TEXT PRIMARY KEY, mode TEXT NOT NULL, t INTEGER NOT NULL, broker_id TEXT,
  symbol TEXT, strategy_id TEXT, purpose TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS positions_meta (
  mode TEXT NOT NULL, symbol TEXT NOT NULL, strategy_id TEXT, entry_t INTEGER, data TEXT,
  PRIMARY KEY (mode, symbol));
CREATE TABLE IF NOT EXISTS journal (
  id TEXT PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT, underlying TEXT, strategy_id TEXT,
  asset_class TEXT, direction INTEGER, entry_t INTEGER, exit_t INTEGER, entry_price REAL,
  exit_price REAL, qty REAL, pnl REAL, pnl_pct REAL, reason_exit TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_journal_t ON journal(mode, entry_t);
CREATE TABLE IF NOT EXISTS news (
  id TEXT PRIMARY KEY, mode TEXT NOT NULL, t INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS ix_news_t ON news(mode, t);
CREATE TABLE IF NOT EXISTS signals_log (
  mode TEXT NOT NULL, t INTEGER NOT NULL, symbol TEXT NOT NULL, model TEXT NOT NULL,
  score REAL, confidence REAL, horizon_days REAL, price REAL);
CREATE INDEX IF NOT EXISTS ix_signals_log ON signals_log(mode, model, t);
CREATE TABLE IF NOT EXISTS iv_history (
  mode TEXT NOT NULL, symbol TEXT NOT NULL, d TEXT NOT NULL, atm_iv REAL, rv REAL,
  PRIMARY KEY (mode, symbol, d));
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, d TEXT NOT NULL, data TEXT);
CREATE TABLE IF NOT EXISTS llm_calls (
  id TEXT PRIMARY KEY, mode TEXT, t INTEGER, purpose TEXT, model TEXT, latency_ms INTEGER,
  ok INTEGER, request TEXT, response TEXT);
CREATE INDEX IF NOT EXISTS ix_llm_calls_t ON llm_calls(mode, t);
"""

MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "llm_calls": [("system", "TEXT"), ("error", "TEXT"), ("parsed", "TEXT"), ("cached", "INTEGER DEFAULT 0"),
                  ("prompt_tokens", "INTEGER"), ("completion_tokens", "INTEGER")],
}

_STOP = object()


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._q: queue.Queue[Any] = queue.Queue()
        self._writer = self._connect()
        self._writer.executescript(SCHEMA)
        self._migrate()
        self._writer.commit()
        self._reader = self._connect() if self.path != ":memory:" else self._writer
        self._read_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="store-writer", daemon=True)
        self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        """Additive column migrations (SQLite has no ADD COLUMN IF NOT EXISTS)."""
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self._writer.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    self._writer.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ---------------------------------------------------------------- writer thread
    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                break
            batch = [item]
            while len(batch) < 500:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    self._q.put(_STOP)
                    break
                batch.append(nxt)
            try:
                with self._read_lock:
                    self._writer.execute("BEGIN")
                    for sql, params, _done in batch:
                        try:
                            self._writer.execute(sql, params)
                        except sqlite3.Error:
                            log.exception("store write failed: %s", sql[:80])
                    self._writer.execute("COMMIT")
            except sqlite3.Error:
                log.exception("store batch failed")
            for _, _, done in batch:
                if done is not None:
                    done.set()

    def write(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Fire-and-forget write (queued to the writer thread)."""
        self._q.put((sql, tuple(params), None))

    def write_sync(self, sql: str, params: Sequence[Any] = ()) -> None:
        ev = threading.Event()
        self._q.put((sql, tuple(params), ev))
        ev.wait(10)

    async def awrite(self, sql: str, params: Sequence[Any] = ()) -> None:
        await asyncio.to_thread(self.write_sync, sql, params)

    def flush(self) -> None:
        self.write_sync("SELECT 1")

    def close(self) -> None:
        self.flush()
        self._q.put(_STOP)
        self._thread.join(5)
        self._writer.close()
        if self._reader is not self._writer:
            self._reader.close()

    # ---------------------------------------------------------------- reads
    def query_sync(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._read_lock:
            rows = self._reader.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.query_sync, sql, params)

    # ---------------------------------------------------------------- kv helpers
    def kv_get(self, mode: str, key: str, default: Any = None) -> Any:
        rows = self.query_sync("SELECT value FROM kv WHERE mode=? AND key=?", (mode, key))
        return loads(rows[0]["value"]) if rows else default

    def kv_set(self, mode: str, key: str, value: Any) -> None:
        self.write_sync(
            "INSERT INTO kv(mode,key,value) VALUES(?,?,?) ON CONFLICT(mode,key) DO UPDATE SET value=excluded.value",
            (mode, key, dumps(value).decode()),
        )
