"""Die Oberflaeche in allen Zustandskombinationen durchspielen.

Warum es diese Datei gibt: Die Logik war jedes Mal in Ordnung, die Anzeige
nicht. Ein Haekchen, das den gespeicherten Stand nie uebernahm; eine Meldung,
die den falschen Grund nannte; eine Tabelle, die nach dem Schalten veraltete.
Solche Fehler findet kein Test der Schaltregeln -- sie entstehen erst aus der
Kombination von Zustaenden, und sie zeigen sich nur im gerenderten HTML.

Deshalb hier: jede Seite in jeder sinnvollen Lage abrufen und pruefen, dass
nichts Widerspruechliches, Unaufgeloestes oder Kaputtes darin steht.

Aufruf:  python tests/test_ui.py
"""

from __future__ import annotations

import itertools
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="tuya-ui-"))

from fastapi.testclient import TestClient  # noqa: E402

from app import geraete, main  # noqa: E402
from app.config import config  # noqa: E402

# Alle Seiten, die ein angemeldeter Nutzer erreichen kann.
SEITEN = ["/", "/preise", "/verlauf", "/automation", "/settings",
          "/devices", "/zugang", "/preisquelle", "/tibber"]

# Spuren nicht aufgeloester Vorlagen oder fehlender Werte. Wenn eines davon
# in der Ausgabe steht, hat die Seite etwas anzuzeigen versucht, das es nicht
# gab -- fuer den Nutzer sieht das aus wie ein Programmfehler.
VERRAETER = ["{{", "{%", "Undefined", "None None", ">None<", "undefined",
             "Internal Server Error", "Traceback"]


class FakeGeraet:
    """Ein Geraet, das lokal antwortet -- ohne Netz und ohne Cloud."""

    def __init__(self, gid: str, werte: dict, erreichbar: bool = True) -> None:
        self.device_id, self.ip, self.local_key = gid, "10.0.0.1", "k"
        self.werte, self.erreichbar = werte, erreichbar

    async def status(self):
        if not self.erreichbar:
            raise RuntimeError("nicht erreichbar")
        return [{"code": c, "value": v} for c, v in self.werte.items()]

    async def send_commands(self, befehle):
        for b in befehle:
            self.werte[b["code"]] = b["value"]


