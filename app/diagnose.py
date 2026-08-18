"""Ein Bericht ueber den Zustand der App, den man verschicken kann.

Gedacht fuer den Fall "bei mir geht etwas nicht": ein Klick, eine Datei, und
der andere sieht, woran es liegt -- ohne Fernzugriff, ohne Screenshots von
Einstellungsseiten, ohne Nachfragen im Halbstundentakt.

Die wichtigste Eigenschaft ist deshalb nicht, was drinsteht, sondern was
nicht: **kein Geheimnis verlaesst diese Funktion.** Zugangsdaten, Schluessel
und Token erscheinen ausschliesslich als Befund ("gesetzt, 32 Zeichen"), denn
genau dieser Befund ist bei Anmeldefehlern die entscheidende Information --
ein Secret mit 31 Zeichen ist abgeschnitten, eines mit 0 fehlt.

Messwerte bleiben ebenfalls draussen. Sie sagen ueber ein Problem selten
etwas aus, machen den Bericht aber unhandlich und verraten nebenbei, wann
jemand zu Hause ist.
"""

from __future__ import annotations

import datetime as dt
import os
import platform
import sys
import time
from typing import Any

from . import automation, geraete, prices, store
from .config import CONFIG_FILE, config

# Alles, was niemals im Klartext im Bericht stehen darf. Der Abgleich laeuft
# ueber den Schluesselnamen, nicht ueber den Inhalt -- ein neues Geheimnis
# faellt so automatisch unter die Regel, solange es sinnvoll heisst.
GEHEIM = ("secret", "key", "token", "hash", "salt", "password", "passwort")

# Nicht geheim, aber auch nichts fuer eine Nachricht an Dritte: Werte, die ein
# Konto oder Projekt benennen. Von ihnen bleibt der Anfang stehen -- genug, um
# zwei Installationen zu vergleichen ("sind das dieselben Zugangsdaten?"),
# zu wenig, um damit etwas anzufangen.
HALBOFFEN = ("client_id", "user_code", "home_id", "uid")


def _befund(wert: Any) -> str:
    """Ein Geheimnis auf das reduzieren, was zur Fehlersuche taugt."""
    if wert in (None, "", {}, []):
        return "nicht gesetzt"
    if isinstance(wert, dict):
        return f"gesetzt ({len(wert)} Felder)"
    text = str(wert)
    return f"gesetzt ({len(text)} Zeichen)"


def _ist_geheim(schluessel: str) -> bool:
    return any(teil in schluessel.lower() for teil in GEHEIM)


def _angedeutet(wert: Any) -> str:
    """Anfang zeigen, Rest verschweigen."""
    if wert in (None, "", {}, []):
        return "nicht gesetzt"
    text = str(wert)
    if len(text) <= 6:
        return f"{text[:2]}… ({len(text)} Zeichen)"
    return f"{text[:4]}… ({len(text)} Zeichen)"


def _saeubern(daten: Any, pfad: str = "") -> Any:
    """Rekursiv durch die Konfiguration und jedes Geheimnis ersetzen."""
    if isinstance(daten, dict):
        sauber = {}
        for schluessel, wert in daten.items():
            if _ist_geheim(schluessel):
                sauber[schluessel] = _befund(wert)
            elif schluessel.lower() in HALBOFFEN:
                sauber[schluessel] = _angedeutet(wert)
            elif schluessel == "dp_map" and isinstance(wert, dict):
                # Die Zuordnung selbst ist unverdaechtig, aber lang.
                sauber[schluessel] = f"{len(wert)} Eintraege: {', '.join(sorted(wert.values())[:8])}"
            else:
                sauber[schluessel] = _saeubern(wert, f"{pfad}.{schluessel}")
        return sauber
    if isinstance(daten, list):
        return [_saeubern(eintrag, pfad) for eintrag in daten]
    return daten


def _kennung(wert: str) -> str:
    """Geraetekennungen bleiben vollstaendig.

    Sie sind zum Steuern wertlos, solange der lokale Schluessel und die
    Cloud-Zugangsdaten fehlen -- und beide stehen hier nicht drin. Fuer die
    Fehlersuche sind sie dagegen der Schluessel: Nur damit laesst sich ein
    Geraet in der Tuya-Konsole wiederfinden oder pruefen, ob die App vom
    selben Geraet spricht wie der Nutzer.
    """
    return wert or ""


