"""Tuya Smartmeter Control — Weboberflaeche, Dauerbetrieb-Poller, Preisautomatik."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import automation, diagnose, geraete, local, prices, sharing, store
from .config import config
from .tibber import LEVEL_LABELS, LEVELS, TibberClient, TibberError, upcoming
from .tuya import ENDPOINTS, TuyaClient, TuyaError, build_view

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tuya-smartmeter")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _zeitpunkt(ts: float) -> str:
    """Unix-Zeit lesbar machen — in der Zeitzone des Servers."""
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%d.%m. %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


TEMPLATES.env.filters["zeitpunkt"] = _zeitpunkt

# Wird beim Bauen gesetzt; im Entwicklungsbetrieb bleibt es bei "dev".
VERSION = os.environ.get("APP_VERSION", "dev")
BUILD_DATE = os.environ.get("BUILD_DATE", "unbekannt")
GIT_COMMIT = os.environ.get("GIT_COMMIT", "unbekannt")

# Tuyas kostenlose "Trial Edition" erlaubt 26.000 API-Aufrufe im Monat - das
# sind 867 am Tag oder einer alle 100 Sekunden. Bei 10 s Takt waere das
# Kontingent nach drei Tagen aufgebraucht. 180 s lassen der Automatik reichlich
# Genauigkeit (die Preise wechseln stuendlich) und verbrauchen nur 60 Prozent.
TRIAL_CALLS_PER_MONTH = 26_000
MIN_INTERVAL = 5
MAX_INTERVAL = 3600
PRICE_REFRESH_SECONDS = 600  # Preise sind stundenscharf; 10 min reicht reichlich

# Wie oft ein Messwert in die Historie geschrieben wird. Bewusst entkoppelt vom
# Abfrageintervall: Auf einem Raspberry Pi liegt die Datenbank auf einer
# SD-Karte, und alle 10 s zu schreiben killt die Karte binnen Monaten.
# Geschaltet wird trotzdem im vollen Takt - nur das Protokoll ist gröber.
HISTORY_SECONDS_DEFAULT = 60


class Preise:
    """Die Strompreise gelten fuer alle Geraete gleich.

    Sie haengen am Stromvertrag, nicht am Geraet -- deshalb werden sie einmal
    abgerufen und von allen Geraetezustaenden gelesen, statt pro Geraet ein
    eigenes Kontingent zu verbrauchen.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ts: float = 0.0
        self.error: str = ""


preise = Preise()


class DeviceState:
    """Letzter bekannter Stand eines Geraets, vom Hintergrund-Poller gepflegt."""

    def __init__(self, device_id: str = "", name: str = "") -> None:
        self.device_id: str = device_id
        self.name: str = name
        self.ts: float = 0.0
        self.ok: bool = False
        self.error: str = ""
        self.view: dict[str, Any] = {"switches": [], "metrics": [], "phases": []}
        self.spec: dict[str, Any] = {}
        self.spec_fetched_at: float = 0.0
        self.polls: int = 0
        self.failures: int = 0
        self.started_at: float = time.time()
        self.backoff: float = 0.0

        # Automatik
        self.last_decision: dict[str, Any] = {}
        self.last_action_ts: float = 0.0
        self.last_action: str = ""
        self.off_since: float | None = None
        self.on_since: float | None = None

        # Schaltzustand nachhalten, um fremde Eingriffe zu erkennen
        self.last_seen: bool | None = None
        self.expected_state: bool | None = None
        self.block: set[str] = set()   # laufender Block im Modus "am Stueck"

        self.last_record_ts: float = 0.0
        self.online: bool | None = None
        self.offline_since: float | None = None
        self.kanal: str = ""     # lokal | cloud

    # Preise sind gemeinsam; als Eigenschaft gelesen, damit jeder
    # Geraetezustand denselben Stand sieht.
    @property
    def prices(self) -> dict[str, Any]:
        return preise.data

    @property
    def prices_ts(self) -> float:
        return preise.ts

    @property
    def price_error(self) -> str:
        return preise.error

    @property
    def auto(self) -> dict[str, Any]:
        """Die gemeinsame Regel, angewandt auf dieses Geraet.

        Die Regel selbst gilt fuer alle. Vom Geraet kommen nur zwei Dinge
        dazu: ob es mitmacht und welcher Ausgang geschaltet wird.
        """
        eintrag = geraete.holen(self.device_id) or {}
        auto = automation.settings(config.get("automation"))
        # Der Ausgang gehoert zum Geraet -- ausschliesslich. Ein gemeinsamer
        # Rueckfallwert waere fuer neue Geraete schlicht geraten: Was beim
        # Zaehler "switch_1" heisst, heisst bei der naechsten Steckdose
        # vielleicht "switch". Der Start raeumt den Altwert in die Geraete,
        # danach bleibt "switch" als neutrale Vorgabe, bis die erste
        # Rueckmeldung den echten Namen liefert.
        auto["switch_code"] = eintrag.get("switch_code") or "switch"
        auto["mitmachen"] = bool(eintrag.get("automatik_aktiv", True))
        auto["enabled"] = bool(auto["enabled"] and auto["mitmachen"])
        return auto

    def label(self) -> str:
        """Kurzname fuers Protokoll — Name, sonst die halbe Kennung."""
        return self.name or self.device_id[:8] or "?"

    def switch_value(self, code: str) -> bool | None:
        for sw in self.view.get("switches", []):
            if sw["code"] == code and sw.get("present"):
                return bool(sw["value"])
        return None

    def as_dict(self) -> dict[str, Any]:
        auto = self.auto
        price_cfg = prices.settings(config.get("price"))
        override_until = geraete.handbetrieb_bis(self.device_id)
        return {
            "ts": self.ts,
            "age_seconds": round(time.time() - self.ts, 1) if self.ts else None,
            "ok": self.ok,
            "error": self.error,
            "device_id": self.device_id,
            "device_name": self.name,
            "online": self.online,
            "kanal": self.kanal,
            "offline_minutes": round((time.time() - self.offline_since) / 60)
            if self.offline_since
            else 0,
            "refresh_seconds": config.get("refresh_seconds", 180),
            "calls_per_month": api_calls_per_month(
                int(config.get("refresh_seconds", 180) or 180)
            ),
            "trial_call_limit": TRIAL_CALLS_PER_MONTH,
            "polls": self.polls,
            "failures": self.failures,
            "uptime_seconds": round(time.time() - self.started_at),
            "version": VERSION,
            "build_date": BUILD_DATE,
            **self.view,
            "price": {
                "source": price_cfg["source"],
                "source_label": prices.SOURCES.get(price_cfg["source"], {}).get("label", ""),
                "is_spot": prices.is_spot(price_cfg["source"]),
                "spot_ct": round((self.prices.get("current", {}).get("spot") or 0) * 100, 2)
                if self.prices.get("current", {}).get("spot") is not None
                else None,
                "current": self.prices.get("current", {}),
                "ct": round((self.prices.get("current", {}).get("total") or 0) * 100, 2)
                if self.prices.get("current")
                else None,
                "level": self.prices.get("current", {}).get("level"),
                "level_label": LEVEL_LABELS.get(
                    self.prices.get("current", {}).get("level", ""), ""
                ),
                "age_seconds": round(time.time() - self.prices_ts, 1) if self.prices_ts else None,
                "error": self.price_error,
                "currency": self.prices.get("currency", "EUR"),
                "einheit": "ct/kWh" if self.prices.get("currency", "EUR") == "EUR"
                           else f"{self.prices.get('currency')}-Cent/kWh",
                # Dieselbe Vorschau wie auf der Preisseite -- samt Markierung,
                # in welcher Stunde die Regel dieses Geraet einschalten wuerde.
                # Vorher zeigte die Uebersicht nur Preise: gleiche Balken,
                # weniger Aussage, und zwei Ansichten, die sich widersprachen.
                "upcoming": automation.schedule_preview(self.prices, auto, hours=12)[:12]
                if self.prices
                else [],
            },
            "trial": trial_status(),
            "automation": {
                **auto,
                # `enabled` ist der zusammengefasste Wert: wird DIESES Geraet
                # automatisch geschaltet? Die Oberflaeche braucht daneben die
                # beiden Ursachen einzeln -- sonst schickt sie bei einem
                # ausgenommenen Geraet auf die Regelseite, wo alles stimmt.
                "regel_aktiv": bool(automation.settings(config.get("automation"))["enabled"]),
                "mode_label": automation.MODE_LABELS.get(auto["mode"], auto["mode"]),
                "decision": self.last_decision,
                "last_action": self.last_action,
                "last_action_ts": self.last_action_ts,
                "override_until": override_until,
                "override_active": override_until > time.time(),
                "override_remaining_minutes": max(0, round((override_until - time.time()) / 60))
                if override_until > time.time()
                else 0,
            },
        }


_states: dict[str, DeviceState] = {}


def zustand(device_id: str | None = None) -> DeviceState:
    """Zustand eines Geraets; ohne Angabe der des ersten.

    Wer die App mit einem Geraet betreibt, merkt von der Liste nichts -- alle
    Ansichten fallen auf dieses eine zurueck.
    """
    eintrag = geraete.aufloesen(device_id)
    gid = (eintrag or {}).get("id", "")
    vorhanden = _states.get(gid)
    if vorhanden is None:
        vorhanden = DeviceState(gid, (eintrag or {}).get("name", ""))
        _states[gid] = vorhanden
    elif eintrag and vorhanden.name != eintrag.get("name", ""):
        vorhanden.name = eintrag.get("name", "")
    return vorhanden


def alle_zustaende(nur_aktive: bool = False) -> list[DeviceState]:
    quelle = geraete.aktive() if nur_aktive else geraete.liste()
    return [zustand(e["id"]) for e in quelle]


def vergessen(device_id: str) -> None:
    _states.pop(device_id, None)
    _locals.pop(device_id, None)
    _sharings.pop(device_id, None)


_client: TuyaClient | None = None


def client() -> TuyaClient:
    global _client
    tuya = config.tuya
    if not (tuya.get("client_id") and tuya.get("client_secret")):
        raise HTTPException(status_code=400, detail="Tuya-Zugangsdaten fehlen noch")
    if (
        _client is None
        or _client.client_id != tuya["client_id"]
        or _client.client_secret != tuya["client_secret"]
        or _client.region != tuya.get("region", "eu")
    ):
        _client = TuyaClient(
            client_id=tuya["client_id"],
            client_secret=tuya["client_secret"],
            region=tuya.get("region", "eu"),
        )
    return _client


