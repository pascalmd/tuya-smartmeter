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

from . import automation, prices, store
from .config import config
from .tibber import LEVEL_LABELS, LEVELS, TibberClient, TibberError, upcoming
from .tuya import ENDPOINTS, TuyaClient, TuyaError, build_view

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tuya-smartmeter")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MIN_INTERVAL = 5
MAX_INTERVAL = 3600
PRICE_REFRESH_SECONDS = 600  # Preise sind stundenscharf; 10 min reicht reichlich

# Wie oft ein Messwert in die Historie geschrieben wird. Bewusst entkoppelt vom
# Abfrageintervall: Auf einem Raspberry Pi liegt die Datenbank auf einer
# SD-Karte, und alle 10 s zu schreiben killt die Karte binnen Monaten.
# Geschaltet wird trotzdem im vollen Takt - nur das Protokoll ist gröber.
HISTORY_SECONDS_DEFAULT = 60


class State:
    """Letzter bekannter Stand, vom Hintergrund-Poller gepflegt."""

    def __init__(self) -> None:
        self.ts: float = 0.0
        self.ok: bool = False
        self.error: str = ""
        self.view: dict[str, Any] = {"switches": [], "metrics": [], "phases": []}
        self.spec: dict[str, Any] = {}
        self.spec_fetched_at: float = 0.0
        self.polls: int = 0
        self.failures: int = 0
        self.started_at: float = time.time()

        # Strompreise
        self.prices: dict[str, Any] = {}
        self.prices_ts: float = 0.0
        self.price_error: str = ""

        # Automatik
        self.last_decision: dict[str, Any] = {}
        self.last_action_ts: float = 0.0
        self.last_action: str = ""
        self.off_since: float | None = None
        self.on_since: float | None = None

        # Schaltzustand nachhalten, um fremde Eingriffe zu erkennen
        self.last_seen: bool | None = None
        self.expected_state: bool | None = None

        self.last_record_ts: float = 0.0
        self.online: bool | None = None
        self.offline_since: float | None = None

    def switch_value(self, code: str) -> bool | None:
        for sw in self.view.get("switches", []):
            if sw["code"] == code and sw.get("present"):
                return bool(sw["value"])
        return None

    def as_dict(self) -> dict[str, Any]:
        auto = automation.settings(config.get("automation"))
        price_cfg = prices.settings(config.get("price"))
        override_until = float(config.get("override_until") or 0)
        return {
            "ts": self.ts,
            "age_seconds": round(time.time() - self.ts, 1) if self.ts else None,
            "ok": self.ok,
            "error": self.error,
            "device_id": config.get("device_id", ""),
            "device_name": config.get("device_name", ""),
            "online": self.online,
            "offline_minutes": round((time.time() - self.offline_since) / 60)
            if self.offline_since
            else 0,
            "refresh_seconds": config.get("refresh_seconds", 10),
            "polls": self.polls,
            "failures": self.failures,
            "uptime_seconds": round(time.time() - self.started_at),
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
                "upcoming": upcoming(
                    list(self.prices.get("today") or []) + list(self.prices.get("tomorrow") or []),
                    dt.datetime.now(dt.timezone.utc),
                    12,
                )
                if self.prices
                else [],
            },
            "trial": trial_status(),
            "automation": {
                **auto,
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


state = State()
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


def tibber_client() -> TibberClient:
    return TibberClient(token=(config.get("tibber") or {}).get("token", ""))


# --------------------------------------------------------------------- Poller


async def poll_device() -> None:
    device_id = config.get("device_id", "")
    if not device_id:
        return
    api = client()

    # Die Spezifikation aendert sich praktisch nie -> hoechstens stuendlich neu holen.
    if not state.spec or time.time() - state.spec_fetched_at > 3600:
        state.spec = await api.device_spec(device_id)
        state.spec_fetched_at = time.time()

    schnappschuss = await api.device_snapshot(device_id)
    state.view = build_view(state.spec, schnappschuss["status"])
    state.ts = time.time()
    state.ok = True
    state.error = ""
    state.polls += 1

    war_online = state.online
    state.online = schnappschuss["online"]
    if not state.online:
        if state.offline_since is None:
            state.offline_since = time.time()
            store.log_event("warn", "Geraet meldet sich nicht mehr (offline)")
            log.warning("Geraet ist offline")
    else:
        if war_online is False:
            store.log_event("info", "Geraet ist wieder online")
            log.info("Geraet ist wieder online")
        state.offline_since = None

    # Solange das Geraet offline ist, liefert die Cloud unveraendert alte Werte.
    # Die aufzuzeichnen wuerde eine Messreihe erzeugen, die es nie gegeben hat.
    history_seconds = max(0, int(config.get("history_seconds", HISTORY_SECONDS_DEFAULT) or 0))
    if state.online and history_seconds and time.time() - state.last_record_ts >= history_seconds:
        store.record(state.view["metrics"], state.view["phases"])
        state.last_record_ts = time.time()

    # Aus-Zeit mitschreiben, damit das Sicherheitsnetz greifen kann.
    auto = automation.settings(config.get("automation"))
    current = state.switch_value(auto["switch_code"])
    if current is False:
        if state.off_since is None:
            state.off_since = time.time()
        state.on_since = None
    elif current is True:
        if state.on_since is None:
            state.on_since = time.time()
        state.off_since = None

    if current is None:
        # Der eingestellte Kanal existiert an diesem Geraet nicht. Das ist der
        # Normalfall nach der Ersteinrichtung: Der Standard heisst "switch",
        # viele Geraete nennen ihren Ausgang aber "switch_1". Ohne diese
        # Korrektur stuende die Automatik still, ohne dass jemand den Grund sieht.
        vorhandene = [sw["code"] for sw in state.view.get("switches", []) if sw.get("present")]
        if len(vorhandene) == 1 and vorhandene[0] != auto["switch_code"]:
            auto["switch_code"] = vorhandene[0]
            config.set("automation", automation.settings(auto))
            config.save()
            log.info("Schaltkanal automatisch auf '%s' gesetzt", vorhandene[0])
            store.log_event("info", f"Schaltkanal automatisch auf '{vorhandene[0]}' gesetzt")
            current = state.switch_value(auto["switch_code"])

    if current is not None:
        note_switch_state(current, auto)


def note_switch_state(current: bool, auto: dict[str, Any]) -> None:
    """Erkennen, ob jemand anders geschaltet hat — etwa in der Smart-Life-App.

    Ohne das wuerde die Automatik eine Handbedienung am Geraet oder im Handy
    binnen Sekunden zurueckdrehen. Ein Wechsel, den wir nicht selbst ausgeloest
    haben, zaehlt deshalb genauso als Handbetrieb wie ein Klick in dieser
    Oberflaeche.
    """
    vorher = state.last_seen
    state.last_seen = current

    if vorher is None or vorher == current:
        return  # erster Messwert oder keine Aenderung

    if state.expected_state is not None and current == state.expected_state:
        state.expected_state = None
        return  # das waren wir selbst

    # Ab hier: der Zustand hat sich geaendert, ohne dass wir es veranlasst haben.
    state.expected_state = None
    wort = "ein" if current else "aus"
    store.log_event("switch", f"Von aussen geschaltet: {auto['switch_code']} = {wort}")
    log.info("Fremdschaltung erkannt: %s = %s", auto["switch_code"], wort)

    if auto["enabled"] and auto["override_minutes"]:
        config.set("override_until", time.time() + auto["override_minutes"] * 60)
        config.save()
        state.last_action = (
            f"{wort.upper()} — von Hand geschaltet (nicht über diese Oberfläche), "
            f"Automatik pausiert {auto['override_minutes']} min"
        )
        state.last_action_ts = time.time()


async def poll_prices(force: bool = False) -> None:
    price_cfg = prices.settings(config.get("price"))
    if price_cfg["source"] == "tibber":
        tibber = config.get("tibber") or {}
        if not (tibber.get("token") and tibber.get("home_id")):
            return
    if not force and time.time() - state.prices_ts < PRICE_REFRESH_SECONDS:
        return
    try:
        state.prices = await prices.fetch(price_cfg, config.get("tibber") or {})
        state.prices_ts = time.time()
        state.price_error = ""
    except Exception as exc:
        state.price_error = str(exc)
        log.warning("Preisabruf (%s) fehlgeschlagen: %s", price_cfg["source"], exc)


async def apply_automation() -> None:
    """Regel auswerten und bei Bedarf schalten."""
    auto = automation.settings(config.get("automation"))
    if not auto["enabled"]:
        state.last_decision = {"desired": None, "reason": "Automatik ist aus", "price_ct": None}
        return

    override_until = float(config.get("override_until") or 0)
    if override_until > time.time():
        remaining = round((override_until - time.time()) / 60)
        state.last_decision = {
            "desired": None,
            "reason": f"Handbetrieb aktiv, Automatik pausiert (noch {remaining} min)",
            "price_ct": None,
        }
        return

    if state.online is False:
        dauer = ""
        if state.offline_since:
            dauer = f" (seit {round((time.time() - state.offline_since) / 60)} min)"
        state.last_decision = {
            "desired": None,
            "reason": f"Geraet ist nicht erreichbar{dauer} — es wird nicht geschaltet",
            "price_ct": None,
        }
        return

    if not state.prices:
        state.last_decision = {
            "desired": None,
            "reason": state.price_error or "Noch keine Strompreise abgerufen",
            "price_ct": None,
        }
        return

    decision = automation.decide(
        state.prices, auto, dt.datetime.now(dt.timezone.utc), off_since=state.off_since
    )
    state.last_decision = decision.as_dict()
    if decision.desired is None:
        return

    code = auto["switch_code"]
    current = state.switch_value(code)
    if current is None:
        state.last_decision["reason"] += f" — Schaltkanal '{code}' meldet keinen Zustand"
        return
    if current == decision.desired:
        return

    # Flatterschutz: nach dem Ausschalten eine Mindestzeit warten.
    if (
        decision.desired is True
        and auto["min_off_minutes"]
        and state.off_since
        and (time.time() - state.off_since) < auto["min_off_minutes"] * 60
    ):
        state.last_decision["reason"] += " — Mindest-Aus-Zeit noch nicht erreicht"
        return

    # Mindestlaufzeit: einmal Eingeschaltetes eine Weile laufen lassen - fuer
    # Verbraucher, die eine kurze Unterbrechung nicht gut vertragen.
    if (
        decision.desired is False
        and auto["min_on_minutes"]
        and state.on_since
        and (time.time() - state.on_since) < auto["min_on_minutes"] * 60
    ):
        rest = round(auto["min_on_minutes"] - (time.time() - state.on_since) / 60)
        state.last_decision["reason"] += f" — Mindestlaufzeit laeuft noch ({rest} min)"
        return

    try:
        await client().send_commands(
            config.get("device_id"), [{"code": code, "value": decision.desired}]
        )
    except Exception as exc:
        store.log_event("error", f"Automatik konnte nicht schalten: {exc}")
        log.warning("Automatik-Schaltbefehl fehlgeschlagen: %s", exc)
        return

    state.expected_state = decision.desired  # damit der naechste Poll uns nicht fuer fremd haelt
    state.last_action = f"{'EIN' if decision.desired else 'AUS'} — {decision.reason}"
    state.last_action_ts = time.time()
    store.log_event(
        "switch",
        f"Automatik: {code} = {'ein' if decision.desired else 'aus'} ({decision.reason})",
    )
    log.info("Automatik schaltet %s: %s", "EIN" if decision.desired else "AUS", decision.reason)
    await asyncio.sleep(1)
    try:
        await poll_device()
    except Exception:
        pass


async def poller() -> None:
    """Laeuft dauerhaft, unabhaengig von geoeffneten Browser-Tabs."""
    backoff = 0
    last_prune = 0.0
    while True:
        interval = int(config.get("refresh_seconds", 10) or 10)
        interval = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))

        if config.setup_done and config.get("device_id"):
            try:
                await poll_device()
                if backoff:
                    store.log_event("info", "Verbindung zur Tuya-Cloud wieder da")
                backoff = 0
            except Exception as exc:
                state.ok = False
                if isinstance(exc, TuyaError):
                    state.error = tuya_error_hint(
                        exc, config.tuya.get("client_id", ""), ""
                    )
                else:
                    state.error = str(exc)
                state.failures += 1
                if backoff == 0:
                    store.log_event("error", str(exc))
                    log.warning("Poll fehlgeschlagen: %s", exc)
                backoff = min(backoff * 2 or interval, 300)

            await poll_prices()
            try:
                await apply_automation()
            except Exception as exc:
                log.warning("Automatik-Durchlauf fehlgeschlagen: %s", exc)

        now = time.time()
        if now - last_prune > 6 * 3600:
            last_prune = now
            try:
                removed = store.prune()
                if removed:
                    log.info("Historie aufgeraeumt: %d alte Messwerte entfernt", removed)
            except Exception as exc:
                log.warning("Aufraeumen der Historie fehlgeschlagen: %s", exc)

        await asyncio.sleep(backoff or interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
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
            "Ueberwachung des Tuya-Testzeitraums beginnt ab heute "
            "(tatsaechlicher Projektstart unbekannt)",
        )
    task = asyncio.create_task(poller(), name="tuya-poller")
    log.info("Poller gestartet (Intervall %ss)", config.get("refresh_seconds", 10))
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
    return TEMPLATES.TemplateResponse(request, name, {"cfg": config, "show_nav": True, **ctx})


