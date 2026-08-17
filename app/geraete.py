"""Der Geraetebestand.

Bis Version 1.4 kannte die App genau ein Geraet: `device_id`, `device_name`,
`local` und `automation` lagen einzeln in der Konfiguration. Ab 1.5 gibt es
eine Liste -- ein zweiter Zaehler, eine Schaltsteckdose oder ein Geraet, das
nur von Hand bedient wird, koennen nebeneinander stehen.

**Die Schaltregel ist dabei gemeinsam.** Sie haengt am Strompreis, nicht am
Geraet: Wenn Strom billig ist, ist er es fuer alle. Je Geraet gibt es deshalb
nur zwei Dinge -- ob es der Automatik folgt und welchen Ausgang sie schaltet
(der eine nennt ihn `switch`, der naechste `switch_1`).

Die alten Felder bleiben erhalten und zeigen weiter auf das erste Geraet. Das
kostet wenig und haelt bestehende Anbindungen (ioBroker, Zabbix, eigene
Skripte an /api/state) am Leben.
"""

from __future__ import annotations

import time
from typing import Any

from .config import config

# Wie oft ein Geraet abgefragt wird, haengt am gemeinsamen Takt. Der Verbrauch
# des Tuya-Kontingents vervielfacht sich aber mit jedem Geraet, das ueber die
# Cloud laeuft -- darauf weist die Oberflaeche hin.
VORLAGE: dict[str, Any] = {
    "id": "",
    "name": "",
    "local": {
        "enabled": False,
        "ip": "",
        "key": "",
        "version": 0,
        "dp_map": {},
        "fallback_cloud": True,
    },
    # Die Schaltregel ist gemeinsam (siehe unten). Je Geraet bleibt nur, was
    # wirklich am Geraet haengt: ob es mitmacht und welcher Ausgang gemeint ist.
    "automatik_aktiv": True,
    "switch_code": "",        # leer = beim ersten Abruf selbst erkennen
    "override_until": 0,
    "aufzeichnen": True,
}


def _leer() -> dict[str, Any]:
    import copy

    return copy.deepcopy(VORLAGE)


def normalisieren(roh: dict[str, Any]) -> dict[str, Any]:
    """Einen gespeicherten Eintrag auf die volle Form bringen."""
    eintrag = _leer()
    for schluessel, wert in (roh or {}).items():
        if schluessel in eintrag and isinstance(eintrag[schluessel], dict) and isinstance(wert, dict):
            eintrag[schluessel].update(wert)
        else:
            eintrag[schluessel] = wert
    eintrag["id"] = str(eintrag.get("id") or "").strip()
    eintrag["name"] = str(eintrag.get("name") or "").strip()
    return eintrag


def liste() -> list[dict[str, Any]]:
    """Alle eingerichteten Geraete, immer in der gespeicherten Reihenfolge."""
    roh = config.get("devices")
    if roh is None:
        roh = _aus_einzelgeraet()
    return [normalisieren(e) for e in roh if (e or {}).get("id")]


def _aus_einzelgeraet() -> list[dict[str, Any]]:
    """Uebergang von der Einzelgeraet-Konfiguration, ohne sie zu zerstoeren."""
    device_id = (config.get("device_id") or "").strip()
    if not device_id:
        return []
    eintrag = _leer()
    eintrag["id"] = device_id
    eintrag["name"] = config.get("device_name") or ""
    eintrag["local"].update(config.get("local") or {})
    eintrag["switch_code"] = (config.get("automation") or {}).get("switch_code", "")
    eintrag["override_until"] = config.get("override_until") or 0
    return [eintrag]


def migrieren() -> bool:
    """Einmalig die Liste anlegen. Gibt zurueck, ob etwas geschrieben wurde."""
    if config.get("devices") is not None:
        return False
    config.set("devices", _aus_einzelgeraet())
    config.save()
    return True


def speichern(eintraege: list[dict[str, Any]]) -> None:
    """Liste ablegen und die alten Einzelfelder auf das erste Geraet zeigen lassen."""
    sauber = [normalisieren(e) for e in eintraege if (e or {}).get("id")]
    config.set("devices", sauber)
    erstes = sauber[0] if sauber else None
    config.set("device_id", erstes["id"] if erstes else "")
    config.set("device_name", erstes["name"] if erstes else "")
    config.set("local", dict(erstes["local"]) if erstes else dict(VORLAGE["local"]))
    config.set("override_until", erstes["override_until"] if erstes else 0)
    config.save()


def holen(device_id: str) -> dict[str, Any] | None:
    for eintrag in liste():
        if eintrag["id"] == device_id:
            return eintrag
    return None


def primaer() -> dict[str, Any] | None:
    """Das Geraet, das gemeint ist, wenn keines genannt wurde."""
    alle = liste()
    return alle[0] if alle else None


def aufloesen(device_id: str | None) -> dict[str, Any] | None:
    """Angefragtes Geraet oder, wenn es das nicht gibt, das primaere."""
    if device_id:
        gefunden = holen(device_id)
        if gefunden:
            return gefunden
    return primaer()


def hinzufuegen(device_id: str, name: str) -> dict[str, Any]:
    """Neues Geraet aufnehmen; ein bereits vorhandenes wird nur umbenannt."""
    alle = liste()
    for eintrag in alle:
        if eintrag["id"] == device_id:
            if name:
                eintrag["name"] = name
            speichern(alle)
            return eintrag
    neu = _leer()
    neu["id"] = device_id.strip()
    neu["name"] = name.strip()
    alle.append(neu)
    speichern(alle)
    return neu


def entfernen(device_id: str) -> bool:
    alle = liste()
    rest = [e for e in alle if e["id"] != device_id]
    if len(rest) == len(alle):
        return False
    speichern(rest)
    return True


def aktualisieren(device_id: str, **felder: Any) -> dict[str, Any] | None:
    """Einzelne Felder eines Geraets aendern und die Liste sichern."""
    alle = liste()
    getroffen = None
    for eintrag in alle:
        if eintrag["id"] == device_id:
            getroffen = eintrag
            for schluessel, wert in felder.items():
                if isinstance(eintrag.get(schluessel), dict) and isinstance(wert, dict):
                    eintrag[schluessel].update(wert)
                else:
                    eintrag[schluessel] = wert
            break
    if getroffen is None:
        return None
    speichern(alle)
    return getroffen


def handbetrieb_bis(device_id: str) -> float:
    eintrag = holen(device_id)
    return float((eintrag or {}).get("override_until") or 0)


def handbetrieb_setzen(device_id: str, bis: float) -> None:
    aktualisieren(device_id, override_until=bis)


def anzahl_cloud() -> int:
    """Wie viele Geraete (voraussichtlich) ueber die Tuya-Cloud laufen.

    Fuer die Hochrechnung des Kontingents: Wer lokal angebunden ist, kostet
    keine Aufrufe. Zaehlt nur, was keinen eingerichteten lokalen Zugang hat.
    """
    return sum(
        1 for e in liste()
        if not (e["local"].get("enabled") and e["local"].get("ip") and e["local"].get("key"))
    ) or (1 if liste() else 0)


def zusammenfassung() -> list[dict[str, Any]]:
    """Kurzform fuer Auswahlmenues."""
    return [
        {"id": e["id"], "name": e["name"] or e["id"][:8],
         "lokal": bool(e["local"].get("enabled") and e["local"].get("key"))}
        for e in liste()
    ]


def jetzt() -> float:
    return time.time()
