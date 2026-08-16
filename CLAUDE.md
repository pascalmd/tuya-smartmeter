# tuya-smartmeter

Weboberfläche + Dauerdienst, der einen Tuya-Stromzähler (Zielgerät: **KTEM06**)
über die **Tuya Cloud API** ausliest und schaltet, und ihn anhand der
**Tibber-Preise** automatisch ein- und ausschaltet.

Gebaut als weitergebbare App: kein Wert ist fest verdrahtet, die komplette
Einrichtung passiert im Browser. Zielplattform ist eine **TrueNAS-Installation
über die UI** — siehe [INSTALL-TRUENAS.md](INSTALL-TRUENAS.md).

## Wo es läuft

Testinstanz auf **docker01**: `http://192.168.178.17:8099`
Compose `/DATA/Docker/tuya-smartmeter/`, Daten `/DATA/AppData/tuya-smartmeter/`.
Image dorthin per `docker save | docker load` von docker02 gebracht (ghcr-Paket
ist noch privat), Container von Watchtower ausgenommen.

Die TrueNAS-Installation über die UI ist der eigentliche Auslieferungsweg und in
[INSTALL-TRUENAS.md](INSTALL-TRUENAS.md) beschrieben.

## Aufbau

| Datei | Inhalt |
|-------|--------|
| `app/tuya.py` | Tuya-OpenAPI-Client (HMAC-SHA256-Signatur, Token-Refresh), Aufbereitung von Status + Spezifikation, Base64-Phasendekoder |
| `app/tibber.py` | Tibber-GraphQL (Homes, Preise heute/morgen), Hilfsfunktionen für günstigste Stunden |
| `app/automation.py` | Schaltregeln, zustandslose Entscheidung, Vorschauberechnung |
| `app/store.py` | SQLite-Historie (`/config/history.db`), Messwerte + Ereignisse, 90 Tage Aufbewahrung |
| `app/config.py` | Konfiguration in `/config/config.json`, scrypt-Passworthash, API-Token |
| `app/main.py` | FastAPI: Seiten, JSON-API, Hintergrund-Poller |
| `tests/test_logic.py` | 18 Tests der Schaltlogik und Datenaufbereitung, laufen ohne Cloud |

## Betrieb

Der Poller läuft dauerhaft im Container, unabhängig von geöffneten Browser-Tabs:

1. Gerätestatus von Tuya holen (Intervall einstellbar, Standard 10 s)
2. Messwerte in die SQLite-Historie schreiben
3. Tibber-Preise auffrischen (alle 10 min — Preise sind stundenscharf)
4. Regel auswerten, bei Abweichung schalten

Bei Cloud-Fehlern greift ein exponentielles Backoff bis 5 Minuten; die Oberfläche
zeigt den letzten bekannten Stand weiter und meldet das Alter der Daten.

## Schaltregeln

| Modus | Verhalten |
|-------|-----------|
| `threshold` | EIN, solange der aktuelle Preis ≤ Schwelle (ct/kWh) |
| `cheapest` | EIN in den n günstigsten Stunden des Tages |
| `level` | EIN bei den ausgewählten Tibber-Preisstufen |

Schutzmechanismen: Sicherheitsnetz (Zwangs-EIN nach x Stunden aus),
Mindest-Aus-Zeit gegen Flattern, Automatikpause nach Handbedienung.

## JSON-API

Zugriff per Session-Cookie oder Kopfzeile `X-API-Token` (Token unter Einstellungen).

| Endpunkt | Zweck |
|----------|-------|
| `GET /api/state` | kompletter Zustand: Messwerte, Schalter, Preis, Automatik |
| `POST /api/switch` | `{"code":"switch","value":true}` — schaltet und pausiert die Automatik |
| `GET /api/prices` | Tibber-Preise roh |
| `GET /api/series?code=cur_power&hours=24` | Verlauf aus der Historie |
| `GET /api/events` | Ereignisprotokoll (Schaltvorgänge, Fehler) |
| `GET /healthz` | Dienststatus, ohne Anmeldung — für Zabbix/Kuma |

## Tests

```bash
python -m unittest discover -s tests -v
```

Läuft ohne Zugangsdaten und ohne Netz.

## Image bereitstellen

TrueNAS installiert nur fertige Images — ein Build aus dem Quelltext geht dort
nicht. Also einmal bauen und in eine Registry schieben:

```bash
docker build -t ghcr.io/<konto>/tuya-smartmeter:latest .
docker push ghcr.io/<konto>/tuya-smartmeter:latest
```

Alternative ohne GitHub: die Forgejo-Registry auf zima (`git.7x10.net`).
Damit Fremde (z. B. der Kumpel mit dem zweiten KTEM06) das Image ziehen können,
muss das Paket öffentlich sein.

## Bekannte Grenzen

- **Tuya-Testzeitraum:** Neue Cloud-Projekte sind befristet. Läuft die Frist ab,
  scheitert jeder Abruf mit einem Berechtigungsfehler. Verlängerung im Tuya-Projekt
  unter *Service → Extend Trial*.
- **Cloud-Abhängigkeit:** Ohne Internet kein Schalten. Ein lokaler Weg über den
  Local Key (tinytuya) wäre unabhängig, braucht aber einen zusätzlichen
  Auslesevorgang pro Gerät.

  **Bewusst nicht lokal gebaut** (Entscheidung 2026-08-16): Bei Weitergabe ist
  das Netz des Empfängers unbekannt. Hängt der Zähler z. B. im FritzBox-Gastnetz
  und der Server im Heimnetz, kommt eine lokale Verbindung prinzipiell nicht
  zustande — Gastnetze sperren den Zugriff aufs Heimnetz und lassen sich dafür
  nicht öffnen. Der Cloud-Weg funktioniert dagegen über Segmentgrenzen hinweg,
  weil beide Seiten nur ausgehend verbinden. Lokal bleibt eine Option für
  Installationen, bei denen Gerät und Server nachweislich im selben Netz liegen.
- **Tuya-Abfragelimit:** Das kostenlose Kontingent ist endlich; unter 5 s
  Intervall lässt die App deshalb nicht zu.
- **Schaltkanal:** Die App bietet an, was das Gerät in seiner Spezifikation als
  Boolean meldet — bei den meisten Zählern `switch`.

## Datenschutz / Sicherheit

- Zugangsdaten liegen ausschließlich in `/config/config.json` (Rechte 600), nie im Image.
- Das Oberflächenpasswort wird als scrypt-Hash gespeichert.
- Kein Telemetrie- oder Fremdaufruf außer Tuya und Tibber.
- Die App gehört nicht ungeschützt ins Internet — sie schaltet Strom. VPN nutzen.
