"""Tests der Schaltlogik und der Tuya-Datenaufbereitung — laufen ohne Cloud-Zugang.

Aufruf:  python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Muss vor dem Import von `app` stehen: Sonst legt die Konfiguration ihre Datei
# unter /config an — im Container richtig, beim Testen nicht schreibbar. Die
# Ereignis-Datenbank landet ebenfalls dort, deshalb reicht es nicht, nur die
# Konfiguration umzubiegen.
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="tuya-test-"))

from app import automation, prices  # noqa: E402
from app.tibber import cheapest_hours, upcoming  # noqa: E402
from app.tuya import build_view, decode_phase  # noqa: E402

NOW = dt.datetime(2026, 8, 16, 12, 30, tzinfo=dt.timezone.utc)


def price_day(values: list[float], day: str = "2026-08-16") -> list[dict]:
    """Tagesreihe bauen: 24 Stundenwerte in EUR/kWh."""
    return [
        {
            "startsAt": f"{day}T{hour:02d}:00:00.000+00:00",
            "total": value,
            "level": "CHEAP" if value < 0.25 else "EXPENSIVE",
        }
        for hour, value in enumerate(values)
    ]


class ThresholdMode(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = automation.settings({"enabled": True, "mode": "threshold", "threshold_ct": 25.0})

    def test_einschalten_unter_schwelle(self) -> None:
        prices = {"current": {"total": 0.1834, "level": "CHEAP", "startsAt": "x"}}
        decision = automation.decide(prices, self.cfg, NOW)
        self.assertTrue(decision.desired)
        self.assertEqual(decision.price_ct, 18.34)

    def test_ausschalten_ueber_schwelle(self) -> None:
        prices = {"current": {"total": 0.3120, "level": "EXPENSIVE", "startsAt": "x"}}
        self.assertFalse(automation.decide(prices, self.cfg, NOW).desired)

    def test_grenzwert_ist_inklusiv(self) -> None:
        prices = {"current": {"total": 0.25, "startsAt": "x"}}
        self.assertTrue(automation.decide(prices, self.cfg, NOW).desired)

    def test_ohne_preis_keine_entscheidung(self) -> None:
        self.assertIsNone(automation.decide({"current": {}}, self.cfg, NOW).desired)

    def test_automatik_aus_entscheidet_nicht(self) -> None:
        cfg = automation.settings({"enabled": False, "mode": "threshold"})
        prices = {"current": {"total": 0.10, "startsAt": "x"}}
        self.assertIsNone(automation.decide(prices, cfg, NOW).desired)


class CheapestMode(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = automation.settings(
            {"enabled": True, "mode": "cheapest", "cheapest_hours": 3}
        )
        # Guenstigste Stunden: 03, 04, 05
        self.today = price_day(
            [0.30, 0.29, 0.28, 0.10, 0.11, 0.12, 0.26, 0.27, 0.31, 0.32, 0.33, 0.34,
             0.35, 0.36, 0.37, 0.38, 0.39, 0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46]
        )

    def test_guenstige_stunde_schaltet_ein(self) -> None:
        prices = {"current": dict(self.today[3]), "today": self.today, "tomorrow": []}
        self.assertTrue(automation.decide(prices, self.cfg, NOW).desired)

    def test_teure_stunde_schaltet_aus(self) -> None:
        prices = {"current": dict(self.today[12]), "today": self.today, "tomorrow": []}
        self.assertFalse(automation.decide(prices, self.cfg, NOW).desired)

    def test_auswahl_beachtet_anzahl(self) -> None:
        cheap = cheapest_hours(self.today, 3)
        self.assertEqual(len(cheap), 3)
        self.assertIn(self.today[3]["startsAt"], cheap)
        self.assertNotIn(self.today[6]["startsAt"], cheap)


class LevelMode(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = automation.settings(
            {"enabled": True, "mode": "level", "levels": ["VERY_CHEAP", "CHEAP"]}
        )

    def test_passende_stufe_schaltet_ein(self) -> None:
        prices = {"current": {"total": 0.19, "level": "CHEAP", "startsAt": "x"}}
        self.assertTrue(automation.decide(prices, self.cfg, NOW).desired)

    def test_andere_stufe_schaltet_aus(self) -> None:
        prices = {"current": {"total": 0.41, "level": "VERY_EXPENSIVE", "startsAt": "x"}}
        self.assertFalse(automation.decide(prices, self.cfg, NOW).desired)


class SafetyNet(unittest.TestCase):
    def test_zwangs_ein_nach_max_off(self) -> None:
        cfg = automation.settings(
            {"enabled": True, "mode": "threshold", "threshold_ct": 5.0, "max_off_hours": 4}
        )
        prices = {"current": {"total": 0.40, "startsAt": "x"}}  # weit ueber der Schwelle
        off_since = NOW.timestamp() - 5 * 3600
        decision = automation.decide(prices, cfg, NOW, off_since=off_since)
        self.assertTrue(decision.desired)
        self.assertIn("Sicherheitsnetz", decision.reason)

    def test_vor_ablauf_bleibt_aus(self) -> None:
        cfg = automation.settings(
            {"enabled": True, "mode": "threshold", "threshold_ct": 5.0, "max_off_hours": 4}
        )
        prices = {"current": {"total": 0.40, "startsAt": "x"}}
        off_since = NOW.timestamp() - 1 * 3600
        self.assertFalse(automation.decide(prices, cfg, NOW, off_since=off_since).desired)


class SettingsNormalisierung(unittest.TestCase):
    def test_unsinn_wird_begrenzt(self) -> None:
        cfg = automation.settings(
            {"mode": "quatsch", "cheapest_hours": 99, "max_off_hours": -5, "levels": ["FALSCH"]}
        )
        self.assertEqual(cfg["mode"], "threshold")
        self.assertEqual(cfg["cheapest_hours"], 24)
        self.assertEqual(cfg["max_off_hours"], 0)
        self.assertEqual(cfg["levels"], ["VERY_CHEAP", "CHEAP"])


class TuyaAufbereitung(unittest.TestCase):
    def test_skalierung_und_einheit(self) -> None:
        spec = {
            "status": [
                {"code": "cur_voltage", "values": '{"unit":"V","scale":1,"min":0,"max":2500}'},
                {"code": "cur_power", "values": '{"unit":"W","scale":1}'},
            ],
            "functions": [{"code": "switch", "type": "Boolean", "values": "{}"}],
        }
        status = [
            {"code": "cur_voltage", "value": 2312},
            {"code": "cur_power", "value": 4560},
            {"code": "switch", "value": True},
        ]
        view = build_view(spec, status)
        volt = next(m for m in view["metrics"] if m["code"] == "cur_voltage")
        self.assertEqual(volt["value"], 231.2)
        self.assertEqual(volt["unit"], "V")
        self.assertEqual(view["switches"][0]["code"], "switch")
        self.assertTrue(view["switches"][0]["value"])

    def test_schalter_erscheint_nicht_als_messwert(self) -> None:
        spec = {"status": [], "functions": [{"code": "switch", "type": "Boolean"}]}
        view = build_view(spec, [{"code": "switch", "value": False}])
        self.assertEqual([m["code"] for m in view["metrics"]], [])
        self.assertFalse(view["switches"][0]["value"])

    def test_phasendaten_dekodieren(self) -> None:
        # 230,5 V / 2,345 A / 540 W
        raw = (2305).to_bytes(2, "big") + (2345).to_bytes(3, "big") + (540).to_bytes(3, "big")
        decoded = decode_phase(base64.b64encode(raw).decode())
        self.assertAlmostEqual(decoded["voltage_v"], 230.5)
        self.assertAlmostEqual(decoded["current_a"], 2.345)
        self.assertEqual(decoded["power_w"], 540)

    def test_kaputte_phasendaten_werfen_nicht(self) -> None:
        self.assertIsNone(decode_phase("nonsense!!"))


class Anzeigehelfer(unittest.TestCase):
    def test_upcoming_ueberspringt_vergangenes(self) -> None:
        today = price_day([0.20] * 24)
        rows = upcoming(today, NOW, hours=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["hour"], "12:00")  # laufende Stunde bleibt drin



class Preisquellen(unittest.TestCase):
    """prices.py — Aggregation, Aufschlag, Preisstufen."""

    def test_viertelstunden_werden_zu_stunden(self) -> None:
        roh = []
        for stunde in range(2):
            for viertel in range(4):
                roh.append(
                    {
                        "startsAt": f"2026-08-16T{stunde:02d}:{viertel * 15:02d}:00+00:00",
                        "total": 0.20 + viertel * 0.01,
                        "spot": 0.05,
                    }
                )
        stunden = prices.to_hourly(roh)
        self.assertEqual(len(stunden), 2)
        self.assertEqual(stunden[0]["slots"], 4)
        # Mittel aus 0.20, 0.21, 0.22, 0.23
        self.assertAlmostEqual(stunden[0]["total"], 0.215, places=6)

    def test_stundenwerte_bleiben_unveraendert(self) -> None:
        roh = [
            {"startsAt": "2026-08-16T00:00:00+00:00", "total": 0.30, "spot": 0.10},
            {"startsAt": "2026-08-16T01:00:00+00:00", "total": 0.25, "spot": 0.08},
        ]
        stunden = prices.to_hourly(roh)
        self.assertEqual(len(stunden), 2)
        self.assertEqual(stunden[0]["slots"], 1)
        self.assertAlmostEqual(stunden[0]["total"], 0.30)

    def test_aufschlag_und_mwst(self) -> None:
        cfg = prices.settings({"markup_ct": 20.0, "vat_percent": 19.0})
        # 10 ct Boerse + 20 ct Aufschlag = 30 ct netto -> 35,7 ct brutto
        self.assertAlmostEqual(prices.apply_markup(0.10, cfg) * 100, 35.7, places=4)

    def test_aufschlag_ohne_mwst(self) -> None:
        cfg = prices.settings({"markup_ct": 0.0, "vat_percent": 0.0})
        self.assertAlmostEqual(prices.apply_markup(0.1234, cfg), 0.1234, places=6)

    def test_preisstufen_relativ_zum_tagesmittel(self) -> None:
        entries = [{"total": t} for t in [0.10, 0.20, 0.20, 0.50]]  # Mittel 0.25
        prices.classify(entries)
        self.assertEqual(entries[0]["level"], "VERY_CHEAP")   # 0.40 x Mittel
        self.assertEqual(entries[1]["level"], "CHEAP")        # 0.80 x Mittel
        self.assertEqual(entries[3]["level"], "VERY_EXPENSIVE")  # 2.00 x Mittel

    def test_unbekannte_quelle_faellt_zurueck(self) -> None:
        self.assertEqual(prices.settings({"source": "quatsch"})["source"], "awattar_de")

    def test_spot_erkennung(self) -> None:
        self.assertTrue(prices.is_spot("awattar_de"))
        self.assertFalse(prices.is_spot("tibber"))


class OhneSpezifikation(unittest.TestCase):
    """Lokaler Zugang und QR-Anmeldung liefern nur Codes und Werte.

    Ohne Entwicklerprojekt gibt es keine Spezifikation. Wird der Schalter dann
    nicht am Namen erkannt, ist eine einfache Schaltsteckdose auf diesen Wegen
    ueberhaupt nicht bedienbar.
    """

    STATUS = [
        {"code": "switch_1", "value": True},
        {"code": "countdown_1", "value": 0},
        {"code": "cur_power", "value": 1234},
        {"code": "cur_voltage", "value": 2310},
        {"code": "cur_current", "value": 5300},
        {"code": "child_lock", "value": False},
        {"code": "switch_backlight", "value": True},
    ]

    def setUp(self) -> None:
        self.view = build_view({}, self.STATUS)

    def test_schalter_wird_am_namen_erkannt(self) -> None:
        self.assertEqual([s["code"] for s in self.view["switches"]], ["switch_1"])
        self.assertTrue(self.view["switches"][0]["value"])
        self.assertTrue(self.view["switches"][0]["present"])

    def test_beleuchtung_ist_kein_ausgang(self) -> None:
        """switch_backlight faengt mit 'switch' an, schaltet aber nur die Anzeige."""
        codes = [s["code"] for s in self.view["switches"]]
        self.assertNotIn("switch_backlight", codes)
        self.assertNotIn("child_lock", codes)

    def test_messwerte_bekommen_einheit_und_skalierung(self) -> None:
        werte = {m["code"]: (m["value"], m["unit"]) for m in self.view["metrics"]}
        self.assertEqual(werte["cur_voltage"], (231.0, "V"))
        self.assertEqual(werte["cur_power"], (123.4, "W"))
        self.assertEqual(werte["cur_current"], (5.3, "A"))   # mA -> A

    def test_ja_nein_werte_sind_keine_messwerte(self) -> None:
        for eintrag in self.view["metrics"]:
            self.assertNotIsInstance(eintrag["value"], bool, eintrag["code"])

    def test_schalter_steht_nicht_doppelt_in_den_messwerten(self) -> None:
        self.assertNotIn("switch_1", [m["code"] for m in self.view["metrics"]])

    def test_zaehlerstand_wird_nicht_geraten(self) -> None:
        """18 kann 0,18 / 1,8 / 18 kWh sein — ohne Spezifikation bleibt es roh.

        Eine geratene Kommastelle sieht aus wie eine Messung und ist keine.
        """
        view = build_view({}, [{"code": "total_ele", "value": 18}])
        wert = next(m for m in view["metrics"] if m["code"] == "total_ele")
        self.assertEqual(wert["value"], 18)
        self.assertEqual(wert["unit"], "")

    def test_mit_spezifikation_gilt_deren_skalierung(self) -> None:
        spec = {"status": [{"code": "total_ele", "values": '{"unit":"kWh","scale":2}'}],
                "functions": []}
        view = build_view(spec, [{"code": "total_ele", "value": 18}])
        wert = next(m for m in view["metrics"] if m["code"] == "total_ele")
        self.assertEqual(wert["value"], 0.18)
        self.assertEqual(wert["unit"], "kWh")


class Protokollversionen(unittest.TestCase):
    """Lokaler Zugang: 3.3 bis 3.5 muessen alle funktionieren.

    Neuere Geraete sprechen 3.4 oder 3.5 und handeln dabei einen
    Sitzungsschluessel aus. Wird die Version nicht durchprobiert oder die
    Verbindung fuer den Handshake nicht gehalten, bleibt so ein Geraet lokal
    stumm -- und faellt still auf den befristeten Cloud-Weg zurueck.
    """

    class FakeDevice:
        """Antwortet nur auf eine bestimmte Protokollversion."""

        erzeugt: list = []

        def __init__(self, spricht: float) -> None:
            self.spricht = spricht
            self.version = None
            self.persistent = False
            self.geschlossen = False

        def set_version(self, v): self.version = v
        def set_socketTimeout(self, t): pass
        def set_socketRetryLimit(self, n): pass
        def set_socketPersistent(self, an): self.persistent = an
        def close(self): self.geschlossen = True

        def status(self):
            if self.version == self.spricht:
                return {"dps": {"1": True, "20": 2310}}
            return {"Error": "Check device key or version"}

    def geraet_mit(self, spricht: float):
        from app import local

        erzeugte = []

        def fabrik(version):
            d = self.FakeDevice(spricht)
            d.set_version(version)
            d.set_socketTimeout(5)
            d.set_socketRetryLimit(1)
            if version >= 3.4:
                d.set_socketPersistent(True)
            erzeugte.append(d)
            return d

        dev = local.LocalDevice("id", "10.0.0.9", "key", {"1": "switch_1", "20": "cur_voltage"})
        dev._verbindung = fabrik
        return dev, erzeugte

    def test_jede_version_wird_gefunden(self) -> None:
        for spricht in (3.3, 3.4, 3.5, 3.1):
            dev, _ = self.geraet_mit(spricht)
            werte = dev._status_roh()
            self.assertEqual(werte["20"], 2310, f"Version {spricht}")
            self.assertEqual(dev.version, spricht)

    def test_erkannte_version_wird_behalten(self) -> None:
        """Danach nicht wieder alle durchprobieren — das kostet bei jedem Abruf Zeit."""
        dev, erzeugte = self.geraet_mit(3.5)
        dev._status_roh()
        anzahl_erste_runde = len(erzeugte)
        dev._status_roh()
        self.assertEqual(len(erzeugte) - anzahl_erste_runde, 1)

    def test_handshake_versionen_halten_die_verbindung(self) -> None:
        dev, erzeugte = self.geraet_mit(3.5)
        dev._status_roh()
        nach_version = {d.version: d for d in erzeugte}
        self.assertFalse(nach_version[3.3].persistent)
        self.assertTrue(nach_version[3.4].persistent)
        self.assertTrue(nach_version[3.5].persistent)

    def test_verbindungen_werden_wieder_geschlossen(self) -> None:
        dev, erzeugte = self.geraet_mit(3.5)
        dev._status_roh()
        self.assertTrue(all(d.geschlossen for d in erzeugte))

    def test_stummes_geraet_meldet_klaren_fehler(self) -> None:
        from app import local

        dev, _ = self.geraet_mit(9.9)          # spricht keine der Versionen
        with self.assertRaises(local.LocalError) as fehler:
            dev._status_roh()
        self.assertIn("antwortet nicht", str(fehler.exception))


class Testzeitraum(unittest.TestCase):
    """Die Warnung zum Tuya-Testzeitraum darf nicht falsch anschlagen."""

    def setUp(self) -> None:
        from app import geraete, main
        from app.config import config

        self.main = main
        self.config = config
        main._states.clear()
        geraete.speichern([{"id": "zaehler", "name": "Zaehler"}])
        config.set("trial_expires", "")
        config.set("tuya_setup_ts", time.time())

    def test_eingetragenes_datum_schlaegt_fehlercodes(self) -> None:
        """Anlass: Ein zweites, gar nicht vorhandenes Geraet meldete 1106 --
        und die App behauptete daraufhin, der Testzeitraum sei abgelaufen,
        obwohl das eingetragene Datum noch Wochen entfernt lag."""
        in_30_tagen = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        self.config.set("trial_expires", in_30_tagen)

        st = self.main.zustand("zaehler")
        st.error = "Keine Berechtigung (Code 1106)"

        status = self.main.trial_status()
        self.assertFalse(status["expired"])
        self.assertEqual(status["days_left"], 30)

    def test_abgelaufenes_datum_bleibt_abgelaufen(self) -> None:
        gestern = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        self.config.set("trial_expires", gestern)
        self.assertTrue(self.main.trial_status()["expired"])

    def test_ohne_datum_zaehlt_ein_einzelner_rechtefehler_nicht(self) -> None:
        """Ein Geraet mit Rechtefehler, ein anderes liest ueber die Cloud."""
        from app import geraete

        geraete.speichern([{"id": "zaehler", "name": "Zaehler"},
                           {"id": "gibtsnicht", "name": "Fremd"}])
        laeuft = self.main.zustand("zaehler")
        laeuft.kanal, laeuft.ok = "cloud", True
        kaputt = self.main.zustand("gibtsnicht")
        kaputt.error = "Keine Berechtigung (Code 1106)"

        self.assertFalse(self.main.trial_status()["expired"])

    def test_ohne_datum_und_ohne_jeden_zugriff_gilt_er_als_abgelaufen(self) -> None:
        st = self.main.zustand("zaehler")
        st.error = "Tuya-API: [1114] token expired"
        st.ok = False
        self.assertTrue(self.main.trial_status()["expired"])

    def test_gewoehnlicher_fehler_gilt_nicht_als_ablauf(self) -> None:
        st = self.main.zustand("zaehler")
        st.error = "Geraet unter 192.168.1.50 nicht erreichbar"
        self.assertFalse(self.main.trial_status()["expired"])


class GeraetelisteAnzeige(unittest.TestCase):
    """Die Geraeteseite wirklich rendern und die Schalter darin pruefen.

    Anlass: Das Feld `automatik_aktiv` wurde der Vorlage gar nicht uebergeben.
    Das Haekchen "folgt der Regel" blieb dadurch immer leer, egal was
    gespeichert war -- es liess sich nicht umlegen, weil die Anzeige den
    gespeicherten Stand nie uebernahm. Ein Test der Logik allein haette das
    nicht gefunden: Gespeichert wurde korrekt.
    """

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from app import geraete, main
        from app.config import config

        config.set_admin_password("probe1234")
        config.set("setup_done", True)
        main.logged_in = lambda request: True
        geraete.speichern([
            {"id": "a", "name": "Zaehler", "automatik_aktiv": True, "aktiv": True},
            {"id": "b", "name": "Steckdose", "automatik_aktiv": False, "aktiv": False},
        ])
        self.client = TestClient(main.app)
        self.zeilen = self._zeilen(self.client.get("/devices").text)

    def tearDown(self) -> None:
        self.client.close()

    @staticmethod
    def _zeilen(html: str) -> dict:
        import re

        out = {}
        for zeile in re.findall(r"<tr.*?</tr>", html, re.S):
            name = re.search(r"<strong[^>]*>\s*([^<]+?)\s*</strong>", zeile)
            if name:
                out[name.group(1)] = zeile
            elif out:
                # Folgezeile mit den Schaltflaechen gehoert zum letzten Geraet
                out[list(out)[-1]] += zeile
        return out

    @staticmethod
    def _gehakt(zeile: str, feld: str) -> bool:
        if f'name="{feld}"' not in zeile:
            return False
        return "checked" in zeile.split(f'name="{feld}"')[1].split(">")[0]

    def test_regel_haekchen_zeigt_den_gespeicherten_stand(self) -> None:
        self.assertTrue(self._gehakt(self.zeilen["Zaehler"], "mitmachen"))
        self.assertFalse(self._gehakt(self.zeilen["Steckdose"], "mitmachen"))

    def test_beschriftung_passt_zum_haekchen(self) -> None:
        self.assertIn("folgt der Regel", self.zeilen["Zaehler"])
        self.assertIn("nur von Hand", self.zeilen["Steckdose"])

    def test_abfragen_haekchen_zeigt_den_gespeicherten_stand(self) -> None:
        self.assertTrue(self._gehakt(self.zeilen["Zaehler"], "aktiv"))
        self.assertFalse(self._gehakt(self.zeilen["Steckdose"], "aktiv"))

    def test_umschalten_wirkt_und_erscheint_in_der_anzeige(self) -> None:
        from app import geraete

        self.client.post("/devices/automatik", data={"device_id": "a"},
                         follow_redirects=False)
        self.assertFalse(geraete.holen("a")["automatik_aktiv"])
        zeilen = self._zeilen(self.client.get("/devices").text)
        self.assertFalse(self._gehakt(zeilen["Zaehler"], "mitmachen"))

        self.client.post("/devices/automatik",
                         data={"device_id": "a", "mitmachen": "on"},
                         follow_redirects=False)
        self.assertTrue(geraete.holen("a")["automatik_aktiv"])
        zeilen = self._zeilen(self.client.get("/devices").text)
        self.assertTrue(self._gehakt(zeilen["Zaehler"], "mitmachen"))


class AutomatikZustaende(unittest.TestCase):
    """Zwei Schalter, vier Faelle -- die Oberflaeche muss sie unterscheiden.

    Anlass: Ein Geraet auf "nur von Hand" meldete auf der Uebersicht
    "Automatik ist aus" und verlinkte auf die Regelseite. Dort war alles
    richtig eingestellt, denn ausgenommen war nur dieses eine Geraet -- eine
    Sackgasse ohne Weg zurueck.
    """

    def setUp(self) -> None:
        from app import geraete, main
        from app.config import config

        self.main = main
        self.config = config
        main._states.clear()
        geraete.speichern([{"id": "a", "name": "Zaehler", "automatik_aktiv": True}])

    def lage(self, regel_an: bool, macht_mit: bool) -> dict:
        from app import geraete

        self.config.set("automation", {"enabled": regel_an, "mode": "threshold",
                                       "threshold_ct": 25.0})
        geraete.aktualisieren("a", automatik_aktiv=macht_mit)
        return self.main.zustand("a").as_dict()["automation"]

    def test_regel_aus_ist_von_geraet_ausgenommen_unterscheidbar(self) -> None:
        aus = self.lage(regel_an=False, macht_mit=True)
        self.assertFalse(aus["regel_aktiv"])
        self.assertTrue(aus["mitmachen"])

        ausgenommen = self.lage(regel_an=True, macht_mit=False)
        self.assertTrue(ausgenommen["regel_aktiv"])
        self.assertFalse(ausgenommen["mitmachen"])

    def test_zusammengefasster_wert_bleibt_richtig(self) -> None:
        """`enabled` beantwortet weiterhin: Wird DIESES Geraet automatisch geschaltet?"""
        self.assertFalse(self.lage(regel_an=False, macht_mit=True)["enabled"])
        self.assertFalse(self.lage(regel_an=True, macht_mit=False)["enabled"])
        self.assertFalse(self.lage(regel_an=False, macht_mit=False)["enabled"])
        self.assertTrue(self.lage(regel_an=True, macht_mit=True)["enabled"])

    def test_begruendung_nennt_den_richtigen_grund(self) -> None:
        import asyncio

        self.lage(regel_an=True, macht_mit=False)
        st = self.main.zustand("a")
        asyncio.run(self.main.apply_automation(st))
        self.assertIn("dieses Gerät", st.last_decision["reason"])

        self.lage(regel_an=False, macht_mit=True)
        asyncio.run(self.main.apply_automation(st))
        self.assertIn("Automatik ist aus", st.last_decision["reason"])

    def test_rueckkehrziel_bleibt_in_der_app(self) -> None:
        """Ein Ziel aus dem Formular darf nicht auf eine fremde Seite fuehren."""
        self.assertEqual(self.main.sicheres_ziel("/", "/devices"), "/")
        self.assertEqual(self.main.sicheres_ziel("/?device=a", "/devices"), "/?device=a")
        self.assertEqual(self.main.sicheres_ziel("//example.com", "/devices"), "/devices")
        self.assertEqual(self.main.sicheres_ziel("https://example.com", "/devices"), "/devices")
        self.assertEqual(self.main.sicheres_ziel("", "/devices"), "/devices")


class Fehlermeldungen(unittest.TestCase):
    """Was bei einem Rechtefehler dasteht, muss zur Lage passen."""

    def setUp(self) -> None:
        from app import geraete, main
        from app.config import config
        from app.tuya import TuyaError

        self.main = main
        self.config = config
        self.fehler = TuyaError(1106, "no permissions", "/v1.0/devices/x")
        main._states.clear()
        geraete.speichern([{"id": "a", "name": "A"}])

    def test_ferne_frist_wird_nicht_erwaehnt(self) -> None:
        """Sonst schickt die Meldung auf eine falsche Faehrte."""
        self.config.set("trial_expires",
                        (dt.date.today() + dt.timedelta(days=30)).isoformat())
        text = self.main.tuya_error_hint(self.fehler, "x" * 20, "")
        self.assertNotIn("Extend Trial", text)

    def test_nahe_frist_wird_erwaehnt(self) -> None:
        self.config.set("trial_expires",
                        (dt.date.today() + dt.timedelta(days=3)).isoformat())
        self.assertIn("Extend Trial", self.main.tuya_error_hint(self.fehler, "x" * 20, ""))

    def test_ohne_datum_wird_erwaehnt(self) -> None:
        self.config.set("trial_expires", "")
        self.assertIn("Extend Trial", self.main.tuya_error_hint(self.fehler, "x" * 20, ""))

    def test_meldung_nennt_das_geraet_und_die_adresse(self) -> None:
        text = self.main.tuya_error_hint(self.fehler, "x" * 20, "", geraet="Steckdose")
        self.assertIn("Steckdose", text)
        self.assertIn("iot.tuya.com", text)

    def test_haeufigste_ursache_steht_vorn(self) -> None:
        """Bei 1106 ist das die fehlende Verknuepfung des Geraets."""
        text = self.main.tuya_error_hint(self.fehler, "x" * 20, "")
        self.assertLess(text.index("Link App Account"), text.index("IoT Core"))


class Geraetebestand(unittest.TestCase):
    """Mehrere Geraete nebeneinander, jedes mit eigener Regel."""

    def setUp(self) -> None:
        from app import geraete
        self.geraete = geraete
        geraete.speichern([])

    def test_uebergang_vom_einzelgeraet(self) -> None:
        """Wer mit einem Geraet gestartet ist, findet es in der Liste wieder."""
        from app.config import config

        config.set("devices", None)
        config.set("device_id", "altes-geraet")
        config.set("device_name", "Zaehler")
        config.set("local", {"enabled": True, "ip": "192.168.1.50", "key": "abc"})
        config.set("automation", {"enabled": True, "mode": "cheapest", "cheapest_hours": 5,
                                  "switch_code": "switch_1"})

        alle = self.geraete.liste()
        self.assertEqual([e["id"] for e in alle], ["altes-geraet"])
        self.assertEqual(alle[0]["name"], "Zaehler")
        self.assertEqual(alle[0]["local"]["ip"], "192.168.1.50")
        # Die Regel bleibt gemeinsam; vom Geraet kommt nur der Schaltkanal.
        self.assertEqual(config.get("automation")["cheapest_hours"], 5)
        self.assertTrue(alle[0]["automatik_aktiv"])

    def test_gemeinsame_regel_je_geraet_an_oder_aus(self) -> None:
        """Eine Regel fuer alle; pro Geraet nur, ob es ihr folgt."""
        from app import main
        from app.config import config

        config.set("automation", {"enabled": True, "mode": "threshold", "threshold_ct": 20.0})
        self.geraete.hinzufuegen("zaehler", "Zaehler")
        self.geraete.hinzufuegen("steckdose", "Steckdose 16A")
        self.geraete.aktualisieren("steckdose", automatik_aktiv=False, switch_code="switch_1")

        z, d = main.zustand("zaehler"), main.zustand("steckdose")
        self.assertTrue(z.auto["enabled"])
        self.assertFalse(d.auto["enabled"])          # macht nicht mit
        self.assertEqual(z.auto["threshold_ct"], 20.0)
        self.assertEqual(d.auto["threshold_ct"], 20.0)   # dieselbe Regel
        self.assertEqual(d.auto["switch_code"], "switch_1")

    def test_regel_gilt_sofort_fuer_neue_geraete(self) -> None:
        from app import main
        from app.config import config

        config.set("automation", {"enabled": True, "mode": "cheapest", "cheapest_hours": 4})
        self.geraete.hinzufuegen("spaeter", "Neue Steckdose")
        self.assertEqual(main.zustand("spaeter").auto["cheapest_hours"], 4)
        self.assertTrue(main.zustand("spaeter").auto["enabled"])

    def test_lokaler_zugang_ist_je_geraet_verschieden(self) -> None:
        self.geraete.hinzufuegen("zaehler", "Zaehler")
        self.geraete.hinzufuegen("steckdose", "Steckdose")
        self.geraete.aktualisieren("zaehler", local={"enabled": True, "ip": "10.0.0.5", "key": "k1"})
        self.geraete.aktualisieren("steckdose", local={"enabled": True, "ip": "10.0.0.6", "key": "k2"})
        self.assertEqual(self.geraete.holen("zaehler")["local"]["ip"], "10.0.0.5")
        self.assertEqual(self.geraete.holen("steckdose")["local"]["key"], "k2")

    def test_altfelder_zeigen_auf_das_erste_geraet(self) -> None:
        """Bestehende Anbindungen lesen weiter das, was sie bisher gelesen haben."""
        from app.config import config

        self.geraete.hinzufuegen("erstes", "Eins")
        self.geraete.hinzufuegen("zweites", "Zwei")
        self.assertEqual(config.get("device_id"), "erstes")
        self.assertEqual(config.get("device_name"), "Eins")

    def test_entfernen_laesst_die_uebrigen_stehen(self) -> None:
        self.geraete.hinzufuegen("a", "A")
        self.geraete.hinzufuegen("b", "B")
        self.assertTrue(self.geraete.entfernen("a"))
        self.assertEqual([e["id"] for e in self.geraete.liste()], ["b"])
        self.assertFalse(self.geraete.entfernen("gibtsnicht"))

    def test_ruhendes_geraet_wird_nicht_abgefragt(self) -> None:
        """Im Bestand behalten, aber still — ohne Loeschen und ohne Fehlalarm."""
        self.geraete.hinzufuegen("laeuft", "Zaehler")
        self.geraete.hinzufuegen("kommt-noch", "Steckdose")
        self.geraete.aktualisieren("kommt-noch", aktiv=False)

        self.assertEqual([e["id"] for e in self.geraete.aktive()], ["laeuft"])
        self.assertEqual(len(self.geraete.liste()), 2)      # bleibt im Bestand
        self.assertEqual(self.geraete.holen("kommt-noch")["name"], "Steckdose")

    def test_ruhendes_geraet_wieder_aufwecken(self) -> None:
        self.geraete.hinzufuegen("a", "A")
        self.geraete.aktualisieren("a", aktiv=False)
        self.assertEqual(self.geraete.aktive(), [])
        self.geraete.aktualisieren("a", aktiv=True)
        self.assertEqual([e["id"] for e in self.geraete.aktive()], ["a"])

    def test_doppeltes_hinzufuegen_benennt_nur_um(self) -> None:
        self.geraete.hinzufuegen("a", "Alter Name")
        self.geraete.hinzufuegen("a", "Neuer Name")
        self.assertEqual(len(self.geraete.liste()), 1)
        self.assertEqual(self.geraete.holen("a")["name"], "Neuer Name")


class Fremdschaltung(unittest.TestCase):
    """Erkennung von Schaltvorgaengen, die nicht aus dieser App kamen."""

    def setUp(self) -> None:
        from app import geraete, main
        self.main = main
        self.geraete = geraete
        geraete.speichern([{"id": "geraet-a", "name": "Zaehler"}])
        self.state = main.zustand("geraet-a")
        self.state.last_seen = None
        self.state.expected_state = None
        self.state.last_action = ""
        self.auto = automation.settings(
            {"enabled": True, "switch_code": "switch", "override_minutes": 60}
        )
        geraete.handbetrieb_setzen("geraet-a", 0)

    def pause(self) -> float:
        return self.geraete.handbetrieb_bis("geraet-a")

    def test_erste_messung_loest_nichts_aus(self) -> None:
        self.main.note_switch_state(self.state, True, self.auto)
        self.assertEqual(self.pause(), 0)

    def test_unveraenderter_zustand_loest_nichts_aus(self) -> None:
        self.main.note_switch_state(self.state, True, self.auto)
        self.main.note_switch_state(self.state, True, self.auto)
        self.assertEqual(self.pause(), 0)

    def test_eigene_schaltung_gilt_nicht_als_fremd(self) -> None:
        self.main.note_switch_state(self.state, True, self.auto)
        self.state.expected_state = False          # wir schalten selbst aus
        self.main.note_switch_state(self.state, False, self.auto)
        self.assertEqual(self.pause(), 0)
        self.assertIsNone(self.state.expected_state)

    def test_fremde_schaltung_pausiert_die_automatik(self) -> None:
        self.main.note_switch_state(self.state, False, self.auto)
        self.main.note_switch_state(self.state, True, self.auto)  # jemand in der Tuya-App
        self.assertGreater(self.pause(), time.time())
        self.assertIn("von Hand", self.state.last_action)

    def test_ohne_pausenzeit_keine_pause(self) -> None:
        auto = automation.settings({"enabled": True, "override_minutes": 0})
        self.main.note_switch_state(self.state, False, auto)
        self.main.note_switch_state(self.state, True, auto)
        self.assertEqual(self.pause(), 0)

    def test_pause_gilt_nur_fuer_das_betroffene_geraet(self) -> None:
        """Eine Handbedienung an einem Geraet darf das andere nicht anhalten."""
        self.geraete.speichern([
            {"id": "geraet-a", "name": "Zaehler"},
            {"id": "geraet-b", "name": "Steckdose"},
        ])
        a = self.main.zustand("geraet-a")
        a.last_seen = None
        self.main.note_switch_state(a, False, self.auto)
        self.main.note_switch_state(a, True, self.auto)
        self.assertGreater(self.geraete.handbetrieb_bis("geraet-a"), time.time())
        self.assertEqual(self.geraete.handbetrieb_bis("geraet-b"), 0)


class BlockModus(unittest.TestCase):
    """Guenstigster zusammenhaengender Block."""

    def setUp(self) -> None:
        self.cfg = automation.settings(
            {"enabled": True, "mode": "cheapest_block", "cheapest_hours": 3}
        )
        # Billig am Stueck: 04,05,06 (Summe 0.33). Einzelne Ausreisser bei 12 und 20,
        # die die verstreute Auswahl nehmen wuerde, den Block aber nicht.
        self.today = price_day(
            [0.40, 0.39, 0.38, 0.37, 0.11, 0.11, 0.11, 0.36, 0.35, 0.34, 0.33, 0.32,
             0.05, 0.31, 0.30, 0.29, 0.28, 0.27, 0.26, 0.25, 0.05, 0.24, 0.23, 0.22]
        )
        self.frueh = dt.datetime(2026, 8, 16, 0, 30, tzinfo=dt.timezone.utc)

    def test_block_ist_zusammenhaengend(self) -> None:
        block = automation.cheapest_block(self.today, 3, self.frueh)
        stunden = sorted(int(s[11:13]) for s in block)
        self.assertEqual(stunden, [4, 5, 6])

    def test_einzelne_ausreisser_werden_nicht_gepflueckt(self) -> None:
        # Die verstreute Auswahl wuerde 12, 20 und eine 0.11er nehmen.
        verstreut = sorted(int(s[11:13]) for s in cheapest_hours(self.today, 3))
        block = sorted(int(s[11:13]) for s in automation.cheapest_block(self.today, 3, self.frueh))
        self.assertIn(12, verstreut)
        self.assertNotIn(12, block)

    def test_stunde_im_block_schaltet_ein(self) -> None:
        prices = {"current": dict(self.today[5]), "today": self.today, "tomorrow": []}
        entscheidung = automation.decide(prices, self.cfg, self.frueh)
        self.assertTrue(entscheidung.desired)

    def test_stunde_ausserhalb_schaltet_aus(self) -> None:
        prices = {"current": dict(self.today[12]), "today": self.today, "tomorrow": []}
        self.assertFalse(automation.decide(prices, self.cfg, self.frueh).desired)

    def test_vergangene_bloecke_scheiden_aus(self) -> None:
        # Um 20:30 ist der 04-06-Block vorbei; es muss ein spaeterer gewaehlt werden.
        spaet = dt.datetime(2026, 8, 16, 20, 30, tzinfo=dt.timezone.utc)
        block = automation.cheapest_block(self.today, 3, spaet)
        stunden = sorted(int(s[11:13]) for s in block)
        self.assertTrue(min(stunden) >= 19, f"Block liegt in der Vergangenheit: {stunden}")

    def test_luecken_werden_nicht_ueberbrueckt(self) -> None:
        # Stunde 2 fehlt - 1,3,4 darf kein gueltiger Block sein.
        loechrig = [e for e in price_day([0.10] * 24) if int(e["startsAt"][11:13]) != 2]
        block = automation.cheapest_block(loechrig, 3, self.frueh)
        stunden = sorted(int(s[11:13]) for s in block)
        for a, b in zip(stunden, stunden[1:]):
            self.assertEqual(b - a, 1, f"Block hat eine Luecke: {stunden}")

    def test_zu_wenige_daten(self) -> None:
        self.assertEqual(automation.cheapest_block(price_day([0.2] * 2), 5, self.frueh), set())

    def test_block_ueber_mitternacht(self) -> None:
        heute = price_day([0.40] * 24)
        morgen = price_day([0.40] * 24, day="2026-08-17")
        # Guenstig: heute 23 Uhr und morgen 00/01 Uhr
        heute[23]["total"] = 0.05
        morgen[0]["total"] = 0.05
        morgen[1]["total"] = 0.05
        block = automation.cheapest_block(heute + morgen, 3, self.frueh)
        self.assertEqual(len(block), 3)
        self.assertIn(heute[23]["startsAt"], block)
        self.assertIn(morgen[1]["startsAt"], block)

    def test_mindestlaufzeit_wird_normalisiert(self) -> None:
        self.assertEqual(automation.settings({"min_on_minutes": -5})["min_on_minutes"], 0)
        self.assertEqual(automation.settings({"min_on_minutes": 9999})["min_on_minutes"], 1440)


class GeraeteAufbereitungEcht(unittest.TestCase):
    """Gegen die echte Spezifikation eines DDS238-2 WIFI geprueft."""

    SPEC = {
        "functions": [
            {"code": "switch_1", "type": "Boolean", "values": "{}"},
            {"code": "countdown_1", "type": "Integer",
             "values": '{"unit":"s","min":0,"max":86400,"scale":0,"step":1}'},
        ],
        "status": [
            {"code": "switch_1", "type": "Boolean", "values": "{}"},
            {"code": "countdown_1", "type": "Integer",
             "values": '{"unit":"s","min":0,"max":86400,"scale":0,"step":1}'},
            {"code": "cur_current", "type": "Integer",
             "values": '{"unit":"mA","min":0,"max":100000,"scale":0,"step":1}'},
            {"code": "cur_power", "type": "Integer",
             "values": '{"unit":"W","min":0,"max":500000,"scale":1,"step":1}'},
            {"code": "cur_voltage", "type": "Integer",
             "values": '{"unit":"V","min":0,"max":5000,"scale":1,"step":1}'},
        ],
    }

    def view(self, **werte):
        status = [{"code": k, "value": v} for k, v in werte.items()]
        return build_view(self.SPEC, status)

    def test_milliampere_werden_ampere(self) -> None:
        v = self.view(cur_current=16000)
        strom = next(m for m in v["metrics"] if m["code"] == "cur_current")
        self.assertEqual(strom["value"], 16.0)
        self.assertEqual(strom["unit"], "A")

    def test_skalierte_werte(self) -> None:
        v = self.view(cur_voltage=2315, cur_power=12345)
        volt = next(m for m in v["metrics"] if m["code"] == "cur_voltage")
        watt = next(m for m in v["metrics"] if m["code"] == "cur_power")
        self.assertEqual(volt["value"], 231.5)
        self.assertEqual(watt["value"], 1234.5)

    def test_timer_ist_einstellung_kein_messwert(self) -> None:
        v = self.view(countdown_1=3600, cur_power=100)
        self.assertNotIn("countdown_1", [m["code"] for m in v["metrics"]])
        self.assertIn("countdown_1", [x["code"] for x in v["settings"]])

    def test_schaltkanal_heisst_switch_1(self) -> None:
        v = self.view(switch_1=True)
        self.assertEqual([s["code"] for s in v["switches"]], ["switch_1"])
        self.assertTrue(v["switches"][0]["value"])

    def test_unbekannte_codes_werden_nicht_erfunden(self) -> None:
        spec = {"functions": [], "status": [{"code": "irgendwas_neu", "values": "{}"}]}
        v = build_view(spec, [{"code": "irgendwas_neu", "value": 5}])
        self.assertEqual(v["metrics"][0]["label"], "Irgendwas neu")


class DatenpunktZuordnung(unittest.TestCase):
    """Nummern den Klarnamen zuordnen — ohne offizielle Tabelle."""

    def test_eindeutige_werte_werden_zugeordnet(self) -> None:
        from app.local import dp_map_aus_vergleich
        benannt = {"cur_voltage": 2310, "cur_power": 47, "switch_1": True}
        nummeriert = {"20": 2310, "19": 47, "1": True}
        m = dp_map_aus_vergleich(benannt, nummeriert)
        self.assertEqual(m["20"], "cur_voltage")
        self.assertEqual(m["19"], "cur_power")
        self.assertEqual(m["1"], "switch_1")

    def test_mehrdeutige_werte_werden_ausgelassen(self) -> None:
        from app.local import dp_map_aus_vergleich
        # Im Leerlauf sind mehrere Werte 0 — daraus darf nichts geraten werden.
        benannt = {"cur_current": 0, "cur_power": 0, "cur_voltage": 2295}
        nummeriert = {"18": 0, "19": 0, "20": 2295}
        m = dp_map_aus_vergleich(benannt, nummeriert)
        self.assertEqual(m, {"20": "cur_voltage"})

    def test_wahr_und_eins_werden_nicht_verwechselt(self) -> None:
        from app.local import dp_map_aus_vergleich
        # In Python gilt True == 1; ohne Typvergleich käme hier Unsinn heraus.
        benannt = {"switch_1": True, "countdown_1": 1}
        nummeriert = {"1": True, "9": 1}
        m = dp_map_aus_vergleich(benannt, nummeriert)
        self.assertEqual(m.get("1"), "switch_1")
        self.assertEqual(m.get("9"), "countdown_1")

    def test_ohne_gemeinsame_werte_leer(self) -> None:
        from app.local import dp_map_aus_vergleich
        self.assertEqual(dp_map_aus_vergleich({"a": 1}, {"5": 99}), {})


class LokaleUebersetzung(unittest.TestCase):
    """Die lokale Antwort muss aussehen wie die aus der Cloud."""

    def bau(self, dp_map):
        from app.local import LocalDevice
        d = LocalDevice.__new__(LocalDevice)   # ohne tinytuya-Prüfung
        d.dp_map = dp_map
        return d

    def test_nummern_werden_zu_namen(self) -> None:
        d = self.bau({"1": "switch_1", "20": "cur_voltage"})
        roh = {"1": True, "20": 2310}
        umgesetzt = {e["code"]: e["value"] for e in [
            {"code": d.dp_map.get(str(k), f"dp_{k}"), "value": v} for k, v in roh.items()]}
        self.assertEqual(umgesetzt, {"switch_1": True, "cur_voltage": 2310})

    def test_unbekannte_nummer_bleibt_sichtbar(self) -> None:
        d = self.bau({"1": "switch_1"})
        code = d.dp_map.get("77", "dp_77")
        self.assertEqual(code, "dp_77")

    def test_code_zu_dp(self) -> None:
        d = self.bau({"1": "switch_1", "20": "cur_voltage"})
        self.assertEqual(d._code_zu_dp("cur_voltage"), 20)
        with self.assertRaises(Exception):
            d._code_zu_dp("gibt_es_nicht")


class BlockGedaechtnis(unittest.TestCase):
    """Ein laufender Block darf nicht neu verhandelt werden."""

    def setUp(self) -> None:
        self.cfg = automation.settings(
            {"enabled": True, "mode": "cheapest_block", "cheapest_hours": 3}
        )
        # Heute: guenstig 12-14. Morgen: durchweg noch etwas guenstiger.
        self.heute = price_day([0.40]*12 + [0.20, 0.20, 0.20] + [0.40]*9)
        self.morgen = price_day([0.18]*24, day="2026-08-17")

    def test_ohne_gedaechtnis_wandert_die_wahl_nach_morgen(self) -> None:
        """Das Verhalten, das die Trockenübung aufgedeckt hat."""
        spaet = dt.datetime(2026, 8, 16, 15, 30, tzinfo=dt.timezone.utc)
        block = automation.cheapest_block(self.heute + self.morgen, 3, spaet, 24)
        self.assertTrue(all(s.startswith("2026-08-17") for s in block),
                        "ohne Gedaechtnis wird erwartungsgemaess morgen gewaehlt")

    def test_laufender_block_bleibt_bestehen(self) -> None:
        mittag = dt.datetime(2026, 8, 16, 12, 30, tzinfo=dt.timezone.utc)
        gemerkt = {e["startsAt"] for e in self.heute[12:15]}
        prices_ = {"current": dict(self.heute[13]), "today": self.heute, "tomorrow": self.morgen}
        e = automation.decide(prices_, self.cfg, mittag, block=gemerkt)
        self.assertTrue(e.desired, "der laufende Block muss weiterlaufen")
        self.assertEqual(e.block, gemerkt, "und unveraendert bleiben")

    def test_abgelaufener_block_wird_neu_geplant(self) -> None:
        abends = dt.datetime(2026, 8, 16, 20, 30, tzinfo=dt.timezone.utc)
        alt = {e["startsAt"] for e in self.heute[12:15]}   # 12-15 Uhr, vorbei
        prices_ = {"current": dict(self.heute[20]), "today": self.heute, "tomorrow": self.morgen}
        e = automation.decide(prices_, self.cfg, abends, block=alt)
        self.assertNotEqual(e.block, alt, "ein abgelaufener Block muss ersetzt werden")

    def test_block_gilt_noch(self) -> None:
        jetzt = dt.datetime(2026, 8, 16, 13, 0, tzinfo=dt.timezone.utc)
        laufend = {e["startsAt"] for e in self.heute[12:15]}
        vorbei = {e["startsAt"] for e in self.heute[5:8]}
        self.assertTrue(automation.block_gilt_noch(laufend, jetzt))
        self.assertFalse(automation.block_gilt_noch(vorbei, jetzt))
        self.assertFalse(automation.block_gilt_noch(set(), jetzt))

    def test_zeitfenster_begrenzt_die_suche(self) -> None:
        frueh = dt.datetime(2026, 8, 16, 0, 30, tzinfo=dt.timezone.utc)
        eng = automation.cheapest_block(self.heute + self.morgen, 3, frueh, 12)
        self.assertTrue(all(s.startswith("2026-08-16") for s in eng),
                        "mit engem Fenster darf morgen nicht gewaehlt werden")


class Waehrung(unittest.TestCase):
    """Tibber ist in mehreren Laendern taetig — die Waehrung darf nicht raten."""

    def test_boersenquellen_sind_euro(self) -> None:
        # aWATTar und Energy-Charts liefern ausschliesslich EUR/MWh.
        from app import prices as pr
        self.assertTrue(pr.is_spot("awattar_de"))
        self.assertTrue(pr.is_spot("energy_charts"))

    def test_einheit_haengt_an_der_waehrung(self) -> None:
        """Die Anzeige darf Kronen nicht als Cent ausgeben."""
        for waehrung, erwartet in (("EUR", "ct/kWh"), ("SEK", "SEK-Cent/kWh"),
                                   ("NOK", "NOK-Cent/kWh")):
            einheit = "ct/kWh" if waehrung == "EUR" else f"{waehrung}-Cent/kWh"
            self.assertEqual(einheit, erwartet)


class TestAufrufbarkeit(unittest.TestCase):
    """Jeder Zugangsweg braucht seine Fabrikfunktion — sonst faellt die Kette.

    Anlass: `sharing_device()` wurde an vier Stellen aufgerufen, war aber nicht
    definiert. Aufgefallen ist das nicht, weil der lokale Weg meist zuerst
    greift und die uebrigen Aufrufe nur im Fehlerfall erreicht werden — genau
    dann, wenn man den Rueckfall am dringendsten braucht.
    """

    def test_fabrikfunktionen_der_drei_wege_existieren(self) -> None:
        from app import main

        for name in ("client", "local_device", "sharing_device",
                     "reset_client", "reset_local", "reset_sharing"):
            self.assertTrue(callable(getattr(main, name, None)),
                            f"{name}() fehlt in main.py")

    def test_qr_zugang_ohne_einrichtung_liefert_none(self) -> None:
        """Ohne Anmeldung darf der Weg leer bleiben, aber nicht werfen."""
        from app import main

        self.assertIsNone(main.sharing_device())

    def test_alle_aufgerufenen_namen_sind_definiert(self) -> None:
        """Statische Gegenprobe ueber das ganze Paket."""
        import ast
        import builtins

        paket = Path(__file__).resolve().parents[1] / "app"
        for datei in sorted(paket.glob("*.py")):
            baum = ast.parse(datei.read_text(encoding="utf-8"))
            definiert = set(dir(builtins))
            for knoten in ast.walk(baum):
                if isinstance(knoten, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    definiert.add(knoten.name)
                elif isinstance(knoten, ast.Name) and isinstance(knoten.ctx, ast.Store):
                    definiert.add(knoten.id)
                elif isinstance(knoten, (ast.Import, ast.ImportFrom)):
                    definiert.update((a.asname or a.name).split(".")[0] for a in knoten.names)
                elif isinstance(knoten, ast.arg):
                    definiert.add(knoten.arg)
                elif isinstance(knoten, ast.ExceptHandler) and knoten.name:
                    definiert.add(knoten.name)
                elif isinstance(knoten, ast.comprehension) and isinstance(knoten.target, ast.Name):
                    definiert.add(knoten.target.id)

            aufgerufen = {
                k.func.id for k in ast.walk(baum)
                if isinstance(k, ast.Call) and isinstance(k.func, ast.Name)
            }
            fehlend = sorted(aufgerufen - definiert)
            self.assertEqual(fehlend, [], f"{datei.name}: nicht definiert: {fehlend}")


if __name__ == "__main__":
    unittest.main()