def reset_client() -> None:
    global _client
    _client = None


_locals: dict[str, local.LocalDevice] = {}


def local_device(device_id: str = "") -> local.LocalDevice | None:
    """Lokaler Zugang eines Geraets, sofern eingerichtet."""
    eintrag = geraete.aufloesen(device_id)
    if not eintrag:
        return None
    gid = eintrag["id"]
    cfg = eintrag["local"]
    if not (cfg.get("enabled") and cfg.get("ip") and cfg.get("key")):
        _locals.pop(gid, None)
        return None
    vorhanden = _locals.get(gid)
    if (
        vorhanden is None
        or vorhanden.ip != cfg["ip"]
        or vorhanden.local_key != cfg["key"]
    ):
        vorhanden = local.LocalDevice(
            device_id=gid,
            ip=cfg["ip"],
            local_key=cfg["key"],
            dp_map=cfg.get("dp_map") or {},
            version=cfg.get("version") or None,
        )
        _locals[gid] = vorhanden
    return vorhanden


def reset_local(device_id: str = "") -> None:
    if device_id:
        _locals.pop(device_id, None)
    else:
        _locals.clear()


_sharings: dict[str, sharing.SharingDevice] = {}


def sharing_device(device_id: str = "") -> sharing.SharingDevice | None:
    """Zugang ueber die QR-Anmeldung, sofern eingerichtet.

    Der zweite der drei Wege: kein Entwicklerprojekt, keine Frist. Er liefert
    ausserdem den lokalen Schluessel fuer Weg 1.

    Das SDK erneuert das Token selbsttaetig; damit die Erneuerung einen Neustart
    ueberlebt, wird sie ueber `token_ablegen` in die Konfiguration
    zurueckgeschrieben.
    """
    cfg = config.get("sharing") or {}
    if not (cfg.get("enabled") and cfg.get("user_code") and cfg.get("token")):
        return None
    eintrag = geraete.aufloesen(device_id)
    gid = (eintrag or {}).get("id", "")
    if not gid:
        return None
    vorhanden = _sharings.get(gid)
    if vorhanden is not None:
        return vorhanden

    def token_ablegen(neu: dict[str, Any]) -> None:
        aktuell = dict(config.get("sharing") or {})
        aktuell["token"] = neu
        config.set("sharing", aktuell)
        config.save()

    try:
        _sharings[gid] = sharing.SharingDevice(
            token_info=cfg.get("token") or {},
            user_code=cfg.get("user_code", ""),
            device_id=gid,
            client_id=cfg.get("client_id", ""),
            schema=cfg.get("schema", ""),
            token_ablegen=token_ablegen,
        )
    except Exception as exc:
        # Kein Grund, den ganzen Abruf scheitern zu lassen — es gibt zwei
        # weitere Wege. Nur vermerken, damit der Grund nachvollziehbar ist.
        log.warning("QR-Zugang nicht nutzbar: %s", exc)
        return None
    return _sharings[gid]


def reset_sharing(device_id: str = "") -> None:
    """Die Anmeldung gilt fuer das ganze Konto -- deshalb standardmaessig alle."""
    if device_id:
        _sharings.pop(device_id, None)
    else:
        _sharings.clear()


async def einrichten_lokal(ip: str, device_id: str = "") -> tuple[bool, str]:
    """Lokalen Zugang einrichten: Schluessel und Datenpunkt-Zuordnung beschaffen.

    Der Schluessel kommt bevorzugt aus der QR-Anmeldung — die ist unbefristet.
    Nur wenn es die nicht gibt, wird das Entwicklerprojekt bemueht. Damit laesst
    sich der lokale Weg einrichten, ohne je ein Projekt anzulegen.
    """
    eintrag = geraete.aufloesen(device_id)
    if not eintrag:
        return False, "Kein Gerät ausgewählt"
    device_id = eintrag["id"]
    ip = ip.strip()
    if not ip:
        return False, "Keine Adresse angegeben"

    key = ""
    benannt: dict[str, Any] = {}
    dp_map: dict[str, str] = {}
    quelle = ""

    # 1) QR-Anmeldung — ohne Frist, deshalb zuerst
    sd = sharing_device(device_id)
    if sd:
        try:
            geraet = await asyncio.to_thread(sd._aktualisieren)
            key = getattr(geraet, "local_key", "") or ""
            benannt = dict(geraet.status or {})
            quelle = "QR-Anmeldung"
        except Exception as exc:
            log.info("QR-Anmeldung liefert keinen Schluessel (%s)", exc)

    # 2) Entwicklerprojekt als Rueckfall
    if not key:
        try:
            api = client()
            key = await api.local_key(device_id)
            spec = await api.device_spec_with_dp(device_id)
            for bereich in ("status", "functions"):
                for eintrag in spec.get(bereich, []):
                    if eintrag.get("dp_id") is not None:
                        dp_map[str(eintrag["dp_id"])] = eintrag["code"]
            try:
                dp_map.update(await api.device_model(device_id))
            except Exception:
                pass
            quelle = quelle or "Entwicklerprojekt"
        except Exception as exc:
            return False, f"Kein Schlüssel zu bekommen: {exc}"

    if not key:
        return False, "Es wurde kein lokaler Schlüssel herausgegeben"

    # Verbindung aufbauen und dabei die Protokollversion ermitteln
    pruef = local.LocalDevice(device_id, ip, key, dp_map or dict(local.STANDARD_DP_MAP))
    try:
        roh = await asyncio.to_thread(pruef._status_roh)
    except Exception as exc:
        return False, f"Gerät unter {ip} nicht erreichbar: {exc}"

    # Zuordnung: liegt keine offizielle vor, aus dem Wertevergleich gewinnen
    if not dp_map:
        if benannt:
            dp_map = local.dp_map_aus_vergleich(benannt, roh)
        for dp, code in local.STANDARD_DP_MAP.items():
            dp_map.setdefault(dp, code)

    geraete.aktualisieren(device_id, local={
        "enabled": True, "ip": ip, "key": key,
        "dp_map": dp_map, "version": pruef.version or 0,
    })
    reset_local(device_id)
    store.log_event(
        "info",
        f"Lokaler Zugang eingerichtet ({ip}, Protokoll {pruef.version}, Schlüssel via {quelle})",
        device=device_id,
    )
    return True, (
        f"Verbunden — {len(roh)} Datenpunkte, Protokoll {pruef.version}, "
        f"Schlüssel über {quelle}"
    )


def tibber_client() -> TibberClient:
    return TibberClient(token=(config.get("tibber") or {}).get("token", ""))


# --------------------------------------------------------------------- Poller


async def poll_device(st: DeviceState | None = None) -> None:
    st = st or zustand()
    device_id = st.device_id
    if not device_id:
        return
    eintrag = geraete.holen(device_id) or {}

    # Lokal zuerst: schneller, ohne Kontingent, ohne Frist.
    schnappschuss = None
    ld = local_device(device_id)
    if ld:
        try:
            werte = await ld.status()
            schnappschuss = {"online": True, "status": werte, "name": ""}
            st.kanal = "lokal"
        except Exception as exc:
            st.kanal = ""
            if not eintrag.get("local", {}).get("fallback_cloud", True):
                raise
            log.warning("[%s] Lokal nicht erreichbar (%s) — weiche aus", st.label(), exc)
            store.log_event("warn", f"Lokal nicht erreichbar: {str(exc)[:120]}", device=device_id)

    if schnappschuss is None:
        sd = sharing_device(device_id)
        if sd:
            try:
                werte = await sd.status()
                schnappschuss = {"online": await sd.online(), "status": werte, "name": ""}
                st.kanal = "qr"
            except Exception as exc:
                log.warning("[%s] QR-Zugang nicht nutzbar (%s)", st.label(), exc)

    if schnappschuss is None:
        api = client()
        # Die Spezifikation aendert sich praktisch nie -> hoechstens stuendlich neu.
        if not st.spec or time.time() - st.spec_fetched_at > 3600:
            st.spec = await api.device_spec(device_id)
            st.spec_fetched_at = time.time()
        schnappschuss = await api.device_snapshot(device_id)
        st.kanal = "cloud"

    # Ueber die kontingentfreien Wege lohnt die Spezifikation ebenfalls, damit
    # die Messwerte Namen und Einheiten bekommen -- aber nur, wenn sie ohnehin
    # zu haben ist. Ohne Entwicklerprojekt bleibt sie leer, und build_view
    # arbeitet dann mit den Rohcodes weiter.
    if not st.spec and time.time() - st.spec_fetched_at > 3600:
        st.spec_fetched_at = time.time()
        try:
            st.spec = await client().device_spec(device_id)
        except Exception:
            st.spec = {}

    st.view = build_view(st.spec, schnappschuss["status"])
    st.ts = time.time()
    st.ok = True
    st.error = ""
    st.polls += 1

    war_online = st.online
    st.online = schnappschuss["online"]
    if not st.online:
        if st.offline_since is None:
            st.offline_since = time.time()
            store.log_event("warn", "Gerät meldet sich nicht mehr (offline)", device=device_id)
            log.warning("[%s] Geraet ist offline", st.label())
    else:
        if war_online is False:
            store.log_event("info", "Gerät ist wieder online", device=device_id)
            log.info("[%s] Geraet ist wieder online", st.label())
        st.offline_since = None

    # Solange das Geraet offline ist, liefert die Cloud unveraendert alte Werte.
    # Die aufzuzeichnen wuerde eine Messreihe erzeugen, die es nie gegeben hat.
    history_seconds = max(0, int(config.get("history_seconds", HISTORY_SECONDS_DEFAULT) or 0))
    if (
        st.online
        and history_seconds
        and eintrag.get("aufzeichnen", True)
        and time.time() - st.last_record_ts >= history_seconds
    ):
        store.record(st.view["metrics"], st.view["phases"], device=device_id)
        st.last_record_ts = time.time()

    # Aus-Zeit mitschreiben, damit das Sicherheitsnetz greifen kann.
    auto = st.auto
    current = st.switch_value(auto["switch_code"])
    if current is False:
        if st.off_since is None:
            st.off_since = time.time()
        st.on_since = None
    elif current is True:
        if st.on_since is None:
            st.on_since = time.time()
        st.off_since = None

    if current is None:
        # Der eingestellte Kanal existiert an diesem Geraet nicht -- der
        # Normalfall, solange keiner erkannt wurde: Der Platzhalter heisst
        # "switch", viele Geraete nennen ihren Ausgang aber "switch_1".
        #
        # Welcher es ist, kann die App selbst entscheiden: Das Geraet meldet
        # seine schaltbaren Ausgaenge, und mehr als deren Namen gibt es nicht
        # zu wissen. Frueher griff die Korrektur nur bei genau einem Ausgang;
        # eine Mehrfachsteckdose blieb damit stumm, obwohl die Antwort
        # ("nimm den ersten") auf der Hand lag.
        vorhandene = [sw["code"] for sw in st.view.get("switches", []) if sw.get("present")]
        if vorhandene and auto["switch_code"] not in vorhandene:
            gewaehlt = vorhandene[0]
            auto["switch_code"] = gewaehlt
            geraete.aktualisieren(device_id, switch_code=gewaehlt)
            log.info("[%s] Schaltkanal erkannt: '%s'%s", st.label(), gewaehlt,
                     f" (von {len(vorhandene)} Ausgaengen)" if len(vorhandene) > 1 else "")
            store.log_event(
                "info",
                f"Schaltkanal erkannt: '{gewaehlt}'"
                + (f" — das Gerät meldet {len(vorhandene)}: {', '.join(vorhandene)}"
                   if len(vorhandene) > 1 else ""),
                device=device_id,
            )
            current = st.switch_value(auto["switch_code"])

    if current is not None:
        note_switch_state(st, current, auto)


