"""SQLite persistence layer: schema, migrations, backups and DAO helpers.

The database file is the durable state of the agent. It is written back to the
repository by the scheduler so that state survives completely separate
processes (see .github/workflows/scan.yml).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from typing import Any, Iterable, Optional

from .util import utcnow_iso

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Migrations. Each entry is (version, [sql statements]). Never edit a shipped
# migration - append a new one instead.
# ---------------------------------------------------------------------------
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS challenge (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                start_utc TEXT,
                deadline_24h_utc TEXT,
                end_45h_utc TEXT,
                status TEXT DEFAULT 'PENDING',
                software_version TEXT,
                strategy_version TEXT,
                provider TEXT,
                config_version TEXT,
                initialized INTEGER DEFAULT 0,
                exploration_enabled INTEGER DEFAULT 0,
                exploration_orders INTEGER DEFAULT 0,
                first_trade_met INTEGER DEFAULT 0,
                first_trade_type TEXT,
                first_trade_at TEXT,
                deadline_alert INTEGER DEFAULT 0,
                hour12_done INTEGER DEFAULT 0,
                hour24_done INTEGER DEFAULT 0,
                hour45_done INTEGER DEFAULT 0,
                canary_done INTEGER DEFAULT 0,
                canary_result TEXT,
                final_report TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                starting_balance REAL NOT NULL,
                cash REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exec_type TEXT NOT NULL,
                strategy_version TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                current_price REAL,
                exit_price REAL,
                exit_time TEXT,
                realized_pnl REAL DEFAULT 0.0,
                settled INTEGER DEFAULT 0,
                updated_at TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                requested_price REAL,
                fill_price REAL,
                quantity REAL,
                notional REAL,
                fee REAL DEFAULT 0.0,
                slippage REAL DEFAULT 0.0,
                exec_type TEXT NOT NULL,
                status TEXT NOT NULL,
                strategy_version TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                order_id TEXT,
                account_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL,
                exit_price REAL,
                realized_pnl REAL DEFAULT 0.0,
                fees REAL DEFAULT 0.0,
                slippage REAL DEFAULT 0.0,
                status TEXT NOT NULL,
                exec_type TEXT NOT NULL,
                opened_at TEXT,
                closed_at TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT,
                ts TEXT NOT NULL,
                market_id TEXT,
                question TEXT,
                market_prob REAL,
                est_prob REAL,
                edge REAL,
                confidence REAL,
                decision TEXT,
                direction TEXT,
                features TEXT,
                data_ts TEXT,
                reason TEXT,
                risk_flags TEXT,
                stage_results TEXT,
                exec_type TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT,
                ts TEXT NOT NULL,
                market_id TEXT,
                question TEXT,
                price REAL,
                bid REAL,
                ask REAL,
                spread REAL,
                liquidity REAL,
                volume REAL,
                end_date TEXT,
                payload TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                provider TEXT,
                status TEXT,
                discovered INTEGER DEFAULT 0,
                eligible INTEGER DEFAULT 0,
                candidates INTEGER DEFAULT 0,
                analyzed INTEGER DEFAULT 0,
                signals INTEGER DEFAULT 0,
                rejected INTEGER DEFAULT 0,
                risk_approved INTEGER DEFAULT 0,
                executed INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0.0,
                error TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT,
                ts TEXT NOT NULL,
                market_id TEXT,
                stage TEXT,
                reason TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS provider_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                provider TEXT,
                ok INTEGER,
                latency_ms REAL,
                error TEXT,
                stale INTEGER DEFAULT 0,
                eligible_markets INTEGER DEFAULT 0
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                reason TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL DEFAULT 'RUNNING',
                last_success_utc TEXT,
                last_cycle_utc TEXT,
                last_api_utc TEXT,
                last_api_latency_ms REAL,
                last_scan_gap_minutes REAL,
                updated_at TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                account_id TEXT NOT NULL,
                cash REAL,
                equity REAL,
                exposure REAL,
                realized_pnl REAL,
                unrealized_pnl REAL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);",
            "CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id, account_id, status);",
            "CREATE INDEX IF NOT EXISTS idx_decisions_scan ON decisions(scan_id);",
            "CREATE INDEX IF NOT EXISTS idx_rejections_scan ON rejections(scan_id);",
        ],
    ),
]


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE;")
        self.conn.execute("PRAGMA foreign_keys=ON;")

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def current_version(self) -> int:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        if cur.fetchone() is None:
            return 0
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0)

    def backup(self, backup_dir: str, tag: str = "premigrate") -> Optional[str]:
        """Copy the database file before a schema change."""
        if not os.path.exists(self.path):
            return None
        os.makedirs(backup_dir, exist_ok=True)
        stamp = utcnow_iso().replace(":", "").replace("-", "")
        dest = os.path.join(backup_dir, f"state-{tag}-{stamp}.db")
        try:
            self.conn.commit()
        except sqlite3.Error:
            pass
        shutil.copy2(self.path, dest)
        return dest

    def migrate(self, backup_dir: Optional[str] = None) -> int:
        """Apply pending migrations, backing up first if the schema changes."""
        pending = [(v, stmts) for v, stmts in MIGRATIONS if v > self.current_version()]
        if pending and backup_dir and self.current_version() > 0:
            self.backup(backup_dir, tag="premigrate")
        for version, statements in pending:
            for stmt in statements:
                self.conn.execute(stmt)
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, utcnow_iso()),
            )
        self.conn.commit()
        return self.current_version()

    # -- generic helpers ----------------------------------------------------
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- settings -----------------------------------------------------------
    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )

    def get_setting(self, key: str, default=None):
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return default