def trial_status() -> dict[str, Any]:
    """Erinnerung an den ablaufenden Tuya-Testzeitraum.

    Tuya befristet kostenlose Cloud-Projekte und verraet ueber die API nicht,
    wann Schluss ist — es hoert einfach auf zu funktionieren. Deshalb zaehlen wir
    selbst ab dem Tag, an dem die Zugangsdaten zuletzt bestaetigt wurden, und
    erinnern rechtzeitig. Wer verlaengert hat, setzt den Zaehler per Klick zurueck.
    """
    seit = float(config.get("tuya_setup_ts") or 0)
    if not seit:
        return {"known": False, "days": 0, "warn": False, "expired": False}

    tage = (time.time() - seit) / 86400
    grenze = int(config.get("trial_reminder_days", 25) or 25)
    abgelaufen = bool(state.error and ("1106" in state.error or "1114" in state.error))
    return {
        "known": True,
        "days": int(tage),
        "days_until_reminder": max(0, grenze - int(tage)),
        "warn": tage >= grenze,
        "expired": abgelaufen,
    }


def tuya_error_hint(exc: TuyaError, client_id: str, client_secret: str) -> str:
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
            "vollstaendig markieren und kopieren."
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
            "oder das Projekt liegt in einem anderen Rechenzentrum als hier ausgewaehlt. "
            "Im Tuya-Projekt unter Overview steht, welches es ist."
        )

    if code in (1106, 1114):  # no permissions
        return (
            f"Keine Berechtigung (Code {code}). Entweder fehlt im Tuya-Projekt eine der "
            "APIs (IoT Core, Authorization, Smart Home Scene Linkage), oder der "
            "Testzeitraum ist abgelaufen — dann unter Service → Extend Trial verlaengern."
        )

    return (
        f"Die Tuya-Cloud lehnt die Anfrage ab: {exc.msg} (Code {code}). "
        "Bitte Access ID, Access Secret und das Rechenzentrum pruefen."
    )


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
    client_id: str = Form(...),
    client_secret: str = Form(...),
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
        return fail("Die beiden Passwoerter stimmen nicht ueberein.")
    if region not in ENDPOINTS:
        return fail("Unbekannte Region.")

    config.set_tuya(client_id, client_secret, region)
    reset_client()
    try:
        await client().list_devices()
    except TuyaError as exc:
        return fail(tuya_error_hint(exc, client_id, client_secret))
    except Exception as exc:
        return fail(f"Keine Verbindung zur Tuya-Cloud: {exc}")

    config.set_admin_password(password)
    config.set("setup_done", True)
    config.set("tuya_setup_ts", time.time())
    config.ensure_api_token()
    config.save()
    request.session["user"] = "admin"
    store.log_event("info", "Ersteinrichtung abgeschlossen")
    return RedirectResponse("/devices", status_code=303)


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
    if not config.get("device_id"):
        return RedirectResponse("/devices", status_code=303)
    return page(request, "dashboard.html", state=state.as_dict())