def note_switch_state(st: DeviceState, current: bool, auto: dict[str, Any]) -> None:
    """Erkennen, ob jemand anders geschaltet hat — etwa in der Smart-Life-App.

    Ohne das wuerde die Automatik eine Handbedienung am Geraet oder im Handy
    binnen Sekunden zurueckdrehen. Ein Wechsel, den wir nicht selbst ausgeloest
    haben, zaehlt deshalb genauso als Handbetrieb wie ein Klick in dieser
    Oberflaeche.
    """
    vorher = st.last_seen
    st.last_seen = current

    if vorher is None or vorher == current:
        return  # erster Messwert oder keine Aenderung

    if st.expected_state is not None and current == st.expected_state:
        st.expected_state = None
        return  # das waren wir selbst

    # Ab hier: der Zustand hat sich geaendert, ohne dass wir es veranlasst haben.
    st.expected_state = None
    wort = "ein" if current else "aus"
    store.log_event(
        "switch", f"Von aussen geschaltet: {auto['switch_code']} = {wort}", device=st.device_id
    )
    log.info("[%s] Fremdschaltung erkannt: %s = %s", st.label(), auto["switch_code"], wort)

    if auto["enabled"] and auto["override_minutes"]:
        geraete.handbetrieb_setzen(st.device_id, time.time() + auto["override_minutes"] * 60)
        st.last_action = (
            f"{wort.upper()} — von Hand geschaltet (nicht über diese Oberfläche), "
            f"Automatik pausiert {auto['override_minutes']} min"
        )
        st.last_action_ts = time.time()


async def schalten(st: DeviceState, code: str, wert: Any) -> None:
    """Ueber den Kanal schalten, der zuletzt gelesen hat.

    So bleibt Lesen und Schreiben auf demselben Weg — sonst koennte die App
    lokal lesen und ueber die Cloud schalten, was bei Netzproblemen zu
    widerspruechlichen Zustaenden fuehrt.
    """
    if st.kanal == "lokal":
        ld = local_device(st.device_id)
        if ld:
            await ld.send_commands([{"code": code, "value": wert}])
            return
    if st.kanal == "qr":
        sd = sharing_device(st.device_id)
        if sd:
            await sd.send_commands([{"code": code, "value": wert}])
            return
    await client().send_commands(st.device_id, [{"code": code, "value": wert}])


async def poll_prices(force: bool = False) -> None:
    price_cfg = prices.settings(config.get("price"))
    if price_cfg["source"] == "tibber":
        tibber = config.get("tibber") or {}
        if not (tibber.get("token") and tibber.get("home_id")):
            return
    if not force and time.time() - preise.ts < PRICE_REFRESH_SECONDS:
        return
    try:
        preise.data = await prices.fetch(price_cfg, config.get("tibber") or {})
        preise.ts = time.time()
        preise.error = ""
    except Exception as exc:
        preise.error = str(exc)
        log.warning("Preisabruf (%s) fehlgeschlagen: %s", price_cfg["source"], exc)


async def apply_automation(st: DeviceState | None = None) -> None:
    """Regel eines Geraets auswerten und bei Bedarf schalten."""
    st = st or zustand()
    if not st.device_id:
        return
    auto = st.auto
    if not auto["enabled"]:
        grund = ("Für dieses Gerät ist die Automatik abgeschaltet"
                 if not auto.get("mitmachen") else "Automatik ist aus")
        st.last_decision = {"desired": None, "reason": grund, "price_ct": None}
        return

    override_until = geraete.handbetrieb_bis(st.device_id)
    if override_until > time.time():
        remaining = round((override_until - time.time()) / 60)
        st.last_decision = {
            "desired": None,
            "reason": f"Handbetrieb aktiv, Automatik pausiert (noch {remaining} min)",
            "price_ct": None,
        }
        return

    if st.online is False:
        dauer = ""
        if st.offline_since:
            dauer = f" (seit {round((time.time() - st.offline_since) / 60)} min)"
        st.last_decision = {
            "desired": None,
            "reason": f"Gerät ist nicht erreichbar{dauer} — es wird nicht geschaltet",
            "price_ct": None,
        }
        return

    if not preise.data:
        st.last_decision = {
            "desired": None,
            "reason": preise.error or "Noch keine Strompreise abgerufen",
            "price_ct": None,
        }
        return

    decision = automation.decide(
        preise.data, auto, dt.datetime.now(dt.timezone.utc).astimezone(),
        off_since=st.off_since, block=st.block,
    )
    if decision.block:
        st.block = decision.block
    st.last_decision = decision.as_dict()
    if decision.desired is None:
        return

    code = auto["switch_code"]
    current = st.switch_value(code)
    if current is None:
        st.last_decision["reason"] += f" — Schaltkanal '{code}' meldet keinen Zustand"
        return
    if current == decision.desired:
        return

    # Flatterschutz: nach dem Ausschalten eine Mindestzeit warten.
    if (
        decision.desired is True
        and auto["min_off_minutes"]
        and st.off_since
        and (time.time() - st.off_since) < auto["min_off_minutes"] * 60
    ):
        st.last_decision["reason"] += " — Mindest-Aus-Zeit noch nicht erreicht"
        return

    # Mindestlaufzeit: einmal Eingeschaltetes eine Weile laufen lassen - fuer
    # Verbraucher, die eine kurze Unterbrechung nicht gut vertragen.
    if (
        decision.desired is False
        and auto["min_on_minutes"]
        and st.on_since
        and (time.time() - st.on_since) < auto["min_on_minutes"] * 60
    ):
        rest = round(auto["min_on_minutes"] - (time.time() - st.on_since) / 60)
        st.last_decision["reason"] += f" — Mindestlaufzeit läuft noch ({rest} min)"
        return

    try:
        await schalten(st, code, decision.desired)
    except Exception as exc:
        store.log_event("error", f"Automatik konnte nicht schalten: {exc}", device=st.device_id)
        log.warning("[%s] Automatik-Schaltbefehl fehlgeschlagen: %s", st.label(), exc)
        return

    st.expected_state = decision.desired  # damit der naechste Poll uns nicht fuer fremd haelt
    st.last_action = f"{'EIN' if decision.desired else 'AUS'} — {decision.reason}"
    st.last_action_ts = time.time()
    store.log_event(
        "switch",
        f"Automatik: {code} = {'ein' if decision.desired else 'aus'} ({decision.reason})",
        device=st.device_id,
    )
    log.info("[%s] Automatik schaltet %s: %s", st.label(),
             "EIN" if decision.desired else "AUS", decision.reason)
    await asyncio.sleep(1)
    try:
        await poll_device(st)
    except Exception:
        pass


async def durchlauf(st: DeviceState, interval: int) -> None:
    """Ein Geraet abfragen und seine Regel anwenden.

    Fehler bleiben beim Geraet: faellt eines aus, laufen die anderen weiter.
    Das ist der Grund fuer den eigenen Backoff je Geraet -- ein totes Geraet
    darf die Automatik der uebrigen nicht ausbremsen.
    """
    if st.backoff and time.time() < st.backoff:
        return
    try:
        await poll_device(st)
        if st.backoff:
            store.log_event("info", "Verbindung wieder da", device=st.device_id)
        st.backoff = 0.0
    except Exception as exc:
        st.ok = False
        if isinstance(exc, TuyaError):
            st.error = tuya_error_hint(
                exc, config.tuya.get("client_id", ""), "", geraet=st.label()
            )
        else:
            st.error = str(exc)
        st.failures += 1
        if not st.backoff:
            store.log_event("error", str(exc), device=st.device_id)
            log.warning("[%s] Poll fehlgeschlagen: %s", st.label(), exc)
        st.backoff = time.time() + min(max(interval, 30) * 2, 300)
        return

    try:
        await apply_automation(st)
    except Exception as exc:
        log.warning("[%s] Automatik-Durchlauf fehlgeschlagen: %s", st.label(), exc)


