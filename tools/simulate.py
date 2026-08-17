#!/usr/bin/env python3
"""Trockenübung: Was täte die Automatik mit den echten Preisen von heute?

Laesst die tatsaechliche Entscheidungslogik (`automation.decide`) Stunde fuer
Stunde ueber einen Tag laufen und protokolliert jeden Schaltvorgang — mit den
Schutzregeln, die auch im Betrieb greifen: Mindestlaufzeit, Mindest-Aus-Zeit
und Sicherheitsnetz.

    python3 tools/simulate.py                 # alle Regeln vergleichen
    python3 tools/simulate.py --mode cheapest_block --hours 6 --min-on 240

Es wird nichts geschaltet und nichts veraendert; die Preise kommen live von der
eingestellten Quelle.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import automation, prices  # noqa: E402


def stunde_ts(eintrag: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(eintrag["startsAt"])


async def preise_holen(quelle: str, aufschlag: float, mwst: float) -> dict:
    cfg = {"source": quelle, "markup_ct": aufschlag, "vat_percent": mwst}
    return await prices.fetch(cfg, {})


def durchspielen(preisdaten: dict, cfg: dict, zeige_tabelle: bool) -> dict:
    """Einen Tag durchlaufen und die Schaltvorgaenge zaehlen.

    Bildet nach, was `apply_automation` im Betrieb tut: Der Soll-Zustand kommt
    aus `decide`, danach greifen Mindestlaufzeit und Mindest-Aus-Zeit.
    """
    reihe = sorted(
        [e for e in (preisdaten.get("today") or []) if e.get("total") is not None],
        key=lambda e: e["startsAt"],
    )
    if not reihe:
        return {"fehler": "keine Preise"}

    zustand = False
    block: set[str] = set()
    seit = stunde_ts(reihe[0])
    schaltvorgaenge: list[tuple[str, bool, str]] = []
    ein_stunden: list[dict] = []
    zeilen = []

    for eintrag in reihe:
        jetzt = stunde_ts(eintrag)
        # Der Automatik denselben Ausschnitt geben, den sie im Betrieb saehe
        sicht = {
            "current": eintrag,
            "today": reihe,
            "tomorrow": preisdaten.get("tomorrow") or [],
        }
        aus_seit = seit.timestamp() if not zustand else None
        entscheidung = automation.decide(sicht, cfg, jetzt, off_since=aus_seit, block=block)
        if entscheidung.block:
            block = entscheidung.block
        soll = entscheidung.desired

        gehalten = ""
        if soll is not None and soll != zustand:
            laufzeit_min = (jetzt - seit).total_seconds() / 60
            if zustand and cfg["min_on_minutes"] and laufzeit_min < cfg["min_on_minutes"]:
                gehalten = f"Mindestlaufzeit ({int(laufzeit_min)}/{cfg['min_on_minutes']} min)"
            elif not zustand and cfg["min_off_minutes"] and laufzeit_min < cfg["min_off_minutes"]:
                gehalten = f"Mindest-Aus-Zeit ({int(laufzeit_min)}/{cfg['min_off_minutes']} min)"
            else:
                zustand = soll
                seit = jetzt
                schaltvorgaenge.append((jetzt.strftime("%H:%M"), soll, entscheidung.reason))

        if zustand:
            ein_stunden.append(eintrag)
        zeilen.append((jetzt.strftime("%H:%M"), round(eintrag["total"] * 100, 2), zustand, gehalten))

    if zeige_tabelle:
        print(f"\n{'Zeit':>6} {'ct/kWh':>8}  Schalter")
        print("  " + "-" * 44)
        for zeit, ct, an, gehalten in zeilen:
            balken = "█" * max(1, int(ct / 2))
            print(f"{zeit:>6} {ct:>8.2f}  {'EIN ' if an else 'aus '} {balken}"
                  + (f"   ({gehalten})" if gehalten else ""))

    preise_ein = [e["total"] * 100 for e in ein_stunden]
    return {
        "stunden_ein": len(ein_stunden),
        "schaltvorgaenge": len(schaltvorgaenge),
        "verlauf": schaltvorgaenge,
        "schnitt_ct": sum(preise_ein) / len(preise_ein) if preise_ein else 0.0,
        "bloecke": bloecke_zaehlen(ein_stunden),
    }


def bloecke_zaehlen(ein_stunden: list[dict]) -> list[str]:
    """Zusammenhaengende Einschaltzeiten als lesbare Spannen."""
    if not ein_stunden:
        return []
    spannen, start, vorher = [], stunde_ts(ein_stunden[0]), stunde_ts(ein_stunden[0])
    for e in ein_stunden[1:]:
        t = stunde_ts(e)
        if (t - vorher) > dt.timedelta(hours=1):
            spannen.append(f"{start:%H:%M}–{vorher + dt.timedelta(hours=1):%H:%M}")
            start = t
        vorher = t
    spannen.append(f"{start:%H:%M}–{vorher + dt.timedelta(hours=1):%H:%M}")
    return spannen


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default="awattar_de")
    p.add_argument("--markup", type=float, default=20.0)
    p.add_argument("--vat", type=float, default=19.0)
    p.add_argument("--mode", default="", help="threshold | cheapest | cheapest_block | level")
    p.add_argument("--hours", type=int, default=6, help="Stunden fuer cheapest/cheapest_block")
    p.add_argument("--threshold", type=float, default=30.0)
    p.add_argument("--min-on", type=int, default=0, help="Mindestlaufzeit in Minuten")
    p.add_argument("--min-off", type=int, default=0)
    p.add_argument("--max-off", type=int, default=0, help="Sicherheitsnetz in Stunden")
    args = p.parse_args()

    preisdaten = await preise_holen(args.source, args.markup, args.vat)
    heute = preisdaten.get("today") or []
    print(f"Preisquelle: {args.source} · {len(heute)} Stunden · "
          f"Aufschlag {args.markup:.1f} ct + {args.vat:.0f}% MwSt")
    werte = [e["total"] * 100 for e in heute if e.get("total") is not None]
    if werte:
        print(f"Spanne heute: {min(werte):.2f} – {max(werte):.2f} ct/kWh, "
              f"Mittel {sum(werte)/len(werte):.2f}")

    modi = [args.mode] if args.mode else ["threshold", "cheapest", "cheapest_block"]
    ergebnisse = {}
    for modus in modi:
        cfg = automation.settings({
            "enabled": True, "mode": modus, "switch_code": "switch_1",
            "threshold_ct": args.threshold, "cheapest_hours": args.hours,
            "min_on_minutes": args.min_on, "min_off_minutes": args.min_off,
            "max_off_hours": args.max_off,
        })
        print(f"\n{'='*56}\n{automation.MODE_LABELS[modus]}"
              + (f" ({args.hours} h)" if modus.startswith("cheapest") else "")
              + (f" (Schwelle {args.threshold:.1f} ct)" if modus == "threshold" else ""))
        e = durchspielen(preisdaten, cfg, zeige_tabelle=bool(args.mode))
        ergebnisse[modus] = e
        if e.get("fehler"):
            print("  ", e["fehler"]); continue
        print(f"  Eingeschaltet: {e['stunden_ein']} h · Schaltvorgänge: {e['schaltvorgaenge']}"
              f" · Ø {e['schnitt_ct']:.2f} ct/kWh")
        print(f"  Zeitfenster:   {', '.join(e['bloecke']) or '—'}")
        for zeit, an, grund in e["verlauf"]:
            print(f"     {zeit}  {'EIN' if an else 'AUS'}  {grund[:70]}")

    if len(ergebnisse) > 1:
        print(f"\n{'='*56}\nVergleich (10 kWh angenommen)")
        print(f"  {'Regel':<26} {'h':>3} {'Schaltungen':>12} {'Ø ct':>7} {'Kosten':>9}")
        for modus, e in ergebnisse.items():
            if e.get("fehler"):
                continue
            print(f"  {automation.MODE_LABELS[modus]:<26} {e['stunden_ein']:>3}"
                  f" {e['schaltvorgaenge']:>12} {e['schnitt_ct']:>7.2f}"
                  f" {e['schnitt_ct'] * 10 / 100:>8.2f} €")


if __name__ == "__main__":
    asyncio.run(main())