def _historie() -> dict[str, Any]:
    """Kennzahlen der Aufzeichnung -- ohne einen einzigen Messwert."""
    try:
        import sqlite3

        with sqlite3.connect(store.DB_FILE) as conn:
            anzahl = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            spanne = conn.execute("SELECT MIN(ts), MAX(ts) FROM samples").fetchone()
            codes = [r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM samples ORDER BY code"
            ).fetchall()]
            ereignisse = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        groesse = os.path.getsize(store.DB_FILE) if store.DB_FILE.exists() else 0
        return {
            "messpunkte": anzahl,
            "ereignisse": ereignisse,
            "aufgezeichnete_groessen": codes,
            "von": dt.datetime.fromtimestamp(spanne[0]).isoformat(timespec="seconds")
            if spanne[0] else None,
            "bis": dt.datetime.fromtimestamp(spanne[1]).isoformat(timespec="seconds")
            if spanne[1] else None,
            "datei_kb": round(groesse / 1024),
        }
    except Exception as exc:                     # Datei fehlt, gesperrt, kaputt
        return {"fehler": str(exc)[:200]}


def laufzeitumgebung() -> dict[str, Any]:
    """Container, Grenzen, Einbindungen -- die haeufigsten stillen Ursachen.

    "Nach dem Neustart war alles weg" heisst fast immer: /config war kein
    dauerhaft eingebundenes Verzeichnis. "Der Dienst wird staendig neu
    gestartet" heisst oft: das Speicherlimit ist zu klein. Beides steht
    nirgends in der Oberflaeche und laesst sich aus der Ferne kaum erfragen --
    hier steht es.
    """
    aus: dict[str, Any] = {}

    aus["hostname"] = platform.node()
    aus["im_container"] = os.path.exists("/.dockerenv")
    if not aus["im_container"] and os.path.exists("/proc/1/cgroup"):
        try:
            with open("/proc/1/cgroup") as f:
                inhalt = f.read()
            aus["im_container"] = "docker" in inhalt or "kubepods" in inhalt
        except OSError:
            pass

    # Grenzen aus cgroup v2, sonst v1
    def lesen(pfad: str) -> str | None:
        try:
            with open(pfad) as f:
                return f.read().strip()
        except OSError:
            return None

    speicher_max = lesen("/sys/fs/cgroup/memory.max") or \
        lesen("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    speicher_jetzt = lesen("/sys/fs/cgroup/memory.current") or \
        lesen("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    cpu_max = lesen("/sys/fs/cgroup/cpu.max")
    grenzen: dict[str, Any] = {}
    if speicher_max and speicher_max.isdigit() and int(speicher_max) < 2**60:
        grenzen["speicher_limit_mb"] = round(int(speicher_max) / 1024 / 1024)
    elif speicher_max:
        grenzen["speicher_limit_mb"] = "ohne Grenze"
    if speicher_jetzt and speicher_jetzt.isdigit():
        grenzen["speicher_benutzt_mb"] = round(int(speicher_jetzt) / 1024 / 1024)
    if cpu_max:
        grenzen["cpu_max"] = cpu_max
    aus["grenzen"] = grenzen or "keine gefunden"

    # Ist /config wirklich eingebunden? Und laesst es sich beschreiben?
    verzeichnis = CONFIG_FILE.parent
    eingebunden = None
    try:
        with open("/proc/self/mountinfo") as f:
            zeilen = f.read().splitlines()
        ziel = str(verzeichnis)
        eingebunden = any(f" {ziel} " in z for z in zeilen)
    except OSError:
        pass
    try:
        probe = verzeichnis / ".schreibprobe"
        probe.write_text("x")
        probe.unlink()
        schreibbar = True
    except Exception as exc:
        schreibbar = f"nein: {type(exc).__name__}"
    frei = None
    try:
        stat = os.statvfs(verzeichnis)
        frei = round(stat.f_bavail * stat.f_frsize / 1024 / 1024)
    except OSError:
        pass
    aus["konfigurationsverzeichnis"] = {
        "pfad": str(verzeichnis),
        "eigenes_dateisystem": eingebunden,
        "beschreibbar": schreibbar,
        "frei_mb": frei,
        "dateien": sorted(p.name for p in verzeichnis.glob("*")) if verzeichnis.exists() else [],
    }

    # Umgebungsvariablen: nur die, die das Verhalten steuern -- und auch die
    # laufen durch dieselbe Geheimnispruefung wie alles andere.
    interessant = ("TZ", "LOG_LEVEL", "CONFIG_DIR", "APP_VERSION", "BUILD_DATE",
                   "GIT_COMMIT", "PYTHONUNBUFFERED", "PORT", "HOSTNAME", "PATH",
                   "LANG", "HOME", "VIRTUAL_ENV")
    aus["umgebungsvariablen"] = {
        name: (_befund(wert) if _ist_geheim(name) else wert)
        for name, wert in sorted(os.environ.items())
        if name in interessant
    }

    # Eigene Adressen -- fuer die Frage, ob App und Geraet im selben Netz sind
    try:
        import socket

        adressen = sorted({
            a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None)
        })
        aus["eigene_adressen"] = adressen
    except Exception:
        aus["eigene_adressen"] = []

    return aus


