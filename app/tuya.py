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

    async def device_spec(self, device_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1.0/iot-03/devices/{device_id}/specification")

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
    switches.sort(key=lambda s: s["code"])

    metrics = []
    phases = []
    for code, value in sorted(values.items()):
        if code in spec_funcs and spec_funcs[code].get("type") == "Boolean":
            continue
        if code.startswith("phase_") and isinstance(value, str):
            decoded = decode_phase(value)
            if decoded:
                phases.append({"code": code, "label": _pretty(code), **decoded})
                continue
        meta = spec_status.get(code, {})
        scale = int(meta.get("scale", 0) or 0)
        unit = (meta.get("unit") or "").strip()
        shown: Any = value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and scale:
            shown = round(value / (10 ** scale), scale)
        metrics.append(
            {
                "code": code,
                "label": _pretty(code),
                "value": shown,
                "raw": value,
                "unit": unit,
            }
        )

    return {"switches": switches, "metrics": metrics, "phases": phases}


_LABELS = {
    "switch": "Schalter",
    "switch_1": "Schalter 1",
    "switch_prepayment": "Vorkasse-Schalter",
    "cur_voltage": "Spannung",
    "cur_current": "Strom",
    "cur_power": "Leistung",
    "add_ele": "Energie (Zuwachs)",
    "forward_energy_total": "Zaehlerstand gesamt",
    "total_forward_energy": "Zaehlerstand gesamt",
    "energy_reset": "Zaehler zuruecksetzen",
    "phase_a": "Phase A",
    "phase_b": "Phase B",
    "phase_c": "Phase C",
    "balance_energy": "Restguthaben",
    "charge_energy": "Aufladung",
    "temp_current": "Temperatur",
    "leakage_current": "Fehlerstrom",
    "fault": "Stoerung",
    "relay_status": "Relais nach Stromausfall",
    "child_lock": "Kindersicherung",
}


def _pretty(code: str) -> str:
    return _LABELS.get(code, code.replace("_", " ").capitalize())
