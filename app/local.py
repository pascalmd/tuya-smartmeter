"""Direkter Draht zum Geraet im eigenen Netz, ohne Umweg ueber die Tuya-Cloud.

Warum das der bessere Weg ist, wenn er geht:

* **Keine Frist.** Der Cloud-Zugang ist befristet und muss beantragt verlaengert
  werden. Der lokale Schluessel gilt, bis das Geraet neu angelernt wird.
* **Kein Kontingent.** Die kostenlose Cloud erlaubt rund 26.000 Abfragen im
  Monat; lokal gibt es keine Grenze.
* **Schneller.** Gemessen 30 ms statt rund einer Sekunde.
* **Mehr Daten.** Der Zaehlerstand (`total_ele`) kommt nur lokal — ueber die
  Cloud liefert dasselbe Geraet ihn nicht mit.
* **Kein Internet noetig.**

Der Preis: Das Geraet muss vom Server aus erreichbar sein, und der Local Key
muss einmal beschafft werden (dafuer genuegt ein Cloud-Zugang fuer wenige
Minuten — siehe `fetch_local_key` in main.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import tinytuya
except ImportError:  # optional, die App laeuft auch ohne
    tinytuya = None

log = logging.getLogger("tuya-smartmeter.local")

# Reihenfolge der Versuche. 3.3 deckt die meisten Geraete ab; 3.4/3.5 sind
# neuer und verschluesselt, 3.1 ist alt.
PROTOKOLL_VERSIONEN = (3.3, 3.4, 3.5, 3.1)


class LocalError(RuntimeError):
    pass


class LocalDevice:
    """Ein Geraet im eigenen Netz.

    `dp_map` bildet die Datenpunkt-Nummern des Geraets auf die Klarnamen ab,
    die auch die Cloud verwendet ({"1": "switch_1", "20": "cur_voltage", ...}).
    Damit sieht der Rest der App keinen Unterschied zwischen beiden Wegen.
    """

    def __init__(
        self,
        device_id: str,
        ip: str,
        local_key: str,
        dp_map: dict[str, str] | None = None,
        version: float | None = None,
        timeout: float = 5.0,
    ) -> None:
        if tinytuya is None:
            raise LocalError("Das Paket tinytuya fehlt — lokaler Zugriff nicht möglich")
        self.device_id = device_id
        self.ip = ip
        self.local_key = local_key
        self.dp_map = dp_map or {}
        self.version = version
        self.timeout = timeout

    # ---------------------------------------------------------------- intern

    def _verbindung(self, version: float):
        d = tinytuya.OutletDevice(self.device_id, self.ip, self.local_key)
        d.set_version(version)
        d.set_socketTimeout(self.timeout)
        d.set_socketRetryLimit(1)

        # Ab 3.4 handelt das Geraet zu Beginn einen Sitzungsschluessel aus.
        # Ohne dauerhafte Verbindung faellt der nach jedem Aufruf weg, und der
        # naechste beginnt wieder mit dem Handshake - bei manchen Geraeten
        # scheitert er dann. Bei 3.3 und aelter gibt es den Handshake nicht,
        # dort waere eine offen gehaltene Verbindung nur ein Dauerverbraucher.
        if version >= 3.4:
            d.set_socketPersistent(True)
        return d

    def _schliessen(self, d) -> None:
        """Dauerverbindung wieder abbauen — sonst haelt jedes Geraet einen Socket."""
        try:
            d.close()
        except Exception:
            pass

    def _status_roh(self) -> dict[str, Any]:
        """Status holen und dabei die passende Protokollversion ermitteln."""
        versionen = (self.version,) if self.version else PROTOKOLL_VERSIONEN
        letzter_fehler = ""
        for v in versionen:
            d = self._verbindung(v)
            try:
                antwort = d.status()
            except Exception as exc:            # z.B. Handshake-Fehler bei 3.4/3.5
                letzter_fehler = f"{v}: {exc}"
                continue
            finally:
                self._schliessen(d)
            if isinstance(antwort, dict) and "dps" in antwort:
                if self.version != v:
                    self.version = v
                    log.info("Protokollversion %s erkannt", v)
                return antwort["dps"]
            letzter_fehler = f"{v}: {antwort}"
        raise LocalError(f"Gerät antwortet nicht ({letzter_fehler[:140]})")

    def _schalten_roh(self, dp: int, wert: Any) -> None:
        d = self._verbindung(self.version or PROTOKOLL_VERSIONEN[0])
        try:
            antwort = d.set_value(dp, wert)
        finally:
            self._schliessen(d)
        if isinstance(antwort, dict) and antwort.get("Error"):
            raise LocalError(str(antwort.get("Error"))[:120])

    def _code_zu_dp(self, code: str) -> int:
        for dp, name in self.dp_map.items():
            if name == code:
                return int(dp)
        raise LocalError(f"Kein Datenpunkt für '{code}' bekannt")

    # ------------------------------------------------------------ oeffentlich

    async def status(self) -> list[dict[str, Any]]:
        """Wie der Cloud-Status: Liste aus {code, value}."""
        roh = await asyncio.to_thread(self._status_roh)
        out = []
        for dp, wert in roh.items():
            code = self.dp_map.get(str(dp))
            if not code:
                # Unbekannte Datenpunkte durchreichen statt verschweigen -
                # manche Geraete melden mehr, als die Cloud kennt.
                code = f"dp_{dp}"
            out.append({"code": code, "value": wert})
        return out

    async def send_commands(self, commands: list[dict[str, Any]]) -> None:
        for befehl in commands:
            dp = self._code_zu_dp(befehl["code"])
            await asyncio.to_thread(self._schalten_roh, dp, befehl["value"])

    async def erreichbar(self) -> bool:
        try:
            await self.status()
            return True
        except Exception:
            return False


# Bei Steckdosen und Zaehlern (Kategorie "cz") ist diese Belegung weit
# verbreitet. Sie dient als Starthilfe, wenn keine Zuordnung vorliegt — bestaetigt
# wird sie anschliessend durch Wertevergleich.
STANDARD_DP_MAP = {
    "1": "switch_1",
    "9": "countdown_1",
    "18": "cur_current",
    "19": "cur_power",
    "20": "cur_voltage",
    "101": "total_ele",
}


def dp_map_aus_vergleich(
    benannt: dict[str, Any], nummeriert: dict[str, Any]
) -> dict[str, str]:
    """Datenpunkt-Nummern den Klarnamen zuordnen, indem die Werte verglichen werden.

    Ohne Entwicklerkonto gibt es keine offizielle Zuordnungstabelle. Beide Wege
    lesen aber dasselbe Geraet: Meldet die Cloud `cur_voltage = 2310` und das
    Geraet lokal `20 = 2310`, ist die Zuordnung klar.

    Nur eindeutige Werte werden verwendet — kommt eine Zahl mehrfach vor (etwa
    die vielen Nullen im Leerlauf), bleibt sie unberuecksichtigt. Was offen
    bleibt, klaert sich bei spaeteren Messungen oder ueber STANDARD_DP_MAP.
    """
    zuordnung: dict[str, str] = {}

    def eindeutig(d: dict[str, Any]) -> dict[Any, str]:
        haeufigkeit: dict[Any, int] = {}
        for wert in d.values():
            schluessel = (type(wert).__name__, wert)
            haeufigkeit[schluessel] = haeufigkeit.get(schluessel, 0) + 1
        return {
            (type(w).__name__, w): k
            for k, w in d.items()
            if haeufigkeit[(type(w).__name__, w)] == 1
        }

    links, rechts = eindeutig(benannt), eindeutig(nummeriert)
    for wert, code in links.items():
        if wert in rechts:
            zuordnung[str(rechts[wert])] = code
    return zuordnung


async def suche_im_netz(dauer: int = 12) -> dict[str, dict[str, Any]]:
    """Tuya-Geraete im eigenen Netz per Rundruf finden.

    Findet nur Geraete im selben Netzsegment — der Rundruf wird nicht geroutet.
    Haengt das Geraet in einem anderen VLAN, muss die Adresse von Hand
    eingetragen werden.
    """
    if tinytuya is None:
        return {}
    return await asyncio.to_thread(tinytuya.deviceScan, False, dauer)