async def poller() -> None:
    """Laeuft dauerhaft, unabhaengig von geoeffneten Browser-Tabs."""
    last_prune = 0.0
    while True:
        interval = int(config.get("refresh_seconds", 180) or 180)
        interval = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))

        if config.setup_done and geraete.aktive():
            await poll_prices()
            for st in alle_zustaende(nur_aktive=True):
                await durchlauf(st, interval)

        now = time.time()
        if now - last_prune > 6 * 3600:
            last_prune = now
            try:
                removed = store.prune()
                if removed:
                    log.info("Historie aufgeraeumt: %d alte Messwerte entfernt", removed)
            except Exception as exc:
                log.warning("Aufraeumen der Historie fehlgeschlagen: %s", exc)

        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Erst den Geraetebestand herstellen, dann die Datenbank: die Migration der
    # Historie braucht die Kennung des bisherigen Geraets, um die vorhandenen
    # Messwerte zuzuordnen.
    if geraete.migrieren():
        log.info("Geraeteliste aus der bisherigen Einzelkonfiguration angelegt")
    erstes = geraete.primaer()
    store.init(erstes_geraet=(erstes or {}).get("id", ""))
    config.ensure_api_token()

    # Wer die App eingerichtet hat, bevor es diese Ueberwachung gab, haette nie
    # eine Warnung bekommen - der Startzeitpunkt fehlte einfach. Dann ab jetzt
    # zaehlen: ungenau, aber ungleich besser als eine Frist, die stillschweigend
    # ablaeuft.
    if config.setup_done and not config.get("tuya_setup_ts"):
        config.set("tuya_setup_ts", time.time())
        config.save()
        log.info("Startzeitpunkt fuer die Testzeitraum-Ueberwachung nachgetragen")
        store.log_event(
            "info",
            "Überwachung des Tuya-Testzeitraums beginnt ab heute "
            "(tatsaechlicher Projektstart unbekannt)",
        )
    task = asyncio.create_task(poller(), name="tuya-poller")
    log.info("Poller gestartet (Intervall %ss)", config.get("refresh_seconds", 180))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Tuya Smartmeter", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.session_secret(),
    session_cookie="tuya_session",
    max_age=30 * 86400,
    same_site="lax",
)


# ----------------------------------------------------------------------- Auth


def logged_in(request: Request) -> bool:
    return bool(request.session.get("user"))


def require_login(request: Request) -> None:
    if not logged_in(request):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")


def require_api_access(request: Request) -> None:
    """Session-Cookie ODER API-Token (fuer ioBroker, Zabbix, Skripte)."""
    if logged_in(request):
        return
    token = request.headers.get("x-api-token") or request.query_params.get("token")
    if token and config.get("api_token") and token == config.get("api_token"):
        return
    raise HTTPException(status_code=401, detail="Nicht angemeldet")


def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    """Eine Seite ausliefern -- mit allem, worauf das Grundgeruest zugreift.

    Die gemeinsamen Werte stehen hier und nicht in jeder Route einzeln. Fehlte
    einer, lieferte Jinja stillschweigend nichts: kein Fehler, nur eine
    Kopfzeile ohne Namen oder ein Haekchen, das nie gesetzt erscheint. Genau
    daran lag der Fehler, dass sich "folgt der Regel" nicht umlegen liess.
    """
    grund: dict[str, Any] = {
        "cfg": config,
        "show_nav": True,
        "version": VERSION,
        "build_date": BUILD_DATE,
        "git_commit": GIT_COMMIT,
        "geraet": None,          # gesetzt auf geraetebezogenen Seiten
        "geraete_liste": [],     # fuer den Umschalter im Kopf
    }
    grund.update(ctx)
    return TEMPLATES.TemplateResponse(request, name, grund)


# Die Verlaengerung ist bei Tuya ein Antrag, kein Klick - laut Support dauert
# die Bearbeitung bis zu einen Werktag. Deshalb nicht auf den letzten Druecker
# erinnern, sondern mit Puffer.
TRIAL_VORWARNUNG_TAGE = 10


def api_calls_per_month(interval_seconds: int, geraetezahl: int | None = None) -> int:
    """Hochrechnung des Tuya-Verbrauchs: Abfragen plus Spezifikation und Token.

    Jedes Geraet, das ueber die Cloud laeuft, zaehlt einzeln. Lokal oder per
    QR angebundene Geraete kosten nichts -- deshalb wird nur gezaehlt, was
    tatsaechlich am Entwicklerprojekt haengt.
    """
    if interval_seconds <= 0:
        return 0
    if geraetezahl is None:
        geraetezahl = max(1, sum(1 for st in _states.values() if st.kanal == "cloud")) \
            if _states else 1
    pro_tag = (86400 / interval_seconds + 24) * geraetezahl + 12
    return int(pro_tag * 30)


def _cloud_rechte_weg() -> bool:
    """Ob der Cloud-Zugang als Ganzes keine Rechte mehr hat.

    Ohne eingetragenes Ablaufdatum bleibt nur der Rueckschluss aus Fehlern --
    aber vorsichtig: Code 1106 sagt lediglich "keine Berechtigung" und steht
    genauso an einem Geraet, das nicht zum Projekt gehoert oder das es gar
    nicht gibt. Daraus auf einen abgelaufenen Testzeitraum zu schliessen, waere
    bei mehreren Geraeten regelmaessig falsch.

    Deshalb nur, wenn wirklich nichts mehr geht: Kein Geraet liest ueber die
    Cloud, und jedes, das ueberhaupt einen Fehler meldet, meldet einen
    Rechtefehler.
    """
    zustaende = [st for st in _states.values() if st.device_id]
    if any(st.kanal == "cloud" and st.ok for st in zustaende):
        return False        # ueber die Cloud kommen Daten -- die Rechte stehen
    mit_fehler = [st for st in zustaende if st.error]
    if not mit_fehler:
        return False
    return all(("1106" in st.error or "1114" in st.error) for st in mit_fehler)


def trial_status() -> dict[str, Any]:
    """Erinnerung an den ablaufenden Tuya-Testzeitraum.

    Zwei Betriebsarten. Wer das Ablaufdatum aus seinem Tuya-Projekt eintraegt,
    bekommt eine exakte Warnung. Ohne Datum zaehlen wir ab dem Tag, an dem die
    Zugangsdaten zuletzt bestaetigt wurden - ungenau, aber besser als eine Frist,
    die stillschweigend ablaeuft. Die API verraet das Datum nicht.
    """
    datum = (config.get("trial_expires") or "").strip()
    if datum:
        try:
            ablauf = dt.date.fromisoformat(datum)
        except ValueError:
            ablauf = None
        if ablauf:
            # Steht ein Ablaufdatum fest, entscheidet es allein. Ein Fehlercode
            # darf es nicht ueberstimmen: 1106 heisst nur "keine Berechtigung"
            # und trifft genauso auf ein Geraet zu, das gar nicht zum Projekt
            # gehoert. Sonst behauptet die App einen abgelaufenen Testzeitraum,
            # waehrend noch Wochen davon uebrig sind.
            rest = (ablauf - dt.date.today()).days
            return {
                "known": True,
                "exact": True,
                "expires": datum,
                "days_left": rest,
                "warn": rest <= TRIAL_VORWARNUNG_TAGE,
                "expired": rest < 0,
            }

    abgelaufen_laut_fehler = _cloud_rechte_weg()

    seit = float(config.get("tuya_setup_ts") or 0)
    if not seit:
        return {"known": False, "exact": False, "days": 0, "warn": False, "expired": False}

    tage = (time.time() - seit) / 86400
    grenze = int(config.get("trial_reminder_days", 25) or 25)
    return {
        "known": True,
        "exact": False,
        "days": int(tage),
        "days_until_reminder": max(0, grenze - int(tage)),
        "warn": tage >= grenze,
        "expired": abgelaufen_laut_fehler,
    }


def tuya_error_hint(exc: TuyaError, client_id: str, client_secret: str,
                    geraet: str = "") -> str:
    """Aus dem Tuya-Fehlercode eine Meldung machen, mit der man etwas anfangen kann.

    Die Codes sind aussagekraeftig, aber Tuyas Klartext ist es nicht - "sign
    invalid" klingt nach Programmfehler, ist aber fast immer ein unvollstaendig
    kopiertes Secret.
    """
    cid = (client_id or "").strip()
    sec = (client_secret or "").strip()
    code = exc.code

    if code == 1004:  # sign invalid
        hint = (
            "Die Access ID wurde erkannt, aber das Access Secret passt nicht dazu. "
            "Fast immer liegt es am Kopieren: In der Tuya-Oberflaeche ist das Secret "
            "verborgen — erst auf das Augen-Symbol klicken, dann den sichtbaren Text "
            "vollständig markieren und kopieren."
        )
        if len(sec) != 32:
            hint += (
                f" Dein Secret ist {len(sec)} Zeichen lang; Tuya-Secrets haben "
                "normalerweise genau 32. Es fehlt also vermutlich etwas."
            )
        if len(cid) != 20:
            hint += (
                f" Auch die Access ID weicht ab ({len(cid)} statt der ueblichen 20 Zeichen) "
                "— stammen beide aus demselben Projekt?"
            )
        return f"Signatur abgelehnt (Code 1004). {hint}"

    if code == 2009:  # clientId is invalid
        return (
            "Die Access ID kennt Tuya nicht (Code 2009). Entweder ist sie vertippt, "
            "oder das Projekt liegt in einem anderen Rechenzentrum als hier ausgewählt. "
            "Im Tuya-Projekt unter Overview steht, welches es ist."
        )

    if code in (1106, 1114):  # no permissions
        # Den Testzeitraum nur erwaehnen, wenn er ueberhaupt in Frage kommt.
        # Steht ein Ablaufdatum fest und liegt es noch fern, schickt dieser Satz
        # nur auf eine falsche Faehrte -- der Fehler kommt dann von woanders.
        trial = trial_status()
        frist_plausibel = not (
            trial.get("exact") and trial.get("days_left", 0) > TRIAL_VORWARNUNG_TAGE
        )
        frist = (
            " Oder der Testzeitraum ist abgelaufen — dann dort unter "
            "Service → Extend Trial verlaengern."
            if frist_plausibel else ""
        )
        wen = f" fuer »{geraet}«" if geraet else ""

        if code == 1106:
            # Haeufigster Fall zuerst, und der ist geraetebezogen: Das Geraet
            # haengt nicht am Projekt. Frueher stand hier zuerst der
            # Projektfehler -- und wer ein Geraet falsch eingetragen hatte,
            # suchte den Fehler in den Projekteinstellungen.
            return (
                f"Keine Berechtigung{wen} (Code 1106). Meist gehört das Gerät nicht "
                "zum Projekt: auf iot.tuya.com unter Devices → Link App Account pruefen, "
                "ob das Smart-Life-Konto verknuepft ist und das Gerät dort auftaucht. "
                "Sonst fehlt im Projekt eine der APIs (IoT Core, Authorization, "
                f"Smart Home Scene Linkage).{frist}"
            )

        return (
            "Keine Berechtigung (Code 1114). Im Tuya-Projekt auf iot.tuya.com fehlt "
            "eine der APIs (IoT Core, Authorization, Smart Home Scene Linkage), oder "
            "der Testzeitraum ist abgelaufen — dann dort unter Service → Extend Trial "
            "verlängern."
        )

    return (
        f"Die Tuya-Cloud lehnt die Anfrage ab: {exc.msg} (Code {code}). "
        "Bitte Access ID, Access Secret und das Rechenzentrum pruefen."
    )


