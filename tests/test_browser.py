"""Die Uebersicht so pruefen, wie sie im Browser aussieht.

Die Uebersicht baut ihren Inhalt per JavaScript aus /api/state und
/api/devices. Ein Test gegen das ausgelieferte HTML sieht davon nichts -- er
liest nur das Geruest und die Skriptzeilen. Genau in dieser Luecke sassen
mehrere Fehler: eine Tabelle, die nach dem Schalten veraltet blieb, ein
Hinweis mit dem falschen Grund, zwei Knoepfe mit widerspruechlicher Bedeutung.

Deshalb hier ein echter Browser. Ohne Playwright wird uebersprungen, damit
die Veroeffentlichung nicht daran haengt.

Aufruf:  python tests/test_browser.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="tuya-br-"))

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                   # pragma: no cover
    sync_playwright = None

import uvicorn  # noqa: E402

from app import geraete, main  # noqa: E402
from app.config import config  # noqa: E402

PORT = 8087


class FakeGeraet:
    def __init__(self, gid: str, werte: dict) -> None:
        self.device_id, self.ip, self.local_key = gid, "10.0.0.1", "k"
        self.werte = werte

    async def status(self):
        return [{"code": c, "value": v} for c, v in self.werte.items()]

    async def send_commands(self, befehle):
        for b in befehle:
            self.werte[b["code"]] = b["value"]


@unittest.skipIf(sync_playwright is None, "Playwright nicht installiert")
class Uebersicht(unittest.TestCase):
    """Was der Nutzer wirklich sieht."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.geraete = {
            "zaehler": FakeGeraet("zaehler", {"switch": True, "cur_power": 4560,
                                              "cur_voltage": 2310}),
            "dose": FakeGeraet("dose", {"switch_1": False, "cur_power": 0}),
        }
        main.local_device = lambda gid="": cls.geraete.get(
            (geraete.aufloesen(gid) or {}).get("id")
        )
        main.sharing_device = lambda gid="": None
        main.logged_in = lambda request: True

        config.set_admin_password("browsertest1")
        config.set("setup_done", True)
        config.set("price", {"source": "awattar_de"})
        # Kurzer Takt, damit eine Aenderung am Geraet in der Pruefzeit ankommt.
        # Im Betrieb sind es 180 s; hier wartet sonst jeder Fall zu lange.
        config.set("refresh_seconds", 2)
        # Die Regel bleibt im Grundzustand aus: Eine Automatik, die waehrend
        # der Pruefung selbst schaltet, macht jede Zustandsaussage wertlos.
        # Die Faelle, die sie brauchen, schalten sie gezielt ein.
        config.set("automation", {"enabled": False, "mode": "threshold",
                                  "threshold_ct": 25.0})
        config.save()
        geraete.speichern([
            {"id": "zaehler", "name": "Zaehler",
             "local": {"enabled": True, "ip": "10.0.0.1", "key": "k"}},
            {"id": "dose", "name": "Steckdose", "switch_code": "switch_1",
             "local": {"enabled": True, "ip": "10.0.0.2", "key": "k"}},
        ])

        cls.server = uvicorn.Server(uvicorn.Config(
            main.app, host="127.0.0.1", port=PORT, log_level="error"
        ))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        while not cls.server.started:
            import time
            time.sleep(0.1)

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def seite(self, pfad: str = "/"):
        page = self.browser.new_page()
        fehler: list[str] = []
        page.on("pageerror", lambda e: fehler.append(str(e)))
        page.on("console", lambda m: fehler.append(m.text) if m.type == "error" else None)
        page.goto(f"http://127.0.0.1:{PORT}{pfad}")
        page.wait_for_function(
            "document.getElementById('switches').textContent.indexOf('wird geladen') === -1",
            timeout=10_000,
        )
        self.assertEqual(fehler, [], f"JavaScript-Fehler auf {pfad}: {fehler}")
        return page

    # ------------------------------------------------------------------ Faelle

    def test_keine_platzhalter_bleiben_stehen(self) -> None:
        """Nichts darf dauerhaft auf 'wird geladen' stehen."""
        page = self.seite("/")
        try:
            page.wait_for_function(
                "!document.body.innerText.includes('wird geladen')", timeout=10_000
            )
        finally:
            text = page.inner_text("body")
            page.close()
        self.assertNotIn("wird geladen", text)

    @staticmethod
    def _zustand_und_aktion(text: str) -> tuple[str, str]:
        """Aus einem Ausschnitt Zustand (ein/aus) und Knopfbeschriftung lesen."""
        worte = text.replace("\t", " ").split()
        zustand = next((w for w in worte if w.lower() in ("ein", "aus")), "")
        aktion = next((w for w in worte if w.lower() in ("einschalten", "ausschalten")), "")
        return zustand.lower(), aktion.lower()

    def test_liste_und_schalterkarte_zeigen_dasselbe(self) -> None:
        """Der Widerspruch, der zum Umbau gefuehrt hat.

        Geprueft wird nicht ein bestimmter Zustand, sondern die
        Uebereinstimmung: Was die Zeile sagt, muss die Karte auch sagen.
        """
        for erwartet in (True, False):
            self.geraete["zaehler"].werte["switch"] = erwartet
            page = self.seite("/?device=zaehler")
            try:
                # Warten, bis der naechste Abruf den neuen Stand gebracht hat --
                # so folgt die Pruefung dem echten Weg ueber den Poller.
                page.wait_for_function(
                    "(soll) => document.getElementById('switches')"
                    ".innerText.toLowerCase().includes(soll)",
                    arg="ausschalten" if erwartet else "einschalten",
                    timeout=20_000,
                )
                zeile = [z for z in page.inner_text("#geraete-zeilen").split("\n")
                         if "Zaehler" in z][0]
                karte = page.inner_text("#switches")
                self.assertEqual(self._zustand_und_aktion(zeile),
                                 self._zustand_und_aktion(karte),
                                 f"Zeile: {zeile!r} · Karte: {karte!r}")
                zustand, aktion = self._zustand_und_aktion(karte)
                self.assertEqual(zustand, "ein" if erwartet else "aus")
                self.assertEqual(aktion, "ausschalten" if erwartet else "einschalten")
            finally:
                page.close()
        self.geraete["zaehler"].werte["switch"] = True

    def test_schalten_wirkt_sofort_in_beiden_ansichten(self) -> None:
        """Ohne Neuladen, ohne Geraetewechsel."""
        page = self.seite("/?device=dose")
        try:
            page.wait_for_selector("#switches button")
            self.assertIn("aus", page.inner_text("#switches").lower())

            page.click("#switches button")           # einschalten
            page.wait_for_function(
                "document.getElementById('switches').innerText.includes('ausschalten')",
                timeout=10_000,
            )
            karte = page.inner_text("#switches").lower()
            zeilen = page.inner_text("#geraete-zeilen").lower()
            self.assertIn("ausschalten", karte)
            # Die Zeile der Steckdose muss denselben Stand zeigen
            dosen_zeile = [z for z in zeilen.split("\\n") if "steckdose" in z]
            self.assertTrue(dosen_zeile, zeilen)
            self.assertIn("ausschalten", dosen_zeile[0])
        finally:
            self.geraete["dose"].werte["switch_1"] = False
            page.close()

    def test_hinweis_nennt_den_richtigen_grund(self) -> None:
        """Ausgenommenes Geraet vs. abgeschaltete Regel."""
        config.set("automation", {"enabled": True, "mode": "threshold",
                                  "threshold_ct": 25.0})
        geraete.aktualisieren("dose", automatik_aktiv=False)
        page = self.seite("/?device=dose")
        try:
            hinweis = page.inner_text("#automation-note")
            self.assertIn("nur von Hand", hinweis)
            self.assertNotIn("insgesamt ausgeschaltet", hinweis)
            self.assertTrue(page.query_selector("#automation-note button"),
                            "Es fehlt der Weg zurueck in die Automatik")
        finally:
            page.close()

        config.set("automation", {"enabled": False, "mode": "threshold"})
        page = self.seite("/?device=zaehler")
        try:
            self.assertIn("insgesamt ausgeschaltet", page.inner_text("#automation-note"))
        finally:
            config.set("automation", {"enabled": False, "mode": "threshold",
                                      "threshold_ct": 25.0})
            geraete.aktualisieren("dose", automatik_aktiv=True)
            page.close()

    def test_geraetewechsel_zeigt_das_gewaehlte_geraet(self) -> None:
        page = self.seite("/?device=dose")
        try:
            self.assertIn("switch_1", page.inner_text("#switches"))
            self.assertIn("Steckdose", page.inner_text("h1"))
        finally:
            page.close()

    def test_werte_bleiben_beim_aktualisieren_stehen(self) -> None:
        """Nach dem naechsten Abruf darf die Anzeige nicht aufs erste Geraet springen."""
        page = self.seite("/?device=dose")
        try:
            page.wait_for_timeout(5500)          # ein Aktualisierungstakt
            self.assertIn("switch_1", page.inner_text("#switches"))
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
