"""Die Anleitungen gegen den Code halten.

Dokumentation veraltet still: Der Code aendert sich, die Anleitung bleibt
stehen, und jemand sucht nach einem Feld, das es nicht mehr gibt. Beim Kumpel
ist genau das passiert -- Klickpfade, die nicht mehr stimmten.

Geprueft wird deshalb maschinell, was sich maschinell pruefen laesst: Kommen
alle Regeln, Preisquellen und Menuepunkte vor? Steht noch etwas drin, das
abgeschafft wurde?

Aufruf:  python tests/test_doku.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WURZEL))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="tuya-doku-"))

from app import automation, prices  # noqa: E402

ANLEITUNGEN = ["README.md", "INSTALL-TRUENAS.md"]

# Was es einmal gab und heute nicht mehr. Steht eines davon in einer Anleitung,
# schickt sie jemanden auf die Suche nach etwas, das es nicht gibt.
ABGESCHAFFT = {
    "Schaltkanal des Zählers": "Das Feld sitzt nicht mehr in der Automatik",
    "Gerät wählen": "Die Seite heisst jetzt Geräte und verwaltet mehrere",
    "Schritt 1 von": "Der Schrittzaehler wurde entfernt",
    "Fassung": "Programmversionen heissen Version",
}


class Anleitungen(unittest.TestCase):
    def texte(self) -> dict[str, str]:
        return {name: (WURZEL / name).read_text(encoding="utf-8") for name in ANLEITUNGEN}

    def test_alle_regeln_kommen_vor(self) -> None:
        for name, text in self.texte().items():
            for regel in automation.MODE_LABELS.values():
                with self.subTest(datei=name, regel=regel):
                    self.assertIn(regel, text, f"{name} kennt die Regel »{regel}« nicht")

    def test_alle_preisquellen_kommen_vor(self) -> None:
        for name, text in self.texte().items():
            for schluessel, quelle in prices.SOURCES.items():
                erstes_wort = quelle["label"].split()[0]
                with self.subTest(datei=name, quelle=schluessel):
                    self.assertTrue(schluessel in text or erstes_wort in text,
                                    f"{name} erwaehnt {schluessel} nicht")

    def test_nichts_abgeschafftes_mehr(self) -> None:
        for name, text in self.texte().items():
            for begriff, warum in ABGESCHAFFT.items():
                with self.subTest(datei=name, begriff=begriff):
                    self.assertNotIn(begriff, text, f"{name}: {warum}")

    def test_menuepfade_zeigen_auf_vorhandenes(self) -> None:
        """Ein Pfad wie »Einstellungen → Geräte« muss in der Oberflaeche stehen."""
        oberflaeche = " ".join(
            p.read_text(encoding="utf-8") for p in (WURZEL / "app" / "templates").glob("*.html")
        )
        muster = r"(?:Einstellungen|Automatik|Geräte|Preise|Verlauf|Übersicht)\s*→\s*[A-Za-zäöüÄÖÜ ]{3,30}"
        for name, text in self.texte().items():
            for pfad in set(re.findall(muster, text)):
                ziel = pfad.split("→")[-1].strip()
                with self.subTest(datei=name, pfad=pfad):
                    self.assertIn(ziel, oberflaeche,
                                  f"{name}: »{ziel}« gibt es in der Oberflaeche nicht")

    def test_beschriebene_bedienelemente_existieren(self) -> None:
        """Was die Anleitung zum Anklicken auffordert, muss es geben."""
        geraeteseite = (WURZEL / "app" / "templates" / "devices.html").read_text(encoding="utf-8")
        einstellungen = (WURZEL / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
        for begriff, vorlage in [
            ("folgt der Regel", geraeteseite),
            ("abfragen", geraeteseite),
            ("Messwerte aufzeichnen", geraeteseite),
            ("Umbenennen", geraeteseite),
            ("Übernehmen", geraeteseite),
            ("Diagnosebericht", einstellungen),
        ]:
            for name, text in self.texte().items():
                if begriff in text:
                    with self.subTest(datei=name, element=begriff):
                        self.assertIn(begriff, vorlage,
                                      f"{name} nennt »{begriff}«, die Oberflaeche kennt es nicht")

    def test_erwaehnte_endpunkte_gibt_es(self) -> None:
        code = (WURZEL / "app" / "main.py").read_text(encoding="utf-8")
        for name, text in self.texte().items():
            for pfad in set(re.findall(r"`(/(?:api|diagnose|healthz)[a-z0-9/._-]*)`", text)):
                sauber = pfad.split("?")[0]
                with self.subTest(datei=name, pfad=sauber):
                    self.assertIn(f'"{sauber}"', code, f"{name}: {sauber} gibt es nicht")

    def test_versionsangaben_sind_keine_festen_zahlen(self) -> None:
        """Eine Anleitung, die eine Version nennt, veraltet mit dem naechsten Bau."""
        for name, text in self.texte().items():
            treffer = re.findall(r"Version\s+1\.\d+\.\d+", text)
            with self.subTest(datei=name):
                self.assertEqual(treffer, [], f"{name} nennt feste Versionen: {treffer}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