@app.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    if (redirect := guard(request)) is not None:
        return redirect
    error = None
    devices: list[dict[str, Any]] = []
    try:
        devices = await client().list_devices()
    except TuyaError as exc:
        error = (
            f"{exc.msg} (Code {exc.code}). Ist das Smart-Life-Konto im Tuya-Projekt "
            "unter 'Devices → Link App Account' verknuepft?"
        )
    except Exception as exc:
        error = str(exc)
    return page(request, "devices.html", devices=devices, error=error)


@app.post("/devices")
async def devices_select(request: Request, device_id: str = Form(...), device_name: str = Form("")):
    require_login(request)
    config.set("device_id", device_id.strip())
    config.set("device_name", device_name.strip() or device_id.strip())
    config.save()
    state.spec = {}
    state.spec_fetched_at = 0.0
    state.ts = 0.0
    store.log_event("info", f"Geraet ausgewaehlt: {config.get('device_name')}")
    try:
        await poll_device()
    except Exception as exc:
        state.ok = False
        state.error = str(exc)
    return RedirectResponse("/prices", status_code=303)


# --------------------------------------------------------------- Preisquelle


@app.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request, saved: str = ""):
    if (redirect := guard(request)) is not None:
        return redirect
    return page(
        request,
        "prices.html",
        price=prices.settings(config.get("price")),
        sources=prices.SOURCES,
        tibber=config.get("tibber") or {},
        preview=state.prices.get("today", []) if state.prices else [],
        price_error=state.price_error,
        saved=saved,
    )


