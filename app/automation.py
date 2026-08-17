"""Preisgesteuerte Schaltlogik.

Entscheidet anhand der Strompreise, ob der Schaltausgang des Zaehlers an oder
aus sein soll. Die Entscheidung ist absichtlich zustandslos und wird bei jedem
Durchlauf neu getroffen - so ist sie nach einem Neustart sofort wieder richtig.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .tibber import LEVEL_LABELS, cheapest_hours, parse_ts

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "switch_code": "switch",
    "mode": "threshold",          # threshold | cheapest | level
    "threshold_ct": 25.0,         # ct/kWh, brutto (Endpreis, nicht Boersenpreis)
    "cheapest_hours": 6,          # Anzahl guenstigster Stunden pro Tag
    "block_window_hours": 24,     # Zeitfenster, in dem der Block liegen muss
    "levels": ["VERY_CHEAP", "CHEAP"],
    "min_off_minutes": 0,         # Schutz gegen Flattern
    "min_on_minutes": 0,          # Mindestlaufzeit fuer Verbraucher, die durchlaufen sollen
    "max_off_hours": 0,           # 0 = aus; sonst Zwangs-EIN nach X Stunden
    "override_minutes": 60,       # Pause der Automatik nach Handbedienung
}

MODE_LABELS = {
    "threshold": "Preisschwelle",
    "cheapest": "Günstigste Stunden",
    "cheapest_block": "Günstigster Block am Stück",
    "level": "Preisstufe",
}


def settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Gespeicherte Werte auf ein vollstaendiges, plausibles Set bringen."""
    merged = dict(DEFAULTS)
    merged.update(raw or {})
    merged["threshold_ct"] = float(merged.get("threshold_ct") or 0)
    merged["cheapest_hours"] = max(0, min(24, int(merged.get("cheapest_hours") or 0)))
    merged["block_window_hours"] = max(1, min(48, int(merged.get("block_window_hours") or 24)))
    merged["min_off_minutes"] = max(0, min(720, int(merged.get("min_off_minutes") or 0)))
    merged["min_on_minutes"] = max(0, min(1440, int(merged.get("min_on_minutes") or 0)))
    merged["max_off_hours"] = max(0, min(72, int(merged.get("max_off_hours") or 0)))
    merged["override_minutes"] = max(0, min(1440, int(merged.get("override_minutes") or 0)))
    if merged["mode"] not in MODE_LABELS:
        merged["mode"] = "threshold"
    levels = [lvl for lvl in (merged.get("levels") or []) if lvl in LEVEL_LABELS]
    merged["levels"] = levels or list(DEFAULTS["levels"])
    return merged


def cheapest_block(
    entries: list[dict[str, Any]],
    hours: int,
    now: dt.datetime,
    window_hours: int = 24,
) -> set[str]:
    """startsAt-Werte des guenstigsten zusammenhaengenden Blocks.

    Fuer Verbraucher, die am Stueck laufen sollen. Manche nehmen den Betrieb
    nach einer Unterbrechung nicht selbsttaetig wieder auf, und jedes Schalten
    unter Last kostet Relais-Lebensdauer. Die verstreute Auswahl der n billigsten
    Stunden ist geringfuegig guenstiger, erzeugt aber bis zu n Unterbrechungen;
    hier gibt es genau eine Ein- und eine Ausschaltung.

    Bloecke, die bereits komplett vorbei sind, scheiden aus. Loecken in der
    Preisreihe werden uebersprungen, damit kein Block ueber fehlende Stunden
    hinweg gebildet wird.

    `window_hours` begrenzt, wie weit voraus gesucht wird — und das ist keine
    Feinheit, sondern noetig: Ohne Grenze waehlt die Regel den guenstigsten
    Block der gesamten bekannten Reihe. Sind die Preise von morgen auch nur
    geringfuegig niedriger, wird heute gar nicht geschaltet und die Sache immer
    weiter aufgeschoben. Mit 24 Stunden heisst die Regel: "irgendwann am Stueck
    innerhalb des naechsten Tages, moeglichst guenstig".
    """
    if hours <= 0:
        return set()

    usable = sorted(
        (e for e in entries if e.get("total") is not None),
        key=lambda e: e["startsAt"],
    )
    if len(usable) < hours:
        return set()

    bestes: tuple[float, list[dict[str, Any]]] | None = None
    for i in range(len(usable) - hours + 1):
        fenster = usable[i : i + hours]

        try:
            zeiten = [parse_ts(e["startsAt"]) for e in fenster]
        except ValueError:
            continue

        # Nur lueckenlose Bloecke: jede Stunde muss auf die vorige folgen.
        if any(
            (zeiten[k + 1] - zeiten[k]) != dt.timedelta(hours=1)
            for k in range(len(zeiten) - 1)
        ):
            continue

        if zeiten[-1] + dt.timedelta(hours=1) <= now:
            continue  # Block liegt vollstaendig in der Vergangenheit
        if zeiten[0] > now + dt.timedelta(hours=window_hours):
            continue  # Block liegt jenseits des Zeitfensters

        summe = sum(e["total"] for e in fenster)
        if bestes is None or summe < bestes[0]:
            bestes = (summe, fenster)

    return {e["startsAt"] for e in bestes[1]} if bestes else set()


