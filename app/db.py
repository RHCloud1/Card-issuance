from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from flask import current_app, g


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = Path(current_app.config["DATABASE_PATH"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA foreign_keys = ON")
            # A second worker can reach a brand-new database while the first
            # worker is switching its journal mode. Retry only lock failures.
            for attempt in range(5):
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if not is_database_busy(exc) or attempt == 4:
                        raise
                    time.sleep(0.05 * (2**attempt))
        except Exception:
            conn.close()
            raise
        g.db = conn
    return g.db


def close_db(_: object | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    schema_path = Path(current_app.root_path).parent / "schema.sql"
    try:
        db.executescript(
            "BEGIN IMMEDIATE;\n"
            + schema_path.read_text(encoding="utf-8")
            + "\nCOMMIT;"
        )
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    migrate_db(db)


# 已有数据库升级：只做“缺列则补列”这类幂等变更。
COLUMN_MIGRATIONS = [
    ("orders", "crypto_amount", "INTEGER"),
    ("orders", "crypto_address", "TEXT NOT NULL DEFAULT ''"),
]


def migrate_db(db: sqlite3.Connection) -> None:
    try:
        db.execute("BEGIN IMMEDIATE")
        for table, column, definition in COLUMN_MIGRATIONS:
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_orders_crypto_amount
               ON orders(payment_provider, crypto_address, crypto_amount, status)"""
        )
        db.commit()
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def is_database_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    return code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def fetch_one(query: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_db().execute(query, params).fetchone()


def fetch_all(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(get_db().execute(query, params).fetchall())