def pakete() -> dict[str, str]:
    """Versionen der Bibliotheken, an denen es haengen kann.

    tinytuya und das Sharing-SDK aendern ihr Verhalten zwischen Versionen --
    ohne diese Zeilen raet man beim Vergleich zweier Installationen.
    """
    from importlib import metadata

    namen = ("fastapi", "starlette", "uvicorn", "httpx", "jinja2", "tinytuya",
             "tuya-device-sharing-sdk", "qrcode", "itsdangerous", "pycryptodome")
    out = {}
    for name in namen:
        try:
            out[name] = metadata.version(name)
        except Exception:
            out[name] = "fehlt"
    return out


def prozess() -> dict[str, Any]:
    """Was der Dienst gerade verbraucht."""
    import resource
    import threading

    nutzung = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "pid": os.getpid(),
        "threads": threading.active_count(),
        "speicher_mb": round(nutzung.ru_maxrss / 1024),
        "cpu_sekunden": round(nutzung.ru_utime + nutzung.ru_stime, 1),
        "arbeitsverzeichnis": os.getcwd(),
        "benutzer_id": os.getuid(),
    }


def datenbank() -> dict[str, Any]:
    """Aufbau der Historie -- fuer den Fall, dass eine Migration haengt."""
    import sqlite3

    try:
        with sqlite3.connect(store.DB_FILE) as conn:
            tabellen = {}
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ):
                spalten = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
                tabellen[name] = spalten
            indizes = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            seiten = conn.execute("PRAGMA page_count").fetchone()[0]
            groesse = conn.execute("PRAGMA page_size").fetchone()[0]
        return {"tabellen": tabellen, "indizes": indizes, "journal": journal,
                "belegt_kb": round(seiten * groesse / 1024)}
    except Exception as exc:
        return {"fehler": str(exc)[:200]}