@app.post("/prices")
async def prices_save(
    request: Request,
    source: str = Form("awattar_de"),
    markup_ct: float = Form(20.0),
    vat_percent: float = Form(19.0),
):
    require_login(request)
    cfg = prices.settings(
        {"source": source, "markup_ct": markup_ct, "vat_percent": vat_percent}
    )
    config.set("price", cfg)
    config.save()

    # Tibber braucht erst noch Token und Zuhause, bevor ein Abruf klappen kann.
    if cfg["source"] == "tibber":
        tibber = config.get("tibber") or {}
        if not (tibber.get("token") and tibber.get("home_id")):
            return RedirectResponse("/tibber", status_code=303)

    state.prices = {}
    state.prices_ts = 0.0
    await poll_prices(force=True)
    if state.price_error:
        return RedirectResponse("/prices?saved=error", status_code=303)
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

    state.prices = {}
    state.prices_ts = 0.0
    if tibber.get("token") and tibber.get("home_id"):
        # Wer hier Token und Zuhause hinterlegt, will Tibber auch als Quelle.
        price_cfg = prices.settings(config.get("price"))
        price_cfg["source"] = "tibber"
        config.set("price", price_cfg)
        config.save()
        await poll_prices(force=True)
        if state.price_error:
            return RedirectResponse("/tibber?saved=error", status_code=303)
        return RedirectResponse("/automation", status_code=303)
    return RedirectResponse("/tibber?saved=1", status_code=303)