class Decision:
    def __init__(self, desired: bool | None, reason: str, price_ct: float | None = None):
        self.desired = desired          # None = keine Entscheidung moeglich
        self.reason = reason
        self.price_ct = price_ct
        self.block: set[str] = set()    # nur beim Blockmodus belegt

    def as_dict(self) -> dict[str, Any]:
        return {"desired": self.desired, "reason": self.reason, "price_ct": self.price_ct}


def block_gilt_noch(block: set[str], now: dt.datetime) -> bool:
    """Laeuft ein einmal gewaehlter Block noch?"""
    if not block:
        return False
    try:
        ende = max(parse_ts(s) for s in block) + dt.timedelta(hours=1)
    except ValueError:
        return False
    return ende > now


def decide(
    prices: dict[str, Any],
    cfg: dict[str, Any],
    now: dt.datetime,
    *,
    off_since: float | None = None,
    block: set[str] | None = None,
) -> Decision:
    """Soll-Zustand des Schalters bestimmen."""
    if not cfg.get("enabled"):
        return Decision(None, "Automatik ist aus")

    current = prices.get("current") or {}
    total = current.get("total")
    if total is None:
        return Decision(None, "Kein aktueller Strompreis verfuegbar")
    price_ct = round(total * 100, 2)

    # Sicherheitsnetz: nach zu langer Aus-Zeit unabhaengig vom Preis einschalten.
    max_off = cfg.get("max_off_hours") or 0
    if max_off and off_since:
        off_hours = (now.timestamp() - off_since) / 3600
        if off_hours >= max_off:
            return Decision(
                True,
                f"Sicherheitsnetz: seit {off_hours:.1f} h aus (Grenze {max_off} h)",
                price_ct,
            )

    mode = cfg["mode"]

    if mode == "threshold":
        limit = cfg["threshold_ct"]
        on = price_ct <= limit
        return Decision(
            on,
            f"{price_ct:.2f} ct/kWh {'≤' if on else '>'} Schwelle {limit:.2f} ct/kWh",
            price_ct,
        )

    if mode == "cheapest":
        count = cfg["cheapest_hours"]
        today = prices.get("today") or []
        tomorrow = prices.get("tomorrow") or []
        cheap = cheapest_hours(today, count)
        if tomorrow:
            cheap |= cheapest_hours(tomorrow, count)
        starts_at = current.get("startsAt")
        on = starts_at in cheap
        # Fallback, falls startsAt nicht exakt in der Liste steht: ueber die Stunde suchen.
        if not on and starts_at:
            try:
                current_hour = parse_ts(starts_at).replace(minute=0, second=0, microsecond=0)
                on = any(
                    parse_ts(s).replace(minute=0, second=0, microsecond=0) == current_hour
                    for s in cheap
                )
            except ValueError:
                pass
        return Decision(
            on,
            f"Diese Stunde gehoert {'zu' if on else 'nicht zu'} den {count} guenstigsten des Tages",
            price_ct,
        )

    if mode == "cheapest_block":
        count = cfg["cheapest_hours"]
        reihe = list(prices.get("today") or []) + list(prices.get("tomorrow") or [])

        # Einen laufenden Block nicht neu verhandeln. Ohne dieses Gedaechtnis
        # waehlt die Regel bei jedem Durchlauf neu — und schiebt die Einschaltung
        # immer weiter auf, solange der naechste Tag noch etwas guenstiger ist.
        if not block_gilt_noch(block or set(), now):
            block = cheapest_block(reihe, count, now, cfg["block_window_hours"])

        starts_at = current.get("startsAt")
        on = starts_at in (block or set())
        if block:
            beginn = parse_ts(min(block))
            ende = parse_ts(max(block)) + dt.timedelta(hours=1)
            hinweis = f"Block {beginn:%d.%m. %H:%M}–{ende:%H:%M}"
        else:
            hinweis = "kein Block bestimmbar"
        entscheidung = Decision(
            on,
            f"Diese Stunde liegt {'im' if on else 'nicht im'} guenstigsten "
            f"{count}-Stunden-Block ({hinweis})",
            price_ct,
        )
        entscheidung.block = block or set()
        return entscheidung

    if mode == "level":
        level = current.get("level")
        on = level in cfg["levels"]
        wanted = ", ".join(LEVEL_LABELS.get(l, l) for l in cfg["levels"])
        return Decision(
            on,
            f"Aktuelle Stufe '{LEVEL_LABELS.get(level, level)}' "
            f"{'ist' if on else 'ist nicht'} in der Auswahl ({wanted})",
            price_ct,
        )

    return Decision(None, f"Unbekannter Modus '{mode}'", price_ct)


