"""Minimaler Client fuer die Tuya Cloud OpenAPI (v1.0 / iot-03).

Signatur nach Tuya "sign_method=HMAC-SHA256", Token wird gecacht und vor
Ablauf per refresh_token erneuert.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

# Data Center -> API-Endpunkt. Muss zur Region des Tuya-Projekts passen,
# sonst kommt "sign invalid" oder "no permissions".
ENDPOINTS = {
    "eu": "https://openapi.tuyaeu.com",
    "eu-west": "https://openapi-weaz.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "us-west": "https://openapi-ueaz.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}


class TuyaError(RuntimeError):
    """Die API hat success=false geliefert."""

    def __init__(self, code: Any, msg: str, path: str):
        super().__init__(f"Tuya-API {path}: [{code}] {msg}")
        self.code = code
        self.msg = msg


@dataclass
class TuyaClient:
    client_id: str
    client_secret: str
    region: str = "eu"
    timeout: float = 15.0

    _token: str = field(default="", init=False)
    _refresh_token: str = field(default="", init=False)
    _expires_at: float = field(default=0.0, init=False)

    @property
    def base_url(self) -> str:
        if self.region not in ENDPOINTS:
            raise ValueError(f"Unbekannte Region '{self.region}', erlaubt: {', '.join(ENDPOINTS)}")
        return ENDPOINTS[self.region]

    # ------------------------------------------------------------------ Signatur

    def _sign(self, method: str, path: str, body: str, with_token: bool) -> dict[str, str]:
        t = str(int(time.time() * 1000))
        content_hash = hashlib.sha256(body.encode()).hexdigest()
        string_to_sign = "\n".join([method, content_hash, "", path])
        token = self._token if with_token else ""
        payload = f"{self.client_id}{token}{t}{string_to_sign}"
        sign = hmac.new(
            self.client_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest().upper()

        headers = {
            "client_id": self.client_id,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "sign": sign,
            "Content-Type": "application/json",
        }
        if with_token:
            headers["access_token"] = token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        with_token: bool = True,
    ) -> Any:
        if params:
            # Query-Parameter muessen sortiert in die Signatur einfliessen.
            query = urlencode(sorted((k, v) for k, v in params.items() if v is not None))
            path = f"{path}?{query}"
        raw_body = json.dumps(body, separators=(",", ":")) if body is not None else ""

        if with_token:
            await self._ensure_token()

        headers = self._sign(method, path, raw_body, with_token)
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            resp = await http.request(
                method, f"{self.base_url}{path}", headers=headers, content=raw_body or None
            )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            code = data.get("code")
            # 1010/1011 = Token abgelaufen oder ungueltig -> einmal frisch holen.
            if with_token and code in (1010, 1011, 1012):
                self._token = ""
                self._expires_at = 0.0
                await self._ensure_token()
                headers = self._sign(method, path, raw_body, True)
                async with httpx.AsyncClient(timeout=self.timeout) as http:
                    resp = await http.request(
                        method, f"{self.base_url}{path}", headers=headers, content=raw_body or None
                    )
                data = resp.json()
                if data.get("success"):
                    return data.get("result")
            raise TuyaError(code, data.get("msg", "unbekannter Fehler"), path)

        return data.get("result")

    async def _ensure_token(self) -> None:
        if self._token and time.time() < self._expires_at - 60:
            return
        result = await self._request("GET", "/v1.0/token", params={"grant_type": 1}, with_token=False)
        self._token = result["access_token"]
        self._refresh_token = result.get("refresh_token", "")
        self._expires_at = time.time() + int(result.get("expire_time", 7200))

    # ------------------------------------------------------------------ Geraete

    async def list_devices(self) -> list[dict[str, Any]]:
        """Alle Geraete des Projekts (via "Link App Account" verknuepfte Konten)."""
        devices: list[dict[str, Any]] = []
        last_row_key = None
        while True:
            result = await self._request(
                "GET",
                "/v1.0/iot-01/associated-users/devices",
                params={"last_row_key": last_row_key, "size": 100},
            )
            devices.extend(result.get("devices", []))
            if not result.get("has_more"):
                break
            last_row_key = result.get("last_row_key")
        return devices

    async def device_info(self, device_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1.0/iot-03/devices/{device_id}")

    async def device_status(self, device_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/v1.0/iot-03/devices/{device_id}/status")

    async def device_snapshot(self, device_id: str) -> dict[str, Any]:
        """Messwerte UND Verbindungszustand in einem Aufruf.

        Wichtig: Die Cloud liefert auch dann den zuletzt bekannten Stand, wenn
        das Geraet laengst vom Netz ist. Ohne das online-Flag sieht eine tote
        Verbindung aus wie ein stiller Betrieb - mit Spannungswerten, die von
        gestern stammen.
        """
        result = await self._request("GET", f"/v1.0/devices/{device_id}")
        return {
            "online": bool(result.get("online")),
            "status": result.get("status", []),
            "name": result.get("name", ""),
        }

    async def device_spec(self, device_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1.0/iot-03/devices/{device_id}/specification")

    async def device_spec_with_dp(self, device_id: str) -> dict[str, Any]:
        """Spezifikation samt Datenpunkt-Nummern.

        Der lokale Zugriff spricht in Nummern (dp 20), die Cloud in Klarnamen
        (cur_voltage). Diese Abfrage liefert beides und erlaubt damit die
        Uebersetzung — sie ist der einzige Grund, warum fuer die Einrichtung des
        lokalen Wegs ueberhaupt ein Cloud-Zugang noetig ist.
        """
        return await self._request("GET", f"/v1.1/devices/{device_id}/specifications")

    async def device_model(self, device_id: str) -> dict[str, str]:
        """Datenpunkt-Zuordnung aus dem Datenmodell.

        Liefert mehr als die Spezifikation: Beim DDS238-2 etwa den
        Zaehlerstand (`total_ele`), der im normalen Cloud-Status fehlt.
        """
        antwort = await self._request("GET", f"/v2.0/cloud/thing/{device_id}/model")
        roh = antwort.get("model") if isinstance(antwort, dict) else None
        if not roh:
            return {}
        modell = json.loads(roh) if isinstance(roh, str) else roh
        zuordnung: dict[str, str] = {}
        for dienst in modell.get("services", []):
            for eigenschaft in dienst.get("properties", []):
                dp, code = eigenschaft.get("abilityId"), eigenschaft.get("code")
                if dp is not None and code:
                    zuordnung[str(dp)] = code
        return zuordnung

    async def local_key(self, device_id: str) -> str:
        """Den Schluessel fuer den lokalen Zugriff holen.

        Aendert sich, sobald das Geraet neu angelernt wird.
        """
        result = await self._request("GET", f"/v1.0/devices/{device_id}")
        return result.get("local_key", "")

    async def send_commands(self, device_id: str, commands: list[dict[str, Any]]) -> Any:
        return await self._request(
            "POST",
            f"/v1.0/iot-03/devices/{device_id}/commands",
            body={"commands": commands},
        )


# ---------------------------------------------------------------------- Auswertung


def _parse_values(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def decode_phase(value: str) -> dict[str, float] | None:
    """Tuya kodiert Phasendaten (phase_a/b/c) base64: 2B Spannung, 3B Strom, 3B Leistung."""
    try:
        raw = base64.b64decode(value)
    except Exception:
        return None
    if len(raw) < 8:
        return None
    return {
        "voltage_v": int.from_bytes(raw[0:2], "big") / 10,
        "current_a": int.from_bytes(raw[2:5], "big") / 1000,
        "power_w": int.from_bytes(raw[5:8], "big"),
    }


# Schaltbare Ausgaenge heissen bei Tuya durchgaengig so. Bewusst eng gehalten:
# `child_lock` oder `relay_status` sind ebenfalls boolesch, aber keine Ausgaenge
# -- sie hier aufzunehmen wuerde Bedienelemente erzeugen, die Unfug anrichten.
SCHALT_PRAEFIXE = ("switch", "socket", "outlet")
SCHALT_AUSNAHMEN = ("switch_overcharge", "switch_backlight", "switch_led")

# Was die Spezifikation sonst mitliefert: Einheit und Nachkommastellen der
# gaengigen Messgroessen. Tuya haelt sich hier praktisch ueberall dran.
STANDARD_EINHEITEN: dict[str, tuple[str, int]] = {
    "cur_voltage": ("V", 1),
    "cur_current": ("mA", 0),
    "cur_power": ("W", 1),
    "add_ele": ("kWh", 3),
    "total_ele": ("kWh", 2),
    "forward_energy_total": ("kWh", 2),
    "reverse_energy_total": ("kWh", 2),
    "power_factor": ("", 0),
    "temp_current": ("°C", 0),
}


def _ist_schaltbar(code: str) -> bool:
    """Ob ein boolescher Code ein Ausgang ist -- ohne Spezifikation nur am Namen."""
    if any(code.startswith(a) for a in SCHALT_AUSNAHMEN):
        return False
    return any(code.startswith(p) for p in SCHALT_PRAEFIXE)


def build_view(spec: dict[str, Any], status: list[dict[str, Any]]) -> dict[str, Any]:
    """Status + Spezifikation zu einer anzeigefertigen Struktur zusammenfuehren.

    Liefert Schalter (bool-Funktionen) und Messwerte (skaliert, mit Einheit) getrennt.
    """
    spec_status = {item["code"]: _parse_values(item.get("values")) for item in spec.get("status", [])}
    spec_funcs = {item["code"]: item for item in spec.get("functions", [])}
    values = {item["code"]: item.get("value") for item in status}

    switches = []
    for code, func in spec_funcs.items():
        if func.get("type") != "Boolean":
            continue
        switches.append(
            {
                "code": code,
                "label": _pretty(code),
                "value": bool(values.get(code)),
                "present": code in values,
            }
        )

    # Ohne Spezifikation muss der Schalter am Namen erkannt werden. Das ist der
    # Normalfall beim lokalen Zugang und bei der QR-Anmeldung: Beide liefern nur
    # Codes und Werte, die Beschreibung gibt allein das Entwicklerprojekt heraus.
    # Ohne diese Zuordnung waere eine simple Schaltsteckdose auf diesen Wegen
    # ueberhaupt nicht schaltbar -- ihr Schalter stuende als "Messwert" da.
    bekannt = {s["code"] for s in switches}
    for code, value in values.items():
        if code in bekannt or not isinstance(value, bool):
            continue
        if not _ist_schaltbar(code):
            continue
        switches.append(
            {"code": code, "label": _pretty(code), "value": bool(value), "present": True}
        )
    switches.sort(key=lambda s: s["code"])

    metrics = []
    settings_shown = []
    phases = []
    schalter_codes = {s["code"] for s in switches}
    for code, value in sorted(values.items()):
        if code in spec_funcs and spec_funcs[code].get("type") == "Boolean":
            continue
        if code in schalter_codes:
            continue
        if code.startswith("phase_") and isinstance(value, str):
            decoded = decode_phase(value)
            if decoded:
                phases.append({"code": code, "label": _pretty(code), **decoded})
                continue
        meta = spec_status.get(code, {})
        scale = int(meta.get("scale", 0) or 0)
        unit = (meta.get("unit") or "").strip()
        if not meta and code in STANDARD_EINHEITEN:
            unit, scale = STANDARD_EINHEITEN[code]
        shown: Any = value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and scale:
            shown = round(value / (10 ** scale), scale)

        # Tuya liefert Strom in mA und manche Spannungen in mV. In der Anzeige
        # sind daraus 16000 mA statt 16 A - fuer Menschen unbrauchbar.
        if unit in ("mA", "mV") and isinstance(shown, (int, float)) and not isinstance(shown, bool):
            shown = round(shown / 1000, 3)
            unit = unit[1:]
        eintrag = {
            "code": code,
            "label": _pretty(code),
            "value": shown,
            "raw": value,
            "unit": unit,
        }

        # Was sich auch stellen laesst, ist ein Sollwert und keine Messung -
        # etwa ein Abschalt-Timer. Getrennt fuehren, damit die Messwerte
        # Messwerte bleiben. Ein Ja/Nein-Wert ist ohnehin nie eine Messung:
        # Kindersicherung oder Anzeigebeleuchtung gehoeren zu den Zustaenden,
        # sonst stehen sie in der Messwertliste und landen in der Historie.
        if code in spec_funcs or isinstance(value, bool):
            settings_shown.append(eintrag)
        else:
            metrics.append(eintrag)

    return {
        "switches": switches,
        "metrics": metrics,
        "settings": settings_shown,
        "phases": phases,
    }


# Klartext fuer die Codes, die uns bisher tatsaechlich begegnet sind. Alles
# andere faellt auf eine lesbare Schreibweise des Codes zurueck — lieber ein
# nuechternes "Add ele" als eine ausgedachte Bedeutung.
_LABELS = {
    # am DDS238-2 WIFI verifiziert
    "switch": "Schalter",
    "switch_1": "Schalter",
    "countdown_1": "Abschalt-Timer",
    "cur_voltage": "Spannung",
    "cur_current": "Strom",
    "cur_power": "Leistung",
    "total_ele": "Zaehlerstand",
    "add_ele": "Energie (Zuwachs)",
    # bei Tuya-Energiezaehlern gebraeuchlich, hier nicht gegengeprueft
    "add_ele": "Energie (Zuwachs)",
    "forward_energy_total": "Zaehlerstand gesamt",
    "total_forward_energy": "Zaehlerstand gesamt",
    "phase_a": "Phase A",
    "phase_b": "Phase B",
    "phase_c": "Phase C",
    "temp_current": "Temperatur",
    "fault": "Stoerung",
    "child_lock": "Kindersicherung",
    "relay_status": "Relais nach Stromausfall",
}


def _pretty(code: str) -> str:
    return _LABELS.get(code, code.replace("_", " ").capitalize())