def bericht(zustaende: list[dict[str, Any]], preis_stand: dict[str, Any],
            version: dict[str, str], trial: dict[str, Any],
            netz: dict[str, Any] | None = None) -> dict[str, Any]:
    """Den vollstaendigen Bericht bauen.

    Die Zustaende kommen von aussen herein, damit dieses Modul nichts ueber
    den Poller wissen muss -- und damit es sich ohne laufende App pruefen
    laesst.
    """
    auto = automation.settings(config.get("automation"))
    preis_cfg = prices.settings(config.get("price"))

    geraeteliste = []
    for eintrag in geraete.liste():
        stand = next((z for z in zustaende if z.get("device_id") == eintrag["id"]), {})
        lokal = eintrag.get("local") or {}
        geraeteliste.append({
            "name": eintrag.get("name"),
            "kennung": eintrag["id"],
            "aktiv": eintrag.get("aktiv", True),
            "folgt_der_regel": eintrag.get("automatik_aktiv", True),
            "zeichnet_auf": eintrag.get("aufzeichnen", True),
            "schaltkanal": eintrag.get("switch_code") or "(wird erkannt)",
            "lokal": {
                "eingerichtet": bool(lokal.get("enabled")),
                "adresse": lokal.get("ip") or "",
                "schluessel": _befund(lokal.get("key")),
                "protokoll": lokal.get("version") or "(wird erkannt)",
                "datenpunkte": len(lokal.get("dp_map") or {}),
                "cloud_als_rueckfall": lokal.get("fallback_cloud", True),
            },
            "lokal_roh": {
                "dp_map": lokal.get("dp_map") or {},
            },
            "stand": {
                "online": stand.get("online"),
                "weg": stand.get("kanal") or "(noch keiner)",
                "letzter_abruf_vor_s": stand.get("age_seconds"),
                "abrufe": stand.get("polls"),
                "fehlschlaege": stand.get("failures"),
                "fehler": stand.get("error") or "",
                "offline_minuten": stand.get("offline_minutes"),
                "schalter": [
                    {"code": s.get("code"), "an": s.get("value"), "vorhanden": s.get("present")}
                    for s in stand.get("switches", [])
                ],
                "gemeldete_groessen": [m.get("code") for m in stand.get("metrics", [])],
                "gemeldete_einheiten": {m.get("code"): m.get("unit")
                                        for m in stand.get("metrics", [])},
                "geraete_einstellungen": [s.get("code") for s in stand.get("settings", [])],
                "phasen": [p.get("code") for p in stand.get("phases", [])],
                "spezifikation_geladen": stand.get("spezifikation_geladen"),
                "entscheidung": (stand.get("automation") or {}).get("decision", {}),
                "letzte_aktion": (stand.get("automation") or {}).get("last_action", ""),
                "handbetrieb_bis": (stand.get("automation") or {}).get("override_until"),
                "poller": stand.get("poller_intern", {}),
            },
        })

    jetzt = dt.datetime.now()
    return {
        "erzeugt_am": jetzt.isoformat(timespec="seconds"),
        "app": {
            **version,
            "laeuft_seit_minuten": round((time.time() - (zustaende[0].get("started_at", time.time())
                                          if zustaende else time.time())) / 60)
            if zustaende else None,
        },
        "umgebung": {
            "python": sys.version.split()[0],
            "system": platform.platform(),
            "zeitzone": time.tzname[0] if time.tzname else "?",
            "uhrzeit_lokal": jetzt.isoformat(timespec="seconds"),
            "uhrzeit_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "im_container": os.path.exists("/.dockerenv"),
            "konfigurationsdatei": str(CONFIG_FILE),
        },
        "zugang": {
            "tuya_projekt": {
                "access_id": _angedeutet(config.tuya.get("client_id")),
                "access_secret": _befund(config.tuya.get("client_secret")),
                "rechenzentrum": config.tuya.get("region"),
            },
            "qr_anmeldung": {
                "eingerichtet": bool((config.get("sharing") or {}).get("enabled")),
                "benutzercode": _angedeutet((config.get("sharing") or {}).get("user_code")),
                "token": _befund((config.get("sharing") or {}).get("token")),
                "offener_vorgang": bool((config.get("sharing") or {}).get("pending_token")),
            },
            "testzeitraum": trial,
        },
        "preise": {
            "quelle": preis_cfg.get("source"),
            "aufschlag_ct": preis_cfg.get("markup_ct"),
            "tibber_token": _befund((config.get("tibber") or {}).get("token")),
            "letzter_abruf_vor_s": preis_stand.get("age_seconds"),
            "fehler": preis_stand.get("error") or "",
            "waehrung": preis_stand.get("currency"),
            "stunden_heute": preis_stand.get("stunden_heute"),
            "stunden_morgen": preis_stand.get("stunden_morgen"),
        },
        "automatik": {
            **{k: v for k, v in auto.items() if k != "mitmachen"},
            "modus_klartext": automation.MODE_LABELS.get(auto["mode"], auto["mode"]),
        },
        "geraete": geraeteliste,
        "aufzeichnung": {
            # Steht bewusst als erstes Feld: Wer den Bericht ueberfliegt, sieht
            # Zahlen wie "13076 Messpunkte" und haelt sie leicht fuer den
            # Inhalt. Enthalten ist nur der Umfang.
            "hinweis": "nur Umfang und Namen der Groessen — die Messwerte "
                       "selbst sind NICHT Teil dieses Berichts",
            "intervall_s": config.get("history_seconds"),
            "abfrageintervall_s": config.get("refresh_seconds"),
            "aufbewahrung_tage": store.RETENTION_DAYS,
            **_historie(),
        },
        "letzte_ereignisse": [
            {"zeit": dt.datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds"),
             "art": e["kind"], "meldung": e["message"],
             "geraet": _kennung(e.get("device")) or "(App)"}
            for e in store.recent_events(60)
        ],
        "laufzeit": laufzeitumgebung(),
        "netz": netz or {"hinweis": "nicht geprueft"},
        "pakete": pakete(),
        "prozess": prozess(),
        "datenbank": datenbank(),
        "konfiguration_gesaeubert": _saeubern(dict(config._data)),
    }