def schedule_preview(
    prices: dict[str, Any], cfg: dict[str, Any], hours: int = 24
) -> list[dict[str, Any]]:
    """Vorschau: waere der Schalter in den kommenden Stunden an oder aus?

    Rein informativ fuer die Oberflaeche - dieselbe Regel, nur auf die
    Zukunftsstunden angewandt.
    """
    cfg = settings(cfg)
    entries = list(prices.get("today") or []) + list(prices.get("tomorrow") or [])
    if not entries:
        return []

    now = dt.datetime.now(dt.timezone.utc)
    cheap: set[str] = set()
    if cfg["mode"] == "cheapest":
        cheap = cheapest_hours(prices.get("today") or [], cfg["cheapest_hours"])
        cheap |= cheapest_hours(prices.get("tomorrow") or [], cfg["cheapest_hours"])
    elif cfg["mode"] == "cheapest_block":
        cheap = cheapest_block(entries, cfg["cheapest_hours"], now, cfg["block_window_hours"])

    out: list[dict[str, Any]] = []
    for entry in entries:
        try:
            starts = parse_ts(entry["startsAt"])
        except (KeyError, ValueError):
            continue
        if starts + dt.timedelta(hours=1) <= now:
            continue
        total = entry.get("total")
        if total is None:
            continue
        price_ct = round(total * 100, 2)

        if cfg["mode"] == "threshold":
            on = price_ct <= cfg["threshold_ct"]
        elif cfg["mode"] in ("cheapest", "cheapest_block"):
            on = entry["startsAt"] in cheap
        else:
            on = entry.get("level") in cfg["levels"]

        out.append(
            {
                "hour": starts.strftime("%H:%M"),
                "day": starts.strftime("%d.%m."),
                "ct": price_ct,
                "level": entry.get("level"),
                "level_label": LEVEL_LABELS.get(entry.get("level", ""), ""),
                "on": on,
            }
        )
        if len(out) >= hours:
            break
    return out
