"""
storage.py

Lightweight SQLite-backed state store for social-mcp. Tracks which
messages have already been surfaced to the agent (to support future
dedup/"mark as read" behavior) and caches basic conversation metadata.

This is intentionally simple: a single table keyed by
(platform, message_id), plus a small key/value table for misc state.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_messages (
    platform    TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    sender_id   TEXT,
    chat_id     TEXT,
    seen_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (platform, message_id)
);

CREATE TABLE IF NOT EXISTS kv_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Storage:
    """Thread-safe wrapper around a single sqlite3 connection."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def mark_seen(self, platform: str, message_id: str, sender_id: Any = None, chat_id: Any = None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO seen_messages (platform, message_id, sender_id, chat_id) "
                "VALUES (?, ?, ?, ?)",
                (platform, message_id, str(sender_id) if sender_id is not None else None,
                 str(chat_id) if chat_id is not None else None),
            )

    def is_seen(self, platform: str, message_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM seen_messages WHERE platform = ? AND message_id = ?",
                (platform, message_id),
            )
            return cur.fetchone() is not None

    def set_kv(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO kv_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM kv_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else default

    def close(self) -> None:
        with self._lock:
            self._conn.close()