def zahl(wert: Any, standard: float = 0.0) -> float:
    """Eine Zahl aus einem Formularfeld lesen, ohne daran zu scheitern.

    Zwei Faelle, die sonst zum Programmabbruch fuehren: ein leeres oder
    unsinniges Feld -- und, viel haeufiger, das deutsche Komma. Wer "19,5"
    eintippt, hat nichts falsch gemacht; die App muss das lesen koennen.
    """
    if wert is None:
        return standard
    text = str(wert).strip().replace(",", ".")
    if not text:
        return standard
    try:
        ergebnis = float(text)
    except ValueError:
        return standard
    if ergebnis != ergebnis or ergebnis in (float("inf"), float("-inf")):
        return standard          # NaN und Unendlich sind keine Einstellungen
    return ergebnis


def ganzzahl(wert: Any, standard: int = 0) -> int:
    return int(zahl(wert, standard))


def sicheres_ziel(ziel: str, standard: str) -> str:
    """Nur zurueck in die eigene App weiterleiten.

    Ein Ziel aus dem Formular ist Eingabe von aussen. Ohne Pruefung liesse sich
    daraus eine Weiterleitung auf eine fremde Seite bauen -- "//example.com"
    sieht wie ein Pfad aus, ist fuer den Browser aber eine andere Domain.
    """
    ziel = (ziel or "").strip()
    if ziel.startswith("/") and not ziel.startswith("//"):
        return ziel
    return standard


def guard(request: Request):
    """Gemeinsamer Einstieg: Ersteinrichtung bzw. Anmeldung erzwingen."""
    if not config.setup_done:
        return RedirectResponse("/setup", status_code=303)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


# ---------------------------------------------------------------------- Setup


@app.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    if config.setup_done and not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return page(request, "setup.html", regions=ENDPOINTS, error=None, show_nav=False)


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    password: str = Form(...),
    password2: str = Form(...),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    region: str = Form("eu"),
):
    if config.setup_done and not logged_in(request):
        return RedirectResponse("/login", status_code=303)

    def fail(msg: str):
        # Eingaben zurueckgeben, damit bei einem Fehler nicht alles neu getippt
        # werden muss. Passwoerter bleiben absichtlich leer.
        return page(
            request,
            "setup.html",
            regions=ENDPOINTS,
            error=msg,
            show_nav=False,
            client_id=client_id,
            region=region,
        )

    if len(password) < 8:
        return fail("Das Passwort muss mindestens 8 Zeichen haben.")
    if password != password2:
        return fail("Die beiden Passwörter stimmen nicht überein.")
    if region not in ENDPOINTS:
        return fail("Unbekannte Region.")

    # Zugangsdaten eines Entwicklerprojekts sind hier ausdruecklich freiwillig.
    # Der empfohlene Weg ist die QR-Anmeldung, und die braucht kein Projekt --
    # sie laesst sich aber erst nach dem Anmelden einrichten. Wer sie hier
    # verlangt, zwingt jeden durch eine zwanzigminuetige Registrierung, die
    # die Anleitung im selben Atemzug fuer ueberfluessig erklaert.
    mit_projekt = bool(client_id.strip() and client_secret.strip())
    if mit_projekt:
        config.set_tuya(client_id, client_secret, region)
        reset_client()
        try:
            await client().list_devices()
        except TuyaError as exc:
            return fail(tuya_error_hint(exc, client_id, client_secret))
        except Exception as exc:
            return fail(f"Keine Verbindung zur Tuya-Cloud: {exc}")
    elif client_id.strip() or client_secret.strip():
        return fail("Access ID und Access Secret gehören zusammen — bitte beide "
                    "eintragen oder beide leer lassen.")

    config.set_admin_password(password)
    config.set("setup_done", True)
    if mit_projekt:
        config.set("tuya_setup_ts", time.time())
    config.ensure_api_token()
    config.save()
    request.session["user"] = "admin"
    store.log_event(
        "info",
        "Ersteinrichtung abgeschlossen"
        + (" (mit Entwicklerprojekt)" if mit_projekt else " (ohne Entwicklerprojekt)"),
    )
    # Ohne Projekt gibt es noch keine Geraeteliste -- dann zuerst den Zugang.
    return RedirectResponse("/devices" if mit_projekt else "/zugang", status_code=303)


# ---------------------------------------------------------------------- Login


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if not config.setup_done:
        return RedirectResponse("/setup", status_code=303)
    return page(request, "login.html", error=None, show_nav=False)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if not config.setup_done:
        return RedirectResponse("/setup", status_code=303)
    if not config.check_admin_password(password):
        await asyncio.sleep(1)  # Bremse gegen Durchprobieren
        store.log_event("warn", "Fehlgeschlagener Anmeldeversuch")
        return page(request, "login.html", error="Passwort falsch.", show_nav=False)
    request.session["user"] = "admin"
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------- Uebersicht


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if (redirect := guard(request)) is not None:
        return redirect
    if not geraete.liste():
        return RedirectResponse("/devices", status_code=303)
    gewaehlt = zustand(request.query_params.get("device"))
    return page(
        request, "dashboard.html",
        state=gewaehlt.as_dict(),
        uebersicht=[st.as_dict() for st in alle_zustaende()],
        geraet=geraete.holen(gewaehlt.device_id) or {},
        geraete_liste=geraete.zusammenfassung(),
    )


@app.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request, saved: str = "", meldung: str = ""):
    """Geraete verwalten: uebernommene auflisten, verfuegbare anbieten.

    Die verfuegbaren kommen bevorzugt aus der QR-Anmeldung -- die ist
    unbefristet und zeigt alle Geraete des Kontos. Nur wenn es sie nicht gibt,
    wird das Entwicklerprojekt gefragt.
    """
    if (redirect := guard(request)) is not None:
        return redirect
    error = None
    devices: list[dict[str, Any]] = []
    quelle = ""

    sd = sharing_device()
    if sd:
        try:
            devices = await sd.geraete_liste()
            quelle = "QR-Anmeldung"
        except Exception as exc:
            log.info("Geraeteliste ueber die QR-Anmeldung nicht abrufbar: %s", exc)

    if not devices:
        try:
            devices = await client().list_devices()
            quelle = "Entwicklerprojekt"
        except TuyaError as exc:
            error = (
                f"{exc.msg} (Code {exc.code}). Ist das Smart-Life-Konto im Tuya-Projekt "
                "unter 'Devices → Link App Account' verknuepft?"
            )
        except Exception as exc:
            error = str(exc)

    uebernommen = {e["id"] for e in geraete.liste()}
    meine = []
    for eintrag in geraete.liste():
        st = zustand(eintrag["id"])
        # Die Schaltflaechen der Zeile zeigen den gespeicherten Stand des
        # Geraets, nicht den abgeleiteten aus dem Zustand -- sonst laesst sich
        # ein Haekchen nicht umlegen, weil die Anzeige es nie uebernimmt.
        meine.append({**st.as_dict(),
                      "aktiv": eintrag.get("aktiv", True),
                      "automatik_aktiv": eintrag.get("automatik_aktiv", True),
                      "switch_code": eintrag.get("switch_code", ""),
                      "gemeldete_schalter": [sw["code"] for sw in st.view.get("switches", [])
                                             if sw.get("present")],
                      "aufzeichnen": eintrag.get("aufzeichnen", True)})
    return page(
        request, "devices.html",
        devices=devices, error=error, quelle=quelle,
        meine=meine,
        geraete_liste=geraete.zusammenfassung(),
        regel_aktiv=automation.settings(config.get("automation"))["enabled"],
        regel_name=automation.MODE_LABELS.get(
            automation.settings(config.get("automation"))["mode"], ""
        ),
        uebernommen=uebernommen,
        saved=saved, meldung=meldung,
    )


@app.post("/devices")
async def devices_add(request: Request, device_id: str = Form(...), device_name: str = Form("")):
    """Ein Geraet in den Bestand aufnehmen."""
    require_login(request)
    device_id = device_id.strip()
    if not device_id:
        return RedirectResponse("/devices?saved=error&meldung=Keine+Kennung", 303)

    erstes = not geraete.liste()
    eintrag = geraete.hinzufuegen(device_id, device_name.strip() or device_id[:8])
    st = zustand(device_id)
    st.spec = {}
    st.spec_fetched_at = 0.0
    st.ts = 0.0
    store.log_event("info", f"Gerät aufgenommen: {eintrag['name']}", device=device_id)
    try:
        await poll_device(st)
    except Exception as exc:
        st.ok = False
        st.error = str(exc)

    # Beim ersten Geraet fehlt noch die Preisquelle — danach ist der Bestand
    # das Ziel, sonst landet man nach jedem Hinzufuegen wieder in der
    # Ersteinrichtung.
    return RedirectResponse("/preisquelle" if erstes else "/devices?saved=1", status_code=303)


@app.post("/devices/entfernen")
async def devices_remove(request: Request, device_id: str = Form(...)):
    """Ein Geraet aus dem Bestand nehmen. Die Messwerte bleiben erhalten."""
    require_login(request)
    eintrag = geraete.holen(device_id.strip())
    name = (eintrag or {}).get("name") or device_id[:8]
    if geraete.entfernen(device_id.strip()):
        vergessen(device_id.strip())
        store.log_event("info", f"Gerät entfernt: {name}")
        return RedirectResponse(f"/devices?saved=1&meldung=Entfernt:+{name.replace(' ', '+')}", 303)
    return RedirectResponse("/devices?saved=error&meldung=Nicht+gefunden", 303)


@app.post("/devices/umbenennen")
async def devices_rename(request: Request, device_id: str = Form(...), name: str = Form("")):
    require_login(request)
    geraete.aktualisieren(device_id.strip(), name=name.strip())
    st = _states.get(device_id.strip())
    if st:
        st.name = name.strip()
    return RedirectResponse("/devices?saved=1", 303)


