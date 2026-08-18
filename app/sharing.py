"""Geraetezugriff per QR-Anmeldung, ohne Tuya-Entwicklerkonto.

Dies ist derselbe Weg, den die Tuya-Integration von Home Assistant geht: Man
meldet sich mit einem QR-Code aus der Smart-Life-App an, statt ein befristetes
Cloud-Projekt anzulegen. Grundlage ist Tuyas eigenes Paket
`tuya-device-sharing-sdk`.

**Offen gesagt, damit niemand es aus dem Quelltext puzzeln muss:** Die
Anmeldung laeuft ueber eine bei Tuya registrierte Anwendungskennung. Die hier
voreingestellte ist die von Home Assistant; sie steht offen in dessen
Quelltext. Wer diesen Weg nutzt, meldet sich bei Tuya also als Home Assistant
an. Das ist der Grund, warum diese Stufe hier die *zweite* ist und nicht die
erste: Der lokale Weg braucht so etwas nicht.

Beide Kennungen sind konfigurierbar. Wer eine eigene bei Tuya registriert hat,
traegt sie ein und ist damit aus jeder Grauzone heraus.

Vorteile gegenueber dem Cloud-Weg mit eigenem Projekt:
* kein Entwicklerkonto, keine Access ID, kein Access Secret
* **keine Befristung** — der wesentliche Punkt
* Einrichtung in einer Minute per QR-Code

Nachteil: Es bleibt ein Cloud-Weg. Ohne Internet geht nichts, und die
Reaktionszeit liegt bei rund einer Sekunde statt 30 Millisekunden.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    from tuya_sharing import LoginControl, Manager, SharingTokenListener
except ImportError:  # optional
    LoginControl = None
    Manager = None
    SharingTokenListener = object

log = logging.getLogger("tuya-smartmeter.sharing")

# Voreinstellung: die offen dokumentierte Kennung der Home-Assistant-Integration.
STANDARD_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
STANDARD_SCHEMA = "haauthorize"


class SharingError(RuntimeError):
    pass


class _TokenSpeicher(SharingTokenListener):
    """Faengt erneuerte Zugangsdaten ab, damit sie gespeichert werden koennen."""

    def __init__(self, ablegen) -> None:
        self._ablegen = ablegen

    def update_token(self, token_info: dict[str, Any]) -> None:
        try:
            self._ablegen(token_info)
        except Exception as exc:  # niemals den Betrieb daran scheitern lassen
            log.warning("Token konnte nicht gespeichert werden: %s", exc)


# --------------------------------------------------------------- Anmeldung


def qr_code_anfordern(user_code: str, client_id: str = "", schema: str = "") -> dict[str, Any]:
    """Schritt 1: QR-Code besorgen, den der Nutzer mit der App scannt."""
    if LoginControl is None:
        raise SharingError("Das Paket tuya-device-sharing-sdk fehlt")
    steuerung = LoginControl()
    antwort = steuerung.qr_code(
        client_id or STANDARD_CLIENT_ID,
        schema or STANDARD_SCHEMA,
        user_code.strip(),
    )
    if not antwort.get("success"):
        raise SharingError(antwort.get("msg") or "Tuya lehnt die Anfrage ab")
    inhalt = antwort.get("result", {})
    token = inhalt.get("qrcode", "")
    return {
        "token": token,
        # Genau diese Zeichenkette muss im QR-Bild stecken:
        "qr_inhalt": f"tuyaSmart--qrLogin?token={token}",
    }


def anmeldung_pruefen(
    token: str, user_code: str, client_id: str = ""
) -> dict[str, Any]:
    """Schritt 2: nach dem Scannen die Zugangsdaten abholen."""
    if LoginControl is None:
        raise SharingError("Das Paket tuya-device-sharing-sdk fehlt")
    steuerung = LoginControl()
    erfolg, inhalt = steuerung.login_result(
        token, client_id or STANDARD_CLIENT_ID, user_code.strip()
    )
    if not erfolg:
        raise SharingError(inhalt.get("msg") or "Noch nicht bestaetigt")
    return inhalt


# ----------------------------------------------------------------- Betrieb


class SharingDevice:
    """Ein Geraet ueber die QR-Anmeldung — gleiche Schnittstelle wie die anderen Wege."""

    def __init__(self, token_info: dict[str, Any], user_code: str,
                 device_id: str, client_id: str = "", schema: str = "",
                 token_ablegen=None) -> None:
        if Manager is None:
            raise SharingError("Das Paket tuya-device-sharing-sdk fehlt")
        self.device_id = device_id

        # Reihenfolge laut SDK: client_id, user_code, terminal_id, end_point,
        # token_response, listener. Terminal und Endpunkt stehen in der Antwort
        # der Anmeldung — sie sind je Anmeldung verschieden und gehoeren zum
        # Token, nicht zur Anwendung.
        terminal_id = token_info.get("terminal_id", "")
        end_point = token_info.get("endpoint", "")
        if not (terminal_id and end_point):
            raise SharingError(
                "Die gespeicherten Zugangsdaten sind unvollständig "
                "(terminal_id oder endpoint fehlt) — bitte neu anmelden"
            )

        self._manager = Manager(
            client_id or STANDARD_CLIENT_ID,
            user_code.strip(),
            terminal_id,
            end_point,
            token_info,
            _TokenSpeicher(token_ablegen) if token_ablegen else None,
        )

    def _aktualisieren(self) -> dict[str, Any]:
        self._manager.update_device_cache()
        geraet = self._manager.device_map.get(self.device_id)
        if geraet is None:
            raise SharingError(
                f"Gerät {self.device_id} ist über diesen Zugang nicht sichtbar"
            )
        return geraet

    async def status(self) -> list[dict[str, Any]]:
        geraet = await asyncio.to_thread(self._aktualisieren)
        return [{"code": k, "value": v} for k, v in (geraet.status or {}).items()]

    async def online(self) -> bool:
        geraet = await asyncio.to_thread(self._aktualisieren)
        return bool(getattr(geraet, "online", True))

    async def send_commands(self, commands: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(
            self._manager.send_commands, self.device_id, commands
        )

    async def geraete_liste(self) -> list[dict[str, Any]]:
        def holen():
            self._manager.update_device_cache()
            return [
                {"id": g.id, "name": g.name, "product_name": getattr(g, "product_name", ""),
                 "online": getattr(g, "online", True), "category": getattr(g, "category", "")}
                for g in self._manager.device_map.values()
            ]
        return await asyncio.to_thread(holen)