# ------------------------------------------------------------------ Automatik


@app.get("/automation", response_class=HTMLResponse)
async def automation_page(request: Request, saved: str = ""):
    if (redirect := guard(request)) is not None:
        return redirect
    auto = automation.settings(config.get("automation"))
    switch_codes = [s["code"] for s in state.view.get("switches", [])] or ["switch"]
    return page(
        request,
        "automation.html",
        auto=auto,
        levels=LEVELS,
        level_labels=LEVEL_LABELS,
        mode_labels=automation.MODE_LABELS,
        switch_codes=switch_codes,
        preview=automation.schedule_preview(state.prices, auto) if state.prices else [],
        price_error=state.price_error,
        price_source_label=prices.SOURCES.get(
            prices.settings(config.get("price"))["source"], {}
        ).get("label", ""),
        decision=state.last_decision,
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
            "switch_code": (form.get("switch_code") or "switch").strip(),
            "mode": form.get("mode") or "threshold",
            "threshold_ct": float(form.get("threshold_ct") or 0),
            "cheapest_hours": int(form.get("cheapest_hours") or 0),
            "levels": form.getlist("levels"),
            "min_off_minutes": int(form.get("min_off_minutes") or 0),
            "min_on_minutes": int(form.get("min_on_minutes") or 0),
            "max_off_hours": int(form.get("max_off_hours") or 0),
            "override_minutes": int(form.get("override_minutes") or 0),
        }
    )
    config.set("automation", automation.settings(auto))
    config.save()
    store.log_event(
        "info",
        f"Automatik gespeichert: {'aktiv' if auto['enabled'] else 'aus'}, Modus {auto['mode']}",
    )
    await poll_prices(force=True)
    try:
        await apply_automation()
    except Exception as exc:
        log.warning("Automatik nach dem Speichern fehlgeschlagen: %s", exc)
    return RedirectResponse("/automation?saved=1", status_code=303)


