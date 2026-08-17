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


def init(erstes_geraet: str = "") -> None:
    """Tabellen anlegen und aeltere Datenbanken nachziehen.

    Bis Version 1.4 gab es genau ein Geraet, also brauchte keine Zeile eine
    Kennung. Die Spalte kommt jetzt dazu; die vorhandenen Messwerte gehoeren
    dem Geraet, das es damals gab -- alles andere waere eine Messreihe ohne
    Herkunft.
    """
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

        for tabelle in ("samples", "events"):
            spalten = {r[1] for r in conn.execute(f"PRAGMA table_info({tabelle})")}
            if "device" not in spalten:
                conn.execute(f"ALTER TABLE {tabelle} ADD COLUMN device TEXT NOT NULL DEFAULT ''")
                if erstes_geraet:
                    conn.execute(
                        f"UPDATE {tabelle} SET device = ? WHERE device = ''", (erstes_geraet,)
                    )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_dev_code_ts ON samples (device, code, ts)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_dev_ts ON events (device, ts)")


def _mit_tabellen(aktion):
    """Aktion ausfuehren und die Tabellen anlegen, falls sie fehlen.

    Im Betrieb legt sie der Start an. Fehlen sie doch — weil die Datei geloescht
    wurde oder der Aufruf von woanders kommt —, ist es besser, sie still
    nachzuziehen, als jeden Schreibvorgang scheitern zu lassen.
    """
    try:
        return aktion()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        init()
        return aktion()


def record(metrics: list[dict[str, Any]], phases: list[dict[str, Any]],
           device: str = "") -> None:
    """Einen Poll-Durchlauf ablegen."""
    ts = int(time.time())
    rows: list[tuple[int, str, float, str]] = []

    for metric in metrics:
        value = metric.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        rows.append((ts, metric["code"], float(value), device))

    for phase in phases:
        for suffix in ("voltage_v", "current_a", "power_w"):
            rows.append((ts, f"{phase['code']}_{suffix}", float(phase[suffix]), device))

    if not rows:
        return

    def schreiben():
        with _lock, _connect() as conn:
            conn.executemany(
                "INSERT INTO samples (ts, code, value, device) VALUES (?, ?, ?, ?)", rows
            )

    _mit_tabellen(schreiben)


def log_event(kind: str, message: str, device: str = "") -> None:
    def schreiben():
        with _lock, _connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, message, device) VALUES (?, ?, ?, ?)",
                (int(time.time()), kind, message[:500], device),
            )

    _mit_tabellen(schreiben)


def series(code: str, hours: int = 24, max_points: int = 500,
           device: str | None = None) -> list[dict[str, float]]:
    since = int(time.time()) - hours * 3600
    bedingung = "code = ? AND ts >= ?"
    werte: list[Any] = [code, since]
    if device is not None:
        bedingung += " AND device = ?"
        werte.append(device)
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"SELECT ts, value FROM samples WHERE {bedingung} ORDER BY ts", werte
        ).fetchall()
    if len(rows) <= max_points:
        return [{"ts": r[0], "value": r[1]} for r in rows]
    step = len(rows) / max_points
    return [{"ts": rows[int(i * step)][0], "value": rows[int(i * step)][1]} for i in range(max_points)]


def recorded_codes(hours: int = 24, device: str | None = None) -> list[str]:
    since = int(time.time()) - hours * 3600
    bedingung = "ts >= ?"
    werte: list[Any] = [since]
    if device is not None:
        bedingung += " AND device = ?"
        werte.append(device)
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT code FROM samples WHERE {bedingung} ORDER BY code", werte
        ).fetchall()
    return [r[0] for r in rows]


def recent_events(limit: int = 50, device: str | None = None) -> list[dict[str, Any]]:
    """Ereignisse, wahlweise nur die eines Geraets.

    Ereignisse ohne Kennung stammen aus der Zeit vor der Geraeteliste oder
    betreffen die App als Ganzes (Preisabruf, Testzeitraum) -- die bleiben in
    jeder Ansicht sichtbar, sonst verschwaende die Filterung genau die
    Meldungen, die man sucht.
    """
    bedingung = ""
    werte: list[Any] = []
    if device is not None:
        bedingung = "WHERE device = ? OR device = ''"
        werte.append(device)
    werte.append(limit)
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"SELECT ts, kind, message, device FROM events {bedingung} ORDER BY ts DESC LIMIT ?",
            werte,
        ).fetchall()
    return [{"ts": r[0], "kind": r[1], "message": r[2], "device": r[3]} for r in rows]


def prune() -> int:
    """Alte Messwerte wegwerfen, damit die Datei nicht unbegrenzt waechst."""
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with _lock, _connect() as conn:
        deleted = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    return max(deleted, 0)