@app.post("/devices/automatik")
async def devices_automation(request: Request, device_id: str = Form(...),
                             mitmachen: str = Form(""), ziel: str = Form("")):
    """Ob dieses Geraet der gemeinsamen Regel folgt."""
    require_login(request)
    gid = device_id.strip()
    geraete.aktualisieren(gid, automatik_aktiv=bool(mitmachen))
    st = _states.get(gid)
    if st:
        try:
            await apply_automation(st)
        except Exception as exc:
            log.warning("[%s] Automatik nach Umschalten fehlgeschlagen: %s", st.label(), exc)
    store.log_event(
        "info",
        "Folgt der Automatik" if mitmachen else "Wird nur noch von Hand geschaltet",
        device=gid,
    )
    return RedirectResponse(sicheres_ziel(ziel, "/devices?saved=1"), 303)


@app.post("/devices/aktiv")
async def devices_active(request: Request, device_id: str = Form(...), aktiv: str = Form("")):
    """Ein Geraet ruhen lassen oder wieder aufwecken."""
    require_login(request)
    gid = device_id.strip()
    geraete.aktualisieren(gid, aktiv=bool(aktiv))
    if not aktiv:
        vergessen(gid)
        store.log_event("info", "Gerät ruht — wird nicht mehr abgefragt", device=gid)
    else:
        store.log_event("info", "Gerät wieder aktiv", device=gid)
    return RedirectResponse("/devices?saved=1", 303)


@app.post("/devices/schaltkanal")
async def devices_switch_code(request: Request, device_id: str = Form(...),
                              switch_code: str = Form("")):
    """Welchen Ausgang dieses Geraet schaltet."""
    require_login(request)
    gid = device_id.strip()
    geraete.aktualisieren(gid, switch_code=switch_code.strip())
    store.log_event("info", f"Schaltkanal auf '{switch_code.strip()}' gesetzt", device=gid)
    st = _states.get(gid)
    if st:
        try:
            await apply_automation(st)
        except Exception as exc:
            log.warning("[%s] Automatik nach Kanalwechsel: %s", st.label(), exc)
    return RedirectResponse("/devices?saved=1", 303)


@app.post("/devices/aufzeichnen")
async def devices_record(request: Request, device_id: str = Form(...), aufzeichnen: str = Form("")):
    """Ob die Messwerte dieses Geraets in die Historie wandern."""
    require_login(request)
    geraete.aktualisieren(device_id.strip(), aufzeichnen=bool(aufzeichnen))
    return RedirectResponse("/devices?saved=1", 303)


# ------------------------------------------------------------ Gerätezugang


@app.get("/zugang", response_class=HTMLResponse)
async def zugang_seite(request: Request, saved: str = "", meldung: str = "", device: str = ""):
    if (redirect := guard(request)) is not None:
        return redirect
    eintrag = geraete.aufloesen(device) or {}
    st = zustand(eintrag.get("id"))
    return page(
        request,
        "zugang.html",
        lokal=eintrag.get("local") or {},
        qr=config.get("sharing") or {},
        kanal=st.kanal,
        geraet=eintrag,
        geraete_liste=geraete.zusammenfassung(),
        saved=saved,
        meldung=meldung,
    )


@app.post("/zugang/lokal")
async def zugang_lokal(request: Request, ip: str = Form(""), aktiv: str = Form(""),
                       device: str = Form("")):
    require_login(request)
    eintrag = geraete.aufloesen(device)
    if not eintrag:
        return RedirectResponse("/devices", 303)
    gid = eintrag["id"]
    anhang = f"&device={gid}"

    if not aktiv:
        geraete.aktualisieren(gid, local={"enabled": False})
        reset_local(gid)
        return RedirectResponse(
            f"/zugang?saved=1&meldung=Lokaler+Zugang+abgeschaltet{anhang}", 303
        )

    ok, text = await einrichten_lokal(ip, gid)
    return RedirectResponse(
        f"/zugang?saved={'1' if ok else 'error'}&meldung={text.replace(' ', '+')}{anhang}", 303
    )


@app.post("/zugang/lokal/suchen")
async def zugang_lokal_suchen(request: Request, device: str = Form("")):
    """Geräte im eigenen Netz per Rundruf suchen."""
    require_login(request)
    eintrag = geraete.aufloesen(device) or {}
    gid = eintrag.get("id", "")
    gefunden = await local.suche_im_netz(10)
    treffer = [ip for ip, d in gefunden.items() if d.get("gwId") == gid]
    if treffer:
        text = f"Gefunden unter {treffer[0]}"
    elif gefunden:
        text = f"{len(gefunden)} fremde Geräte gefunden, das eigene nicht dabei"
    else:
        text = "Nichts gefunden — vermutlich in einem anderen Netzsegment, Adresse von Hand eintragen"
    return RedirectResponse(f"/zugang?meldung={text.replace(' ', '+')}&device={gid}", 303)


@app.post("/zugang/qr/start")
async def zugang_qr_start(request: Request, user_code: str = Form(...)):
    """QR-Anmeldung beginnen."""
    require_login(request)
    try:
        ergebnis = await asyncio.to_thread(sharing.qr_code_anfordern, user_code)
    except Exception as exc:
        return RedirectResponse(f"/zugang?saved=error&meldung={str(exc)[:120].replace(' ', '+')}", 303)

    cfg = dict(config.get("sharing") or {})
    cfg.update({"user_code": user_code.strip(), "pending_token": ergebnis["token"]})
    config.set("sharing", cfg); config.save()
    return RedirectResponse("/zugang?saved=qr", 303)


@app.post("/zugang/qr/fertig")
async def zugang_qr_fertig(request: Request):
    """Nach dem Scannen die Zugangsdaten abholen."""
    require_login(request)
    cfg = dict(config.get("sharing") or {})
    token = cfg.get("pending_token")
    if not token:
        return RedirectResponse("/zugang?saved=error&meldung=Kein+offener+Anmeldevorgang", 303)
    try:
        info = await asyncio.to_thread(
            sharing.anmeldung_pruefen, token, cfg.get("user_code", "")
        )
    except Exception as exc:
        return RedirectResponse(
            f"/zugang?saved=error&meldung={str(exc)[:120].replace(' ', '+')}", 303
        )

    cfg.update({"enabled": True, "token": info})
    cfg.pop("pending_token", None)
    config.set("sharing", cfg); config.save(); reset_sharing()
    store.log_event("info", "QR-Anmeldung eingerichtet")

    # Wenn möglich gleich den lokalen Weg mitnehmen — dafür ist der QR-Zugang da.
    text = "QR-Anmeldung eingerichtet"
    sd = sharing_device()
    if sd:
        try:
            geraete = await sd.geraete_liste()
            eigenes = [g for g in geraete if g["id"] == config.get("device_id")]
            if eigenes:
                text += f" — {len(geraete)} Geräte sichtbar"
        except Exception:
            pass
    # Wer noch kein Geraet hat, will als naechstes genau das auswaehlen --
    # die Liste steht jetzt zur Verfuegung.
    ziel = "/devices?saved=1" if not geraete.liste() else "/zugang?saved=1"
    trenner = "&" if "?" in ziel else "?"
    return RedirectResponse(f"{ziel}{trenner}meldung={text.replace(' ', '+')}", 303)


@app.get("/zugang/qr.png")
async def zugang_qr_bild(request: Request):
    """Der QR-Code als Bild."""
    require_login(request)
    cfg = config.get("sharing") or {}
    token = cfg.get("pending_token")
    if not token:
        raise HTTPException(status_code=404, detail="Kein offener Anmeldevorgang")
    import io
    import qrcode

    bild = qrcode.make(f"tuyaSmart--qrLogin?token={token}")
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    from fastapi.responses import Response
    return Response(content=puffer.getvalue(), media_type="image/png")


# ------------------------------------------------------------- Ansichten


@app.get("/preise", response_class=HTMLResponse)
async def preise_ansicht(request: Request, device: str = ""):
    """Was der Strom kostet — heute und, sobald bekannt, morgen.

    Die Preise sind fuer alle Geraete dieselben; nur die Markierung "hier
    wuerde eingeschaltet" haengt an der Regel eines bestimmten Geraets.
    """
    if (redirect := guard(request)) is not None:
        return redirect
    st = zustand(device)
    auto = st.auto
    auto["mode_label"] = automation.MODE_LABELS.get(auto["mode"], auto["mode"])
    reihen = list(preise.data.get("today") or []) + list(preise.data.get("tomorrow") or [])
    return page(
        request,
        "preise.html",
        stunden=automation.schedule_preview(preise.data, auto, hours=48) if preise.data else [],
        alle=reihen,
        aktuell=preise.data.get("current") or {},
        quelle=prices.SOURCES.get(prices.settings(config.get("price"))["source"], {}).get("label", ""),
        einheit="ct/kWh" if preise.data.get("currency", "EUR") == "EUR"
                else f"{preise.data.get('currency')}-Cent/kWh",
        auto=auto,
        fehler=preise.error,
        geraet=geraete.holen(st.device_id) or {},
        geraete_liste=geraete.zusammenfassung(),
    )


@app.get("/verlauf", response_class=HTMLResponse)
async def verlauf_ansicht(request: Request, code: str = "", hours: int = 24, device: str = ""):
    """Die aufgezeichneten Messwerte eines Geraets."""
    if (redirect := guard(request)) is not None:
        return redirect
    st = zustand(device)
    hours = max(1, min(24 * 90, hours))
    codes = store.recorded_codes(24 * 90, device=st.device_id)
    if code not in codes:
        # Sinnvoller Einstieg: Leistung, sonst der erste vorhandene Wert
        code = "cur_power" if "cur_power" in codes else (codes[0] if codes else "")
    punkte = store.series(code, hours, device=st.device_id) if code else []
    eintrag = geraete.holen(st.device_id) or {}
    return page(
        request,
        "verlauf.html",
        codes=codes, code=code, hours=hours, punkte=punkte,
        einheiten={m["code"]: m["unit"] for m in st.view.get("metrics", [])},
        aufzeichnung=int(config.get("history_seconds", 60) or 0)
                     if eintrag.get("aufzeichnen", True) else 0,
        aufbewahrung=store.RETENTION_DAYS,
        ereignisse=store.recent_events(25, device=st.device_id),
        geraet=eintrag,
        geraete_liste=geraete.zusammenfassung(),
    )


# --------------------------------------------------------------- Preisquelle