@app.post("/automation/resume")
async def automation_resume(request: Request):
    """Handbetrieb-Pause vorzeitig beenden."""
    require_login(request)
    config.set("override_until", 0)
    config.save()
    await apply_automation()
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
        saved=saved,
        error=None,
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    client_id: str = Form(...),
    client_secret: str = Form(""),
    region: str = Form("eu"),
    refresh_seconds: int = Form(10),
    history_seconds: int = Form(60),
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
            saved="",
            error=msg,
        )

    if region not in ENDPOINTS:
        return fail("Unbekannte Region.")
    if password:
        if len(password) < 8:
            return fail("Das neue Passwort muss mindestens 8 Zeichen haben.")
        if password != password2:
            return fail("Die beiden Passwoerter stimmen nicht ueberein.")
        config.set_admin_password(password)

    # Leeres Secret-Feld = unveraendert lassen (es wird nie im Klartext angezeigt).
    secret = client_secret.strip() or config.tuya.get("client_secret", "")
    if client_id.strip() != config.tuya.get("client_id", "") or client_secret.strip():
        # Neue Zugangsdaten heissen in aller Regel: neues Projekt, neuer Zeitraum.
        config.set("tuya_setup_ts", time.time())
    config.set_tuya(client_id, secret, region)
    config.set("refresh_seconds", max(MIN_INTERVAL, min(MAX_INTERVAL, int(refresh_seconds))))
    config.set("history_seconds", max(0, min(3600, int(history_seconds))))
    config.save()
    reset_client()
    state.spec = {}
    state.spec_fetched_at = 0.0
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/trial-verlaengert")
async def trial_verlaengert(request: Request):
    """Der Nutzer hat den Testzeitraum verlaengert — Zaehler neu starten."""
    require_login(request)
    config.set("tuya_setup_ts", time.time())
    config.save()
    store.log_event("info", "Tuya-Testzeitraum als verlaengert markiert")
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@app.post("/settings/rotate-token")
async def rotate_token(request: Request):
    require_login(request)
    config.rotate_api_token()
    store.log_event("info", "API-Token neu erzeugt")
    return RedirectResponse("/settings?saved=token", status_code=303)


