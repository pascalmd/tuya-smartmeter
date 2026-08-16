"""Messwert-Historie in SQLite (/config/history.db).

Der Poller schreibt hier laufend rein, auch wenn niemand die Weboberflaeche
offen hat. Die UI liest nur aus diesem Speicher, ist damit schnell und
ueberbrueckt kurze Aussetzer der Tuya-Cloud.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

DB_FILE = CONFIG_DIR / "history.db"

# Nur Messwerte behalten, die sich zum Aufzeichnen lohnen. Alles andere
# (Schalterstellungen, Textfelder) steht ohnehin im Live-Status.
NUMERIC_ONLY = True
RETENTION_DAYS = 90

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL statt FULL: spart pro Schreibvorgang ein fsync. Bei einem
    # Stromausfall koennen die letzten Sekunden fehlen - fuer Messwerte
    # verschmerzbar, fuer die Lebensdauer einer SD-Karte deutlich spuerbar.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts     INTEGER NOT NULL,
                code   TEXT    NOT NULL,
                value  REAL    NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_code_ts ON samples (code, ts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                ts      INTEGER NOT NULL,
                kind    TEXT    NOT NULL,
                message TEXT    NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)")


def record(metrics: list[dict[str, Any]], phases: list[dict[str, Any]]) -> None:
    """Einen Poll-Durchlauf ablegen."""
    ts = int(time.time())
    rows: list[tuple[int, str, float]] = []

    for metric in metrics:
        value = metric.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        rows.append((ts, metric["code"], float(value)))

    for phase in phases:
        for suffix in ("voltage_v", "current_a", "power_w"):
            rows.append((ts, f"{phase['code']}_{suffix}", float(phase[suffix])))

    if not rows:
        return
    with _lock, _connect() as conn:
        conn.executemany("INSERT INTO samples (ts, code, value) VALUES (?, ?, ?)", rows)


def log_event(kind: str, message: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO events (ts, kind, message) VALUES (?, ?, ?)",
            (int(time.time()), kind, message[:500]),
        )


def series(code: str, hours: int = 24, max_points: int = 500) -> list[dict[str, float]]:
    since = int(time.time()) - hours * 3600
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ts, value FROM samples WHERE code = ? AND ts >= ? ORDER BY ts",
            (code, since),
        ).fetchall()
    if len(rows) <= max_points:
        return [{"ts": r[0], "value": r[1]} for r in rows]
    step = len(rows) / max_points
    return [{"ts": rows[int(i * step)][0], "value": rows[int(i * step)][1]} for i in range(max_points)]


def recorded_codes(hours: int = 24) -> list[str]:
    since = int(time.time()) - hours * 3600
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT code FROM samples WHERE ts >= ? ORDER BY code", (since,)
        ).fetchall()
    return [r[0] for r in rows]


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ts, kind, message FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"ts": r[0], "kind": r[1], "message": r[2]} for r in rows]


def prune() -> int:
    """Alte Messwerte wegwerfen, damit die Datei nicht unbegrenzt waechst."""
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with _lock, _connect() as conn:
        deleted = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    return max(deleted, 0)