@app.get("/preisquelle", response_class=HTMLResponse)
async def prices_page(request: Request, saved: str = ""):
    if (redirect := guard(request)) is not None:
        return redirect
    return page(
        request,
        "prices.html",
        price=prices.settings(config.get("price")),
        sources=prices.SOURCES,
        tibber=config.get("tibber") or {},
        preview=preise.data.get("today", []) if preise.data else [],
        price_error=preise.error,
        saved=saved,
    )


@app.post("/preisquelle")
async def prices_save(
    request: Request,
    source: str = Form("awattar_de"),
    markup_ct: str = Form("20"),
    vat_percent: str = Form("19"),
):
    # Als Text entgegennehmen und selbst umwandeln: Sonst beantwortet die
    # Formularpruefung ein deutsches Komma mit einer nackten Fehlerseite,
    # auf der nicht steht, was zu tun waere.
    require_login(request)
    cfg = prices.settings({
        "source": source,
        "markup_ct": zahl(markup_ct, 20.0),
        "vat_percent": zahl(vat_percent, 19.0),
    })
    config.set("price", cfg)
    config.save()

    # Tibber braucht erst noch Token und Zuhause, bevor ein Abruf klappen kann.
    if cfg["source"] == "tibber":
        tibber = config.get("tibber") or {}
        if not (tibber.get("token") and tibber.get("home_id")):
            return RedirectResponse("/tibber", status_code=303)

    preise.data = {}
    preise.ts = 0.0
    await poll_prices(force=True)
    if preise.error:
        return RedirectResponse("/preisquelle?saved=error", status_code=303)
    try:
        await apply_automation()
    except Exception as exc:
        log.warning("Automatik nach Quellenwechsel fehlgeschlagen: %s", exc)
    return RedirectResponse("/automation", status_code=303)


# -------------------------------------------------------------------- Tibber


@app.get("/tibber", response_class=HTMLResponse)
async def tibber_page(request: Request, saved: str = ""):
    if (redirect := guard(request)) is not None:
        return redirect
    tibber = config.get("tibber") or {}
    homes: list[dict[str, Any]] = []
    error = None
    if tibber.get("token"):
        try:
            homes = await tibber_client().list_homes()
        except Exception as exc:
            error = str(exc)
    return page(request, "tibber.html", tibber=tibber, homes=homes, error=error, saved=saved)


@app.post("/tibber")
async def tibber_save(
    request: Request,
    token: str = Form(""),
    home_id: str = Form(""),
    home_label: str = Form(""),
):
    require_login(request)
    tibber = dict(config.get("tibber") or {})
    if token.strip():
        tibber["token"] = token.strip()
    if home_id.strip():
        tibber["home_id"] = home_id.strip()
        tibber["home_label"] = home_label.strip() or home_id.strip()
    config.set("tibber", tibber)
    config.save()

    preise.data = {}
    preise.ts = 0.0
    if tibber.get("token") and tibber.get("home_id"):
        # Wer hier Token und Zuhause hinterlegt, will Tibber auch als Quelle.
        price_cfg = prices.settings(config.get("price"))
        price_cfg["source"] = "tibber"
        config.set("price", price_cfg)
        config.save()
        await poll_prices(force=True)
        if preise.error:
            return RedirectResponse("/tibber?saved=error", status_code=303)
        return RedirectResponse("/automation", status_code=303)
    return RedirectResponse("/tibber?saved=1", status_code=303)


# ------------------------------------------------------------------ Automatik


@app.get("/automation", response_class=HTMLResponse)
async def automation_page(request: Request, saved: str = "", device: str = ""):
    if (redirect := guard(request)) is not None:
        return redirect
    st = zustand(device)
    auto = automation.settings(config.get("automation"))
    switch_codes = [s["code"] for s in st.view.get("switches", [])] or ["switch"]
    return page(
        request,
        "automation.html",
        auto=auto,
        levels=LEVELS,
        level_labels=LEVEL_LABELS,
        mode_labels=automation.MODE_LABELS,
        switch_codes=switch_codes,
        preview=automation.schedule_preview(preise.data, auto) if preise.data else [],
        price_error=preise.error,
        price_source_label=prices.SOURCES.get(
            prices.settings(config.get("price"))["source"], {}
        ).get("label", ""),
        decision=st.last_decision,
        geraete=[
            {**geraete.holen(z.device_id), "zustand": z.as_dict()} for z in alle_zustaende()
        ],
        # Fuer die Kopfzeile: Die Regel gilt fuer alle, also steht dort der
        # Name der App und nicht der eines einzelnen Geraets.
        geraete_liste=geraete.zusammenfassung(),
        saved=saved,
    )


@app.post("/automation")
async def automation_save(request: Request):
    require_login(request)
    form = await request.form()
    auto = automation.settings(config.get("automation"))
    auto.update(
        {
            "enabled": form.get("enabled") == "on",
            "mode": form.get("mode") or "threshold",
            "threshold_ct": zahl(form.get("threshold_ct")),
            "cheapest_hours": ganzzahl(form.get("cheapest_hours")),
            "levels": form.getlist("levels"),
            "min_off_minutes": ganzzahl(form.get("min_off_minutes")),
            "min_on_minutes": ganzzahl(form.get("min_on_minutes")),
            "max_off_hours": ganzzahl(form.get("max_off_hours")),
            "override_minutes": ganzzahl(form.get("override_minutes")),
        }
    )
    config.set("automation", automation.settings(auto))
    config.save()

    # Wer der Regel folgt, wird nicht hier entschieden, sondern je Geraet in
    # der Geraeteliste. Zwei Bedienorte fuer dieselbe Sache waeren einer zu viel.

    store.log_event(
        "info",
        f"Automatik gespeichert: {'aktiv' if auto['enabled'] else 'aus'}, Modus {auto['mode']}",
    )
    await poll_prices(force=True)
    for st in alle_zustaende():
        try:
            await apply_automation(st)
        except Exception as exc:
            log.warning("[%s] Automatik nach dem Speichern fehlgeschlagen: %s", st.label(), exc)
    return RedirectResponse("/automation?saved=1", status_code=303)


@app.post("/automation/resume")
async def automation_resume(request: Request, device: str = Form("")):
    """Handbetrieb-Pause vorzeitig beenden."""
    require_login(request)
    st = zustand(device)
    geraete.handbetrieb_setzen(st.device_id, 0)
    await apply_automation(st)
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


