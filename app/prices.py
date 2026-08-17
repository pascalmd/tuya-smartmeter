"""Strompreise aus verschiedenen Quellen, in einem einheitlichen Format.

Alle Quellen liefern dieselbe Struktur, damit die Schaltlogik nichts von der
Herkunft wissen muss:

    {"current": {"total": 0.28, "startsAt": "...", "level": "CHEAP"},
     "today":   [ {...}, ... ],
     "tomorrow":[ {...}, ... ]}

`total` ist immer der Preis in EUR/kWh, den der Nutzer als seinen Preis
betrachtet — bei Tibber der echte Endkundenpreis, bei den Boersenquellen der
Spotpreis zuzueglich des eingestellten Aufschlags.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from .tibber import TibberClient, TibberError

# Boersenquellen brauchen kein Konto und keinen Schluessel.
SOURCES: dict[str, dict[str, str]] = {
    "awattar_de": {
        "label": "aWATTar Deutschland (Börsenpreis, kein Konto nötig)",
        "kind": "spot",
        "url": "https://api.awattar.de/v1/marketdata",
        "note": "EPEX-Spot für Deutschland/Luxemburg, stündlich.",
    },
    "awattar_at": {
        "label": "aWATTar Österreich (Börsenpreis, kein Konto nötig)",
        "kind": "spot",
        "url": "https://api.awattar.at/v1/marketdata",
        "note": "EPEX-Spot für Österreich, stündlich.",
    },
    "energy_charts": {
        "label": "Energy-Charts / Fraunhofer ISE (Börsenpreis, kein Konto nötig)",
        "kind": "spot",
        "url": "https://api.energy-charts.info/price",
        "note": "Day-Ahead-Preise, Gebotszone DE-LU.",
    },
    "tibber": {
        "label": "Tibber (echter Endkundenpreis, Konto und Vertrag nötig)",
        "kind": "retail",
        "url": "",
        "note": "Liefert den Preis inklusive aller Abgaben.",
    },
}

DEFAULTS: dict[str, Any] = {
    "source": "awattar_de",
    "markup_ct": 20.0,      # Netzentgelte, Umlagen, Steuern, Marge - netto
    "vat_percent": 19.0,
}


class PriceError(RuntimeError):
    pass


def settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(raw or {})
    if merged["source"] not in SOURCES:
        merged["source"] = "awattar_de"
    merged["markup_ct"] = max(0.0, min(200.0, float(merged.get("markup_ct") or 0)))
    merged["vat_percent"] = max(0.0, min(100.0, float(merged.get("vat_percent") or 0)))
    return merged


def is_spot(source: str) -> bool:
    return SOURCES.get(source, {}).get("kind") == "spot"


# --------------------------------------------------------------- Preisstufen


def classify(entries: list[dict[str, Any]]) -> None:
    """Preisstufen nachbilden, wie Tibber sie liefert (in place).

    Tibber bezieht sich auf einen laengeren Mittelwert; hier genuegt der
    Tagesdurchschnitt - fuer "ist diese Stunde teuer oder billig" reicht das.
    """
    werte = [e["total"] for e in entries if e.get("total") is not None]
    if not werte:
        return
    schnitt = sum(werte) / len(werte)
    if schnitt <= 0:
        return
    for entry in entries:
        total = entry.get("total")
        if total is None:
            continue
        verhaeltnis = total / schnitt
        if verhaeltnis < 0.60:
            entry["level"] = "VERY_CHEAP"
        elif verhaeltnis < 0.90:
            entry["level"] = "CHEAP"
        elif verhaeltnis < 1.15:
            entry["level"] = "NORMAL"
        elif verhaeltnis < 1.40:
            entry["level"] = "EXPENSIVE"
        else:
            entry["level"] = "VERY_EXPENSIVE"


def apply_markup(spot_eur_kwh: float, cfg: dict[str, Any]) -> float:
    """Boersenpreis auf einen realistischen Endpreis hochrechnen."""
    netto_ct = spot_eur_kwh * 100 + cfg["markup_ct"]
    brutto_ct = netto_ct * (1 + cfg["vat_percent"] / 100)
    return round(brutto_ct / 100, 6)


# ------------------------------------------------------------------- Abrufe


def to_hourly(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Feinere Aufloesungen auf volle Stunden mitteln.

    Die Gebotszone DE-LU wird inzwischen viertelstuendlich abgerechnet, und
    Energy-Charts gibt das auch so heraus. Ohne diesen Schritt zaehlt die Regel
    "die n guenstigsten Stunden" Viertelstunden und schaltet ein Viertel der
    beabsichtigten Zeit.
    """
    eimer: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        beginn = dt.datetime.fromisoformat(entry["startsAt"]).replace(
            minute=0, second=0, microsecond=0
        )
        eimer.setdefault(beginn.isoformat(), []).append(entry)

    stunden = []
    for startsAt, gruppe in sorted(eimer.items()):
        totals = [e["total"] for e in gruppe if e.get("total") is not None]
        spots = [e["spot"] for e in gruppe if e.get("spot") is not None]
        if not totals:
            continue
        stunden.append(
            {
                "startsAt": startsAt,
                "total": round(sum(totals) / len(totals), 6),
                "spot": round(sum(spots) / len(spots), 6) if spots else None,
                "level": None,
                "slots": len(gruppe),
            }
        )
    return stunden


