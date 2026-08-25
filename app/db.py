"""Run history, stored in SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   TEXT    NOT NULL,
    profile_name TEXT    NOT NULL,
    kind         TEXT    NOT NULL,   -- analysis | transfer
    trigger      TEXT    NOT NULL,   -- manual | scheduled
    status       TEXT    NOT NULL,   -- running | success | incomplete | failed | cancelled
    started_at   TEXT    NOT NULL,
    ended_at     TEXT,
    duration_s   REAL,
    moved        INTEGER DEFAULT 0,  -- server-side moves
    transferred  INTEGER DEFAULT 0,  -- files actually uploaded
    deleted      INTEGER DEFAULT 0,
    checks       INTEGER DEFAULT 0,
    bytes        INTEGER DEFAULT 0,  -- bytes that crossed the network
    errors       INTEGER DEFAULT 0,
    message      TEXT,
    details      TEXT                -- free-form JSON
);
CREATE INDEX IF NOT EXISTS idx_runs_profile ON runs(profile_id, id DESC);
"""

# Databases written before the translation to English used French codes.
# The rewrite is idempotent and cheap; it keeps existing histories readable
# without a version marker.
_MIGRATE_CODES = """
UPDATE runs SET kind = 'analysis'   WHERE kind = 'analyse';
UPDATE runs SET kind = 'transfer'   WHERE kind = 'synchro';
UPDATE runs SET trigger = 'manual'    WHERE trigger = 'manuel';
UPDATE runs SET trigger = 'scheduled' WHERE trigger = 'planifie';
UPDATE runs SET status = 'running'   WHERE status = 'en_cours';
UPDATE runs SET status = 'success'   WHERE status = 'reussi';
UPDATE runs SET status = 'failed'    WHERE status = 'echec';
UPDATE runs SET status = 'cancelled' WHERE status = 'annule';
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class History:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.executescript(_MIGRATE_CODES)
            self._conn.commit()

    def open_run(self, profile_id: str, profile_name: str, kind: str, trigger: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (profile_id, profile_name, kind, trigger, status, started_at)"
                " VALUES (?, ?, ?, ?, 'running', ?)",
                (profile_id, profile_name, kind, trigger, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close_run(
        self,
        run_id: int,
        status: str,
        stats: dict | None = None,
        message: str | None = None,
        details: dict | None = None,
    ) -> None:
        stats = stats or {}
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            duration = None
            if row:
                started = datetime.fromisoformat(row["started_at"])
                duration = round((datetime.now(timezone.utc) - started).total_seconds(), 1)

            self._conn.execute(
                "UPDATE runs SET status=?, ended_at=?, duration_s=?, moved=?, transferred=?,"
                " deleted=?, checks=?, bytes=?, errors=?, message=?, details=? WHERE id=?",
                (
                    status,
                    _now(),
                    duration,
                    int(stats.get("moved", 0)),
                    int(stats.get("transferred", 0)),
                    int(stats.get("deleted", 0)),
                    int(stats.get("checks", 0)),
                    int(stats.get("bytes", 0)),
                    int(stats.get("errors", 0)),
                    message,
                    json.dumps(details, ensure_ascii=False) if details else None,
                    run_id,
                ),
            )
            self._conn.commit()

    def recent(self, limit: int = 50, profile_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM runs"
        params: list = []
        if profile_id:
            query += " WHERE profile_id = ?"
            params.append(profile_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def last_success(self, profile_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE profile_id=? AND kind='transfer' AND status='success'"
                " ORDER BY id DESC LIMIT 1",
                (profile_id,),
            ).fetchone()
        return dict(row) if row else None

    def get(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def delete(self, run_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def clear(self, profile_id: str | None = None, keep_last: int = 0) -> int:
        """Empties the history. Returns the number of deleted rows.

        keep_last preserves the N most recent runs, so the table can be
        tidied without losing everything.
        """
        with self._lock:
            params: list = []
            where = ""
            if profile_id:
                where = " WHERE profile_id = ?"
                params.append(profile_id)

            if keep_last > 0:
                kept = self._conn.execute(
                    f"SELECT id FROM runs{where} ORDER BY id DESC LIMIT ?",
                    (*params, keep_last),
                ).fetchall()
                ids = [r["id"] for r in kept]
                slots = ",".join("?" * len(ids)) or "NULL"
                cur = self._conn.execute(
                    f"DELETE FROM runs{where}"
                    + (" AND" if where else " WHERE")
                    + f" id NOT IN ({slots})",
                    (*params, *ids),
                )
            else:
                cur = self._conn.execute(f"DELETE FROM runs{where}", params)

            self._conn.commit()
            return cur.rowcount

    def totals(self) -> dict:
        """All-time totals: bytes uploaded and files moved server-side."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes),0) AS bytes,"
                " COALESCE(SUM(moved),0) AS moved,"
                " COUNT(*) AS runs"
                " FROM runs WHERE kind='transfer' AND status='success'"
            ).fetchone()
        return dict(row)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