# -------------------------------------------------------------------- JSON-API


@app.get("/api/state")
async def api_state(request: Request, _: None = Depends(require_api_access)):
    return state.as_dict()


@app.post("/api/switch")
async def api_switch(request: Request, _: None = Depends(require_api_access)):
    payload = await request.json()
    code = payload.get("code")
    value = payload.get("value")
    if not code or not isinstance(value, bool):
        raise HTTPException(
            status_code=400, detail='Erwartet: {"code": "switch", "value": true|false}'
        )
    device_id = config.get("device_id", "")
    if not device_id:
        raise HTTPException(status_code=400, detail="Kein Geraet ausgewaehlt")
    if state.online is False:
        raise HTTPException(
            status_code=409,
            detail="Das Geraet ist nicht erreichbar. Strom da? WLAN da?",
        )

    try:
        await client().send_commands(device_id, [{"code": code, "value": value}])
    except TuyaError as exc:
        store.log_event("error", f"Schaltbefehl {code}={value} fehlgeschlagen: {exc.msg}")
        raise HTTPException(status_code=502, detail=f"{exc.msg} (Code {exc.code})") from exc

    state.expected_state = value  # eigener Befehl, keine Fremdschaltung

    # Handbedienung pausiert die Automatik, sonst schaltet sie sofort zurueck.
    auto = automation.settings(config.get("automation"))
    if auto["enabled"] and auto["override_minutes"]:
        config.set("override_until", time.time() + auto["override_minutes"] * 60)
        config.save()

    store.log_event("switch", f"Von Hand geschaltet: {code} = {'ein' if value else 'aus'}")
    await asyncio.sleep(1)  # dem Geraet Zeit geben, den neuen Stand zu melden
    try:
        await poll_device()
    except Exception as exc:
        log.warning("Nachlesen nach dem Schalten fehlgeschlagen: %s", exc)
    return state.as_dict()


@app.get("/api/series")
async def api_series(code: str, hours: int = 24, _: None = Depends(require_api_access)):
    hours = max(1, min(24 * 90, hours))
    return {"code": code, "hours": hours, "points": store.series(code, hours)}


@app.get("/api/history-codes")
async def api_history_codes(_: None = Depends(require_api_access)):
    return {"codes": store.recorded_codes(24 * 7)}


@app.get("/api/events")
async def api_events(_: None = Depends(require_api_access)):
    return {"events": store.recent_events()}


@app.get("/api/prices")
async def api_prices(_: None = Depends(require_api_access)):
    return {
        "current": state.prices.get("current", {}),
        "today": state.prices.get("today", []),
        "tomorrow": state.prices.get("tomorrow", []),
        "age_seconds": round(time.time() - state.prices_ts, 1) if state.prices_ts else None,
        "error": state.price_error,
    }


@app.get("/healthz")
async def healthz():
    """Fuer den TrueNAS-Healthcheck: laeuft der Dienst und ist der Stand frisch?"""
    if not config.setup_done:
        return JSONResponse({"status": "setup", "detail": "Ersteinrichtung offen"})
    interval = int(config.get("refresh_seconds", 10) or 10)
    age = time.time() - state.ts if state.ts else None
    stale = age is not None and age > max(60, interval * 6)
    healthy = state.ok and not stale and state.online is not False
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "last_poll_age_seconds": round(age, 1) if age is not None else None,
            "error": state.error,
            "device_online": state.online,
            "price_error": state.price_error,
            "trial": trial_status(),
            "polls": state.polls,
            "failures": state.failures,
        },
        status_code=200,  # Container bleibt oben, auch wenn eine Cloud zickt
    )