def _split_days(entries: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    entries = to_hourly(entries)
    """Eine durchgehende Stundenreihe in heute / morgen / aktuell zerlegen."""
    heute = now.date()
    morgen = heute + dt.timedelta(days=1)

    today: list[dict[str, Any]] = []
    tomorrow: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    for entry in entries:
        beginn = dt.datetime.fromisoformat(entry["startsAt"])
        tag = beginn.astimezone(now.tzinfo).date()
        if tag == heute:
            today.append(entry)
        elif tag == morgen:
            tomorrow.append(entry)
        if beginn <= now < beginn + dt.timedelta(hours=1):
            current = entry

    classify(today)
    classify(tomorrow)

    # current stammt aus einer der beiden Listen und hat die Stufe schon.
    if current:
        for liste in (today, tomorrow):
            for entry in liste:
                if entry["startsAt"] == current["startsAt"]:
                    current = entry
                    break

    return {"current": current, "today": today, "tomorrow": tomorrow}


async def _fetch_awattar(url: str, cfg: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    start = int((now - dt.timedelta(days=1)).timestamp() * 1000)
    end = int((now + dt.timedelta(days=2)).timestamp() * 1000)
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(url, params={"start": start, "end": end})
    resp.raise_for_status()
    data = resp.json()

    entries = []
    for row in data.get("data", []):
        beginn = dt.datetime.fromtimestamp(row["start_timestamp"] / 1000, tz=dt.timezone.utc)
        spot = float(row["marketprice"]) / 1000  # Eur/MWh -> Eur/kWh
        entries.append(
            {
                "startsAt": beginn.astimezone(now.tzinfo).isoformat(),
                "total": apply_markup(spot, cfg),
                "spot": round(spot, 6),
                "level": None,
            }
        )
    if not entries:
        raise PriceError("aWATTar hat keine Preise geliefert")
    return _split_days(entries, now)


async def _fetch_energy_charts(cfg: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(SOURCES["energy_charts"]["url"], params={"bzn": "DE-LU"})
    resp.raise_for_status()
    data = resp.json()

    sekunden = data.get("unix_seconds") or []
    preise = data.get("price") or []
    if not sekunden or len(sekunden) != len(preise):
        raise PriceError("Energy-Charts hat keine verwertbaren Preise geliefert")

    entries = []
    for sek, preis in zip(sekunden, preise):
        if preis is None:
            continue
        beginn = dt.datetime.fromtimestamp(sek, tz=dt.timezone.utc)
        spot = float(preis) / 1000  # EUR/MWh -> EUR/kWh
        entries.append(
            {
                "startsAt": beginn.astimezone(now.tzinfo).isoformat(),
                "total": apply_markup(spot, cfg),
                "spot": round(spot, 6),
                "level": None,
            }
        )
    if not entries:
        raise PriceError("Energy-Charts hat keine Preise geliefert")
    return _split_days(entries, now)


async def fetch(price_cfg: dict[str, Any], tibber_cfg: dict[str, Any]) -> dict[str, Any]:
    """Preise der eingestellten Quelle holen."""
    cfg = settings(price_cfg)
    quelle = cfg["source"]
    now = dt.datetime.now(dt.timezone.utc).astimezone()

    if quelle == "tibber":
        token = (tibber_cfg or {}).get("token", "")
        home = (tibber_cfg or {}).get("home_id", "")
        if not (token and home):
            raise PriceError("Tibber ist als Quelle gewählt, aber Token oder Zuhause fehlt")
        try:
            daten = await TibberClient(token=token).prices(home)
        except TibberError as exc:
            raise PriceError(str(exc)) from exc

        # Tibber ist in mehreren Laendern taetig und liefert die Waehrung mit.
        # Ohne diese Pruefung wuerden schwedische Kronen als Cent angezeigt —
        # aufgefallen am oeffentlichen Demo-Zugang, der ein Haus in Schweden
        # zeigt: 1,506 SEK erschienen als "150,60 ct/kWh".
        daten["currency"] = (daten.get("current") or {}).get("currency") or "EUR"
        return daten

    if quelle in ("awattar_de", "awattar_at"):
        daten = await _fetch_awattar(SOURCES[quelle]["url"], cfg, now)
        daten["currency"] = "EUR"
        return daten

    if quelle == "energy_charts":
        daten = await _fetch_energy_charts(cfg, now)
        daten["currency"] = "EUR"
        return daten

    raise PriceError(f"Unbekannte Preisquelle '{quelle}'")