# --------------------------------------------------------------- Einstellungen


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = ""):
    if (redirect := guard(request)) is not None:
        return redirect
    return page(
        request,
        "settings.html",
        regions=ENDPOINTS,
        api_token=config.ensure_api_token(),
        trial=trial_status(),
        geraete_liste=geraete.zusammenfassung(),
        saved=saved,
        error=None,
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    client_id: str = Form(...),
    client_secret: str = Form(""),
    region: str = Form("eu"),
    refresh_seconds: str = Form("180"),
    history_seconds: str = Form("60"),
    trial_expires: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    require_login(request)

    def fail(msg: str):
        return page(
            request,
            "settings.html",
            regions=ENDPOINTS,
            api_token=config.ensure_api_token(),
            trial=trial_status(),
            geraete_liste=geraete.zusammenfassung(),
            saved="",
            error=msg,
        )

    if region not in ENDPOINTS:
        return fail("Unbekannte Region.")
    if password:
        if len(password) < 8:
            return fail("Das neue Passwort muss mindestens 8 Zeichen haben.")
        if password != password2:
            return fail("Die beiden Passwörter stimmen nicht überein.")
        config.set_admin_password(password)

    # Leeres Secret-Feld = unveraendert lassen (es wird nie im Klartext angezeigt).
    secret = client_secret.strip() or config.tuya.get("client_secret", "")
    if client_id.strip() != config.tuya.get("client_id", "") or client_secret.strip():
        # Neue Zugangsdaten heissen in aller Regel: neues Projekt, neuer Zeitraum.
        config.set("tuya_setup_ts", time.time())
    config.set_tuya(client_id, secret, region)
    config.set("refresh_seconds", max(MIN_INTERVAL, min(MAX_INTERVAL,
                                                        ganzzahl(refresh_seconds, 180))))
    config.set("history_seconds", max(0, min(3600, ganzzahl(history_seconds, 60))))

    datum = (trial_expires or "").strip()
    if datum:
        try:
            dt.date.fromisoformat(datum)
        except ValueError:
            return fail("Das Ablaufdatum muss im Format JJJJ-MM-TT stehen.")
    config.set("trial_expires", datum)
    config.save()
    reset_client()
    for st in _states.values():
        st.spec = {}
        st.spec_fetched_at = 0.0
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/trial-verlaengert")
async def trial_verlaengert(request: Request):
    """Der Nutzer hat den Testzeitraum verlaengert — Zaehler neu starten."""
    require_login(request)
    config.set("tuya_setup_ts", time.time())
    config.save()
    store.log_event("info", "Tuya-Testzeitraum als verlängert markiert")
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@app.post("/settings/rotate-token")
async def rotate_token(request: Request):
    require_login(request)
    config.rotate_api_token()
    store.log_event("info", "API-Token neu erzeugt")
    return RedirectResponse("/settings?saved=token", status_code=303)


# ----------------------------------------------------------------- Diagnose


async def netzpruefung() -> dict[str, Any]:
    """Aktiv nachsehen, was von diesem Rechner aus erreichbar ist.

    Beantwortet die Fragen, die sonst per Ferndiagnose einzeln abgeklopft
    werden muessten: Kommt DNS durch? Antwortet Tuya? Steht das Geraet im
    Netz? Und vor allem -- geht die Uhr richtig? Eine um Minuten falsche
    Systemzeit laesst jede Tuya-Signatur scheitern, und der Fehlertext dazu
    ("sign invalid") deutet in eine voellig andere Richtung.
    """
    import socket

    ergebnis: dict[str, Any] = {}

    def tcp(host: str, port: int, timeout: float = 3.0) -> str:
        beginn = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return f"erreichbar ({round((time.time() - beginn) * 1000)} ms)"
        except Exception as exc:
            return f"nicht erreichbar: {type(exc).__name__}"

    def dns(name: str) -> str:
        try:
            return ", ".join(sorted({a[4][0] for a in socket.getaddrinfo(name, None)}))
        except Exception as exc:
            return f"keine Aufloesung: {type(exc).__name__}"

    endpunkt = ENDPOINTS.get(config.tuya.get("region", "eu"), "")
    tuya_host = endpunkt.replace("https://", "")
    ergebnis["tuya_endpunkt"] = endpunkt
    ergebnis["dns"] = {
        h: await asyncio.to_thread(dns, h)
        for h in filter(None, [tuya_host, "api.awattar.de", "api.energy-charts.info"])
    }
    ergebnis["tcp"] = {}
    if tuya_host:
        ergebnis["tcp"][f"{tuya_host}:443"] = await asyncio.to_thread(tcp, tuya_host, 443)

    # Die Geraete selbst: Port 6668 ist der lokale Tuya-Dienst.
    for eintrag in geraete.liste():
        ip = (eintrag.get("local") or {}).get("ip")
        if ip:
            ergebnis["tcp"][f"{eintrag['name']} {ip}:6668"] = await asyncio.to_thread(
                tcp, ip, 6668
            )

    # Uhrzeitabgleich gegen eine fremde Quelle
    try:
        import email.utils

        import httpx

        async with httpx.AsyncClient(timeout=5) as http:
            antwort = await http.head("https://api.awattar.de/v1/marketdata")
        fremd = antwort.headers.get("date")
        if fremd:
            fremd_ts = email.utils.parsedate_to_datetime(fremd).timestamp()
            abweichung = round(time.time() - fremd_ts, 1)
            ergebnis["uhrzeit"] = {
                "abweichung_s": abweichung,
                "bewertung": "in Ordnung" if abs(abweichung) < 60 else
                             "ZU GROSS — Tuya lehnt Signaturen ab",
            }
    except Exception as exc:
        ergebnis["uhrzeit"] = {"fehler": f"{type(exc).__name__}: {exc}"[:120]}

    return ergebnis


def diagnose_daten(netz: dict[str, Any] | None = None) -> dict[str, Any]:
    """Alles zusammentragen, was zur Fehlersuche taugt."""
    zustaende = []
    for st in alle_zustaende():
        daten = st.as_dict()
        daten["started_at"] = st.started_at
        daten["spezifikation_geladen"] = bool(st.spec)
        # Das Innenleben des Pollers -- genau die Werte, die sonst nur im
        # Speicher stehen und bei einer Ferndiagnose fehlen.
        daten["poller_intern"] = {
            "backoff_bis": st.backoff or 0,
            "an_seit": st.on_since,
            "aus_seit": st.off_since,
            "zuletzt_gesehen": st.last_seen,
            "erwarteter_zustand": st.expected_state,
            "laufender_block": sorted(st.block) if st.block else [],
            "spezifikation_alter_s": round(time.time() - st.spec_fetched_at)
            if st.spec_fetched_at else None,
            "letzte_aufzeichnung_vor_s": round(time.time() - st.last_record_ts)
            if st.last_record_ts else None,
        }
        zustaende.append(daten)
    return diagnose.bericht(
        zustaende=zustaende,
        preis_stand={
            "age_seconds": round(time.time() - preise.ts, 1) if preise.ts else None,
            "error": preise.error,
            "currency": preise.data.get("currency"),
            "stunden_heute": len(preise.data.get("today") or []),
            "stunden_morgen": len(preise.data.get("tomorrow") or []),
        },
        version={"version": VERSION, "gebaut_am": BUILD_DATE, "stand": GIT_COMMIT},
        trial=trial_status(),
        netz=netz,
    )


@app.get("/diagnose", response_class=HTMLResponse)
async def diagnose_seite(request: Request):
    """Ein Bericht zum Weitergeben, wenn etwas nicht laeuft."""
    if (redirect := guard(request)) is not None:
        return redirect
    daten = diagnose_daten(netz=await netzpruefung())
    import json as _json

    return page(
        request, "diagnose.html",
        bericht=daten,
        als_text=_json.dumps(daten, indent=2, ensure_ascii=False),
    )


@app.get("/diagnose.json")
async def diagnose_json(request: Request, _: None = Depends(require_api_access)):
    """Derselbe Bericht als Datei zum Anhaengen an eine Nachricht."""
    from fastapi.responses import Response
    import json as _json

    name = f"tuya-smartmeter-diagnose-{dt.date.today().isoformat()}.json"
    return Response(
        content=_json.dumps(
            diagnose_daten(netz=await netzpruefung()), indent=2, ensure_ascii=False
        ),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# -------------------------------------------------------------------- JSON-API


@app.get("/api/state")
async def api_state(request: Request, device: str = "",
                    _: None = Depends(require_api_access)):
    """Stand eines Geraets.

    Ohne Angabe das erste — so liest jede bestehende Anbindung (ioBroker,
    Zabbix, eigene Skripte) weiter das, was sie bisher gelesen hat. Die Liste
    aller Geraete steht zusaetzlich unter "devices".
    """
    daten = zustand(device).as_dict()
    daten["devices"] = [
        {"device_id": e["id"], "device_name": e["name"]} for e in geraete.liste()
    ]
    return daten


@app.get("/api/devices")
async def api_devices(_: None = Depends(require_api_access)):
    """Alle Geraete mit vollem Stand."""
    return {"devices": [st.as_dict() for st in alle_zustaende()]}


@app.post("/api/switch")
async def api_switch(request: Request, _: None = Depends(require_api_access)):
    payload = await request.json()
    code = payload.get("code")
    value = payload.get("value")
    if not code or not isinstance(value, bool):
        raise HTTPException(
            status_code=400, detail='Erwartet: {"code": "switch", "value": true|false}'
        )
    st = zustand(payload.get("device") or payload.get("device_id") or "")
    if not st.device_id:
        raise HTTPException(status_code=400, detail="Kein Gerät eingerichtet")
    if st.online is False:
        raise HTTPException(
            status_code=409,
            detail="Das Gerät ist nicht erreichbar. Strom da? WLAN da?",
        )

    try:
        await schalten(st, code, value)
    except TuyaError as exc:
        store.log_event(
            "error", f"Schaltbefehl {code}={value} fehlgeschlagen: {exc.msg}", device=st.device_id
        )
        raise HTTPException(status_code=502, detail=f"{exc.msg} (Code {exc.code})") from exc

    st.expected_state = value  # eigener Befehl, keine Fremdschaltung

    # Handbedienung pausiert die Automatik, sonst schaltet sie sofort zurueck.
    auto = st.auto
    if auto["enabled"] and auto["override_minutes"]:
        geraete.handbetrieb_setzen(st.device_id, time.time() + auto["override_minutes"] * 60)

    store.log_event(
        "switch", f"Von Hand geschaltet: {code} = {'ein' if value else 'aus'}", device=st.device_id
    )
    await asyncio.sleep(1)  # dem Geraet Zeit geben, den neuen Stand zu melden
    try:
        await poll_device(st)
    except Exception as exc:
        log.warning("Nachlesen nach dem Schalten fehlgeschlagen: %s", exc)
    return st.as_dict()


@app.get("/api/series")
async def api_series(code: str, hours: int = 24, device: str = "",
                     _: None = Depends(require_api_access)):
    hours = max(1, min(24 * 90, hours))
    gid = zustand(device).device_id
    return {"code": code, "hours": hours, "device": gid,
            "points": store.series(code, hours, device=gid)}


@app.get("/api/history-codes")
async def api_history_codes(device: str = "", _: None = Depends(require_api_access)):
    return {"codes": store.recorded_codes(24 * 7, device=zustand(device).device_id)}


@app.get("/api/events")
async def api_events(device: str = "", _: None = Depends(require_api_access)):
    if device:
        return {"events": store.recent_events(device=zustand(device).device_id)}
    return {"events": store.recent_events()}


@app.get("/api/prices")
async def api_prices(_: None = Depends(require_api_access)):
    return {
        "current": preise.data.get("current", {}),
        "today": preise.data.get("today", []),
        "tomorrow": preise.data.get("tomorrow", []),
        "age_seconds": round(time.time() - preise.ts, 1) if preise.ts else None,
        "error": preise.error,
    }


@app.get("/healthz")
async def healthz():
    """Fuer den TrueNAS-Healthcheck: laeuft der Dienst und ist der Stand frisch?"""
    if not config.setup_done:
        return JSONResponse({
            "status": "setup", "detail": "Ersteinrichtung offen",
            "version": VERSION, "build_date": BUILD_DATE,
        })
    interval = int(config.get("refresh_seconds", 180) or 180)
    # Ruhende Geraete bleiben aussen vor: Sie werden nicht abgefragt, also
    # waere jede Aussage ueber ihren Zustand erfunden.
    zustaende = alle_zustaende(nur_aktive=True) or [zustand()]
    erstes = zustaende[0]
    age = time.time() - erstes.ts if erstes.ts else None

    # Nach dem Start dauert es bis zum ersten Abruf ein Intervall. Das ist kein
    # Fehler — wer hier "degraded" meldet, loest bei jeder Aktualisierung einen
    # Fehlalarm in der Ueberwachung aus.
    if all(st.ts == 0 for st in zustaende) and \
            (time.time() - erstes.started_at) < interval + 30:
        return JSONResponse({
            "status": "starting",
            "detail": "Der erste Abruf steht noch aus",
            "version": VERSION, "build_date": BUILD_DATE,
        })

    # Bei mehreren Geraeten zaehlt das schlechteste: eine Ueberwachung, die
    # nur das erste Geraet ansieht, uebersieht genau den Ausfall, den sie
    # melden soll.
    def gesund(st: DeviceState) -> bool:
        alter = time.time() - st.ts if st.ts else None
        veraltet = alter is not None and alter > max(60, interval * 6)
        return st.ok and not veraltet and st.online is not False

    kranke = [st for st in zustaende if not gesund(st)]
    return JSONResponse(
        {
            "status": "ok" if not kranke else "degraded",
            "last_poll_age_seconds": round(age, 1) if age is not None else None,
            "error": erstes.error,
            "device_online": erstes.online,
            "price_error": preise.error,
            "trial": trial_status(),
            "polls": erstes.polls,
            "failures": erstes.failures,
            "devices": [
                {"device_id": st.device_id, "name": st.label(), "online": st.online,
                 "ok": st.ok, "kanal": st.kanal, "error": st.error}
                for st in zustaende
            ],
            "degraded_devices": [st.label() for st in kranke],
            "version": VERSION,
            "build_date": BUILD_DATE,
            "git_commit": GIT_COMMIT,
        },
        status_code=200,  # Container bleibt oben, auch wenn eine Cloud zickt
    )