class OberflaechenDurchgang(unittest.TestCase):
    """Jede Seite in jeder Lage -- ohne dass jemand klicken muss."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.geraete = {
            "zaehler": FakeGeraet("zaehler", {"switch": True, "cur_power": 4560,
                                              "cur_voltage": 2310, "total_ele": 18}),
            "dose": FakeGeraet("dose", {"switch_1": False, "cur_power": 0}),
        }
        main.local_device = lambda gid="": cls.geraete.get(
            (geraete.aufloesen(gid) or {}).get("id")
        )
        main.sharing_device = lambda gid="": None
        main.logged_in = lambda request: True

        config.set_admin_password("pruefung123")
        config.set("setup_done", True)
        config.set("price", {"source": "awattar_de"})
        config.save()
        # Als Kontextmanager, damit die App wie im Betrieb hochfaehrt --
        # sonst laeuft kein Poller und die Seiten sehen Zustaende, die es
        # in Wirklichkeit nie gibt.
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def lage(self, *, regel_an: bool, folgt: bool, ruht: bool, zweites: bool = True):
        """Eine Zustandskombination herstellen."""
        config.set("automation", {"enabled": regel_an, "mode": "threshold",
                                  "threshold_ct": 25.0})
        eintraege = [{"id": "zaehler", "name": "Zaehler",
                      "local": {"enabled": True, "ip": "10.0.0.1", "key": "k"},
                      "automatik_aktiv": True, "aktiv": True}]
        if zweites:
            eintraege.append({"id": "dose", "name": "Steckdose",
                              "local": {"enabled": True, "ip": "10.0.0.2", "key": "k"},
                              "switch_code": "switch_1",
                              "automatik_aktiv": folgt, "aktiv": not ruht})
        geraete.speichern(eintraege)
        main._states.clear()
        self.pollen()

    def pollen(self) -> None:
        """Einmal abfragen, wie es der Poller im Betrieb tut.

        Ohne das haetten die Zustaende leere Messwerte -- und die Pruefung
        liefe gegen eine Lage, die es im Betrieb nie gibt.
        """
        import asyncio

        async def durchlauf():
            for st in main.alle_zustaende(nur_aktive=True):
                try:
                    await main.poll_device(st)
                except Exception:
                    pass

        asyncio.run(durchlauf())

    def seite(self, pfad: str) -> str:
        antwort = self.client.get(pfad)
        self.assertEqual(antwort.status_code, 200, f"{pfad} antwortete {antwort.status_code}")
        text = antwort.text
        for spur in VERRAETER:
            self.assertNotIn(spur, text, f"{pfad} enthaelt »{spur}«")
        return text

    @staticmethod
    def klartext(html: str) -> str:
        ohne = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
        return " ".join(re.sub(r"<[^>]+>", " ", ohne).split())

    # ------------------------------------------------------------------ Faelle

    def test_jede_seite_in_jeder_lage(self) -> None:
        """Alle Kombinationen aus Regel, Teilnahme und Ruhezustand."""
        for regel_an, folgt, ruht in itertools.product([True, False], repeat=3):
            with self.subTest(regel=regel_an, folgt=folgt, ruht=ruht):
                self.lage(regel_an=regel_an, folgt=folgt, ruht=ruht)
                for pfad in SEITEN:
                    self.seite(pfad)

    def test_mit_nur_einem_geraet(self) -> None:
        """Wer ein Geraet hat, darf nichts von der Liste mitbekommen."""
        self.lage(regel_an=True, folgt=True, ruht=False, zweites=False)
        text = self.klartext(self.seite("/"))
        self.assertNotIn("Alle Geräte", text)
        self.assertNotIn("Gerät:", text)      # kein Umschalter

    def test_ausgenommenes_geraet_meldet_nicht_regel_sei_aus(self) -> None:
        """Der Grund muss zum Fall passen, sonst fuehrt der Link ins Leere."""
        self.lage(regel_an=True, folgt=False, ruht=False)
        js = self.seite("/?device=dose")
        self.assertIn("nur von Hand geschaltet", js)
        self.assertIn("Der Automatik folgen lassen", js)

    def test_ausgeschaltete_regel_wird_als_solche_benannt(self) -> None:
        self.lage(regel_an=False, folgt=True, ruht=False)
        self.assertIn("insgesamt ausgeschaltet", self.seite("/"))
        self.assertIn("insgesamt ausgeschaltet", self.seite("/devices"))

    def test_haekchen_spiegeln_den_gespeicherten_stand(self) -> None:
        for folgt, ruht in itertools.product([True, False], repeat=2):
            with self.subTest(folgt=folgt, ruht=ruht):
                self.lage(regel_an=True, folgt=folgt, ruht=ruht)
                html = self.seite("/devices")
                zeile = html.split('value="dose"', 1)[1]
                block_regel = zeile.split('name="mitmachen"', 1)[1].split(">")[0] \
                    if 'name="mitmachen"' in zeile else ""
                self.assertEqual("checked" in block_regel, folgt)

    def test_alle_verlinkten_seiten_sind_erreichbar(self) -> None:
        """Kein Link darf auf 404 oder 500 zeigen."""
        self.lage(regel_an=True, folgt=True, ruht=False)
        ziele: set[str] = set()
        for pfad in SEITEN:
            for treffer in re.findall(r'href="(/[^"#?]*)', self.seite(pfad)):
                ziele.add(treffer)
        for ziel in sorted(ziele):
            if ziel.endswith(".png") or ziel == "/logout":
                continue
            with self.subTest(ziel=ziel):
                self.assertLess(self.client.get(ziel).status_code, 400, ziel)

    def test_formulare_ueberleben_leere_eingaben(self) -> None:
        """Ein abgeschicktes Formular ohne Werte darf nichts umwerfen."""
        self.lage(regel_an=True, folgt=True, ruht=False)
        for pfad, daten in [
            ("/devices/automatik", {"device_id": "dose"}),
            ("/devices/aktiv", {"device_id": "dose"}),
            ("/devices/aufzeichnen", {"device_id": "dose"}),
            ("/devices/umbenennen", {"device_id": "dose", "name": ""}),
            ("/devices/entfernen", {"device_id": "gibtsnicht"}),
            ("/automation/resume", {}),
        ]:
            with self.subTest(pfad=pfad):
                antwort = self.client.post(pfad, data=daten, follow_redirects=False)
                self.assertLess(antwort.status_code, 400, f"{pfad}: {antwort.status_code}")

    def test_offline_geraet_bricht_nichts(self) -> None:
        """Ein Geraet, das nicht antwortet, ist der haeufigste Realfall."""
        self.geraete["dose"].erreichbar = False
        try:
            self.lage(regel_an=True, folgt=True, ruht=False)
            for pfad in SEITEN:
                self.seite(pfad)
            text = self.klartext(self.seite("/?device=dose"))
            self.assertNotIn("456", text)   # keine Messwerte eines toten Geraets
        finally:
            self.geraete["dose"].erreichbar = True

    def test_frische_installation(self) -> None:
        """Ohne Geraet und ohne Preise darf keine Seite kaputtgehen."""
        config.set("automation", {})
        geraete.speichern([])
        main._states.clear()
        main.preise.data, main.preise.ts, main.preise.error = {}, 0.0, ""
        for pfad in SEITEN:
            antwort = self.client.get(pfad, follow_redirects=False)
            self.assertLess(antwort.status_code, 400, f"{pfad}: {antwort.status_code}")
            if antwort.status_code == 200:
                for spur in VERRAETER:
                    self.assertNotIn(spur, antwort.text, f"{pfad} enthaelt »{spur}«")

    def test_geraet_ohne_schaltbaren_ausgang(self) -> None:
        """Ein reiner Sensor meldet keinen Schalter -- die Daten muessen das hergeben.

        Was die Uebersicht daraus macht, prueft test_browser.py; hier geht es
        um die Quelle, aus der sie es nimmt.
        """
        self.geraete["dose"].werte = {"cur_power": 12, "temp_current": 21}
        try:
            self.lage(regel_an=True, folgt=True, ruht=False)
            daten = self.client.get("/api/state?device=dose").json()
            self.assertEqual(daten["switches"], [])
            self.assertTrue(daten["metrics"])
        finally:
            self.geraete["dose"].werte = {"switch_1": False, "cur_power": 0}

    def test_keine_widersprueche_zwischen_meldung_und_lage(self) -> None:
        """Kein sichtbarer Text darf die Automatik fuer aus erklaeren, wenn sie laeuft.

        Geprueft wird der Klartext ohne Skriptblöcke: Dort stehen alle
        Meldungsvarianten nebeneinander, auch die gerade nicht zutreffenden.
        """
        self.lage(regel_an=True, folgt=True, ruht=False)
        for pfad in ("/automation", "/devices"):
            text = self.klartext(self.seite(pfad))
            self.assertNotIn("insgesamt ausgeschaltet", text, pfad)
        # Die Uebersicht entscheidet erst im Browser; hier die Datenlage pruefen
        auto = self.client.get("/api/state").json()["automation"]
        self.assertTrue(auto["regel_aktiv"])
        self.assertTrue(auto["mitmachen"])

    def test_sprachregeln(self) -> None:
        """Programmversionen heissen Version, nie Fassung."""
        self.lage(regel_an=True, folgt=True, ruht=False)
        for pfad in SEITEN:
            text = self.klartext(self.seite(pfad))
            self.assertNotIn("Fassung", text, pfad)

    def test_umbenennen_wirkt_ueberall(self) -> None:
        self.lage(regel_an=True, folgt=True, ruht=False)
        self.client.post("/devices/umbenennen",
                         data={"device_id": "dose", "name": "Waschmaschine"},
                         follow_redirects=False)
        self.assertIn("Waschmaschine", self.seite("/devices"))
        self.assertIn("Waschmaschine", self.seite("/?device=dose"))
        self.assertIn("Waschmaschine", self.seite("/verlauf?device=dose"))

    def test_ruhendes_geraet_wird_nicht_abgefragt(self) -> None:
        self.lage(regel_an=True, folgt=True, ruht=True)
        gesundheit = self.client.get("/healthz").json()
        namen = [g["name"] for g in gesundheit.get("devices", [])]
        self.assertNotIn("Steckdose", namen)
        self.assertEqual(gesundheit.get("degraded_devices"), [])

    def test_schalten_wirkt_auf_beide_ansichten(self) -> None:
        """Geraeteliste und Schalterkarte kommen aus derselben Quelle."""
        self.lage(regel_an=False, folgt=True, ruht=False)
        self.client.get("/")                       # Poller einmal laufen lassen
        vorher = self.client.get("/api/devices").json()["devices"]
        stand = {g["device_id"]: [s["value"] for s in g["switches"]] for g in vorher}

        self.client.post("/api/switch", json={"code": "switch_1", "value": True,
                                              "device": "dose"})
        nachher = self.client.get("/api/devices").json()["devices"]
        neu = {g["device_id"]: [s["value"] for s in g["switches"]] for g in nachher}
        self.assertNotEqual(stand["dose"], neu["dose"])
        self.assertEqual(stand["zaehler"], neu["zaehler"])   # unberuehrt


class Diagnosebericht(unittest.TestCase):
    """Der Bericht ist zum Verschicken gedacht -- er darf nichts verraten.

    Geprueft wird mit erfundenen, aber wiedererkennbaren Geheimnissen: Taucht
    eines davon irgendwo im Bericht auf, ist der Test rot. Das ist die einzige
    Pruefung, die auch bei kuenftigen Feldern noch greift.
    """

    GEHEIMNISSE = {
        "client_secret": "GEHEIM_SECRET_32_ZEICHEN_LANG_XY",
        "local_key": "GEHEIM_LOCALKEY_1234",
        "tibber_token": "GEHEIM_TIBBER_TOKEN_ABCDEF",
        "api_token": "GEHEIM_API_TOKEN_ABCDEF123",
        "session": "GEHEIM_SESSION_SECRET_XYZ",
        "passwort": "GeheimesPasswort123",
        "sharing_token": "GEHEIM_SHARING_ACCESS_TOKEN",
    }

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from app import geraete, main
        from app.config import config

        self.main = main
        config.set_admin_password(self.GEHEIMNISSE["passwort"])
        config.set("setup_done", True)
        config.set_tuya("CLIENTIDVOLLSTAENDIG", self.GEHEIMNISSE["client_secret"], "eu")
        config.set("api_token", self.GEHEIMNISSE["api_token"])
        config.set("session_secret", self.GEHEIMNISSE["session"])
        config.set("tibber", {"token": self.GEHEIMNISSE["tibber_token"],
                              "home_id": "haus-1", "home_label": "Zuhause"})
        config.set("sharing", {"enabled": True, "user_code": "ABCDEF",
                               "token": {"access_token": self.GEHEIMNISSE["sharing_token"],
                                         "refresh_token": "GEHEIM_REFRESH", "uid": "u1"}})
        geraete.speichern([{
            "id": "bf1234567890abcdef01", "name": "Zaehler",
            "local": {"enabled": True, "ip": "192.168.1.50",
                      "key": self.GEHEIMNISSE["local_key"],
                      "dp_map": {"1": "switch_1", "20": "cur_voltage"}},
        }])
        config.save()
        main.logged_in = lambda request: True
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_kein_geheimnis_im_bericht(self) -> None:
        bericht = self.main.diagnose_daten()
        import json

        text = json.dumps(bericht, ensure_ascii=False)
        for name, geheimnis in self.GEHEIMNISSE.items():
            self.assertNotIn(geheimnis, text, f"{name} steht im Bericht")
        # Auch der Passwort-Hash selbst nicht
        self.assertNotIn(config_hash := self.main.config.get("admin_hash"), text)
        self.assertTrue(config_hash)

    def test_kein_geheimnis_auf_der_seite(self) -> None:
        for pfad in ("/diagnose", "/diagnose.json"):
            text = self.client.get(pfad).text
            for name, geheimnis in self.GEHEIMNISSE.items():
                self.assertNotIn(geheimnis, text, f"{name} steht auf {pfad}")

    def test_kontokennungen_nur_angedeutet(self) -> None:
        """Access ID und Benutzercode sind nicht geheim, aber nichts fuer Dritte.

        Anlass: Im echten Bericht stand die Access ID vollstaendig -- gefunden
        beim Abgleich des Berichts gegen die echte Konfiguration.
        """
        import json

        bericht = self.main.diagnose_daten()
        text = json.dumps(bericht, ensure_ascii=False)
        self.assertNotIn("CLIENTIDVOLLSTAENDIG", text)
        self.assertEqual(bericht["zugang"]["tuya_projekt"]["access_id"],
                         "CLIE… (20 Zeichen)")

    def test_befund_statt_inhalt(self) -> None:
        """Was zaehlt, ist ob und wie lang -- daran erkennt man ein abgeschnittenes Secret."""
        bericht = self.main.diagnose_daten()
        self.assertEqual(bericht["zugang"]["tuya_projekt"]["access_secret"],
                         "gesetzt (32 Zeichen)")
        self.assertEqual(bericht["geraete"][0]["lokal"]["schluessel"],
                         "gesetzt (20 Zeichen)")
        self.assertEqual(bericht["preise"]["tibber_token"], "gesetzt (26 Zeichen)")

    def test_nuetzliches_ist_drin(self) -> None:
        bericht = self.main.diagnose_daten()
        self.assertIn("version", bericht["app"])
        self.assertEqual(bericht["geraete"][0]["lokal"]["adresse"], "192.168.1.50")
        self.assertEqual(bericht["geraete"][0]["name"], "Zaehler")
        self.assertIn("python", bericht["umgebung"])
        self.assertIn("letzte_ereignisse", bericht)
        self.assertIn("messpunkte", bericht["aufzeichnung"])

    def test_keine_messwerte(self) -> None:
        """Nur Kennzahlen -- sonst verraet der Bericht die Anwesenheit.

        Geprueft mit einem unverwechselbaren Messwert: Taucht er auf, ist eine
        Messreihe in den Bericht gerutscht.
        """
        import json

        from app import store

        store.record([{"code": "cur_power", "value": 4711.5}], [],
                     device="bf1234567890abcdef01")
        text = json.dumps(self.main.diagnose_daten(), ensure_ascii=False)
        self.assertNotIn("4711.5", text)
        self.assertNotIn("4711,5", text)
        # Die Kennzahlen daraus sind erwuenscht
        self.assertIn("aufgezeichnete_groessen", text)
        self.assertIn("cur_power", text)

    def test_name_und_kennung_stehen_vollstaendig_drin(self) -> None:
        """Ohne beides laesst sich ein Geraet nicht wiederfinden."""
        bericht = self.main.diagnose_daten()
        self.assertEqual(bericht["geraete"][0]["kennung"], "bf1234567890abcdef01")
        self.assertEqual(bericht["geraete"][0]["name"], "Zaehler")

    def test_ohne_anmeldung_kein_bericht(self) -> None:
        from app import main

        main.logged_in = lambda request: False
        try:
            self.assertEqual(self.client.get("/diagnose", follow_redirects=False).status_code, 303)
            self.assertEqual(self.client.get("/diagnose.json").status_code, 401)
        finally:
            main.logged_in = lambda request: True


if __name__ == "__main__":
    unittest.main(verbosity=1)
