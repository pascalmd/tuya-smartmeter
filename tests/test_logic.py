"""Tests der Schaltlogik und der Tuya-Datenaufbereitung — laufen ohne Cloud-Zugang.

Aufruf:  python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import datetime as dt
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


class Fremdschaltung(unittest.TestCase):
    """Erkennung von Schaltvorgaengen, die nicht aus dieser App kamen."""

    def setUp(self) -> None:
        import os
        os.environ.setdefault("CONFIG_DIR", "/tmp/tuya-test-config")
        from app import main
        self.main = main
        self.state = main.state
        self.state.last_seen = None
        self.state.expected_state = None
        self.state.last_action = ""
        self.auto = automation.settings(
            {"enabled": True, "switch_code": "switch", "override_minutes": 60}
        )
        main.config.set("override_until", 0)

    def test_erste_messung_loest_nichts_aus(self) -> None:
        self.main.note_switch_state(True, self.auto)
        self.assertEqual(self.main.config.get("override_until"), 0)

    def test_unveraenderter_zustand_loest_nichts_aus(self) -> None:
        self.main.note_switch_state(True, self.auto)
        self.main.note_switch_state(True, self.auto)
        self.assertEqual(self.main.config.get("override_until"), 0)

    def test_eigene_schaltung_gilt_nicht_als_fremd(self) -> None:
        self.main.note_switch_state(True, self.auto)
        self.state.expected_state = False          # wir schalten selbst aus
        self.main.note_switch_state(False, self.auto)
        self.assertEqual(self.main.config.get("override_until"), 0)
        self.assertIsNone(self.state.expected_state)

    def test_fremde_schaltung_pausiert_die_automatik(self) -> None:
        self.main.note_switch_state(False, self.auto)
        self.main.note_switch_state(True, self.auto)   # jemand schaltet in der Tuya-App ein
        self.assertGreater(self.main.config.get("override_until"), time.time())
        self.assertIn("von Hand", self.state.last_action)

    def test_ohne_pausenzeit_keine_pause(self) -> None:
        auto = automation.settings({"enabled": True, "override_minutes": 0})
        self.main.note_switch_state(False, auto)
        self.main.note_switch_state(True, auto)
        self.assertEqual(self.main.config.get("override_until"), 0)


if __name__ == "__main__":
    unittest.main()
