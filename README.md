# tuya-smartmeter

Schaltet Tuya-Geräte automatisch nach dem aktuellen Strompreis —
ein wenn Strom günstig ist, aus wenn er teuer ist.

Entwickelt für den **KTEM06**, funktioniert aber mit jedem Tuya-Gerät, das einen
schaltbaren Ausgang meldet — auch mit mehreren gleichzeitig, etwa einem Zähler
und einer Schaltsteckdose. Jedes Gerät bekommt seine eigene Regel; eines kann
nach dem Preis schalten, während ein anderes nur von Hand bedient wird. Läuft als Dienst im Container, bedient wird sie über
eine Weboberfläche.

**Keine Konfigurationsdateien:** Die komplette Einrichtung — Zugangsdaten, Gerät,
Preisquelle, Schaltregeln — passiert im Browser. Für die Installation auf einem
NAS über dessen Oberfläche siehe **[INSTALL-TRUENAS.md](INSTALL-TRUENAS.md)**.

## Installation

**Auf einem Linux-Rechner oder Raspberry Pi** — ein Befehl, der alles einrichtet
(auch Docker, falls es fehlt):

```bash
curl -sSL https://raw.githubusercontent.com/pascalmd/tuya-smartmeter/main/install.sh | sudo bash
```

**Auf einem NAS** über dessen eigene Oberfläche:
siehe [INSTALL-TRUENAS.md](INSTALL-TRUENAS.md).

**Von Hand**, wenn Docker schon läuft:

```bash
docker run -d --name tuya-smartmeter -p 8099:8099 \
  -v ./config:/config -e TZ=Europe/Berlin --restart unless-stopped \
  ghcr.io/pascalmd/tuya-smartmeter:latest
```

Danach `http://<server>:8099` öffnen — der Rest läuft im Browser.

Das Image gibt es für **x86_64 und ARM64**; auf einem Raspberry Pi wird
automatisch die passende Version geladen. Ein 64-bit-Betriebssystem ist nötig
(Raspberry Pi OS Lite 64-bit), ein Pi 3 oder neuer.

## Was man braucht

- Ein **Tuya-Entwicklerprojekt** (kostenlos, `iot.tuya.com`) für Access ID und
  Access Secret.

  > **Wichtig:** `iot.tuya.com` ist ein **eigenes Konto**, getrennt von der
  > Smart-Life-App — der App-Login funktioniert dort nicht, man registriert sich
  > neu. **Die vorhandenen Geräte müssen deswegen aber nicht neu eingerichtet
  > werden.** Kein Zurücksetzen, kein Neuanlernen. Die Verbindung entsteht durch
  > einen QR-Code, den man mit der gewohnten App scannt. Wer das nicht weiß,
  > vermutet leicht, er müsse seine Installation umbauen.
- Eine **Preisquelle**. aWATTar und Energy-Charts liefern Börsenpreise ohne
  Anmeldung; wer einen dynamischen Tarif bei Tibber hat, bekommt dort den echten
  Endkundenpreis.

> Preisgesteuertes Schalten spart nur Geld, wenn der eigene Tarif die
> Stundenpreise tatsächlich weitergibt. Bei einem Festpreistarif ändert sich am
> Rechnungsbetrag nichts.

## Aufbau

| Datei | Inhalt |
|-------|--------|
| `app/local.py` | Direkter Zugriff im eigenen Netz (tinytuya): kein Kontingent, keine Frist, ~30 ms. Übersetzt Datenpunkt-Nummern in Klarnamen |
| `app/sharing.py` | Anmeldung per QR-Code ohne Entwicklerkonto (Tuyas `tuya-device-sharing-sdk`) |
| `app/tuya.py` | Tuya-OpenAPI-Client (HMAC-SHA256-Signatur, Token-Refresh), Aufbereitung von Status + Spezifikation, Base64-Phasendekoder |
| `app/prices.py` | Preisquellen: aWATTar DE/AT, Energy-Charts, Tibber. Einheitliches Format, Aggregation auf Stunden, Aufschlag/MwSt, Preisstufen |
| `app/tibber.py` | Tibber-GraphQL (Homes, Preise heute/morgen), Hilfsfunktionen für günstigste Stunden |
| `app/automation.py` | Schaltregeln, zustandslose Entscheidung, Vorschauberechnung |
| `app/store.py` | SQLite-Historie (`/config/history.db`), Messwerte + Ereignisse, 90 Tage Aufbewahrung |
| `app/config.py` | Konfiguration in `/config/config.json`, scrypt-Passworthash, API-Token |
| `app/main.py` | FastAPI: Seiten, JSON-API, Hintergrund-Poller |
| `install.sh` | Installer für Linux/Raspberry Pi: prüft System, installiert Docker falls nötig, sucht freien Port, richtet den Dienst ein |
| `tests/test_logic.py` | 30 Tests: Schaltregeln, Preisquellen, Fremdschaltungserkennung, Tuya-Aufbereitung — laufen ohne Cloud |

## Die drei Gerätezugänge

Die App versucht sie in dieser Reihenfolge und nimmt den obersten, der
funktioniert:

| | Weg | Braucht | Befristet | Geschwindigkeit |
|---|-----|---------|-----------|-----------------|
| 1 | **lokal** | Gerät erreichbar + Local Key | nein | ~30 ms |
| 2 | **QR-Anmeldung** | Smart-Life-Konto | nein | ~1 s |
| 3 | Entwicklerprojekt | Access ID + Secret | **ja, 1 Monat** | ~1 s |

Stufe 2 beschafft, was Stufe 1 braucht: Die QR-Anmeldung liefert den Local Key
mit. Damit lässt sich der lokale Weg einrichten, **ohne je ein Entwicklerprojekt
anzulegen** — der einzige Weg mit Ablaufdatum wird überflüssig.

Der lokale Weg liefert außerdem mehr: Beim DDS238-2 kommt der Zählerstand
(`total_ele`) nur dort an, im Cloud-Status fehlt er.

> **Zur QR-Anmeldung, offen gesagt:** Sie nutzt eine bei Tuya registrierte
> Anwendungskennung; voreingestellt ist die von Home Assistant, die offen in
> dessen Quelltext steht. Gegenüber Tuya meldet sich die App damit als Home
> Assistant an. Deshalb steht dieser Weg an zweiter Stelle und nicht an erster —
> lokal braucht es so etwas nicht. Wer eine eigene Kennung hat, trägt sie ein.

## Betrieb

Der Poller läuft dauerhaft im Container, unabhängig von geöffneten Browser-Tabs:

1. Gerätestatus von Tuya holen (Intervall einstellbar, Standard 10 s)
2. Messwerte in die SQLite-Historie schreiben — **entkoppelt vom Abfragetakt**
   (Standard 60 s, 0 schaltet ab). Auf einem Raspberry Pi liegt die Datenbank
   auf einer SD-Karte; alle 10 s zu schreiben verschleißt sie unnötig. Geschaltet
   wird trotzdem im vollen Takt
3. Strompreise auffrischen (alle 10 min — Preise sind stundenscharf)
4. Regel auswerten, bei Abweichung schalten

Bei Cloud-Fehlern greift ein exponentielles Backoff bis 5 Minuten; die Oberfläche
zeigt den letzten bekannten Stand weiter und meldet das Alter der Daten.

## Preisquellen

| Quelle | Konto | Liefert |
|--------|-------|---------|
| `awattar_de` / `awattar_at` | nein | EPEX-Spot, stündlich |
| `energy_charts` | nein | Day-Ahead DE-LU (viertelstündlich, wird auf Stunden gemittelt) |
| `tibber` | ja | Endkundenpreis inklusive aller Abgaben |

Börsenquellen liefern den reinen Beschaffungspreis. Aufschlag (ct/kWh netto) und
MwSt sind einstellbar, damit die Preisschwelle mit einem realistischen Endpreis
rechnet. Preisstufen werden für diese Quellen selbst gebildet — relativ zum
Tagesmittel, weil nur Tibber sie mitliefert.

## Schaltregeln

| Modus | Verhalten |
|-------|-----------|
| `threshold` | EIN, solange der aktuelle Preis ≤ Schwelle (ct/kWh) |
| `cheapest` | EIN in den n günstigsten Stunden des Tages (verstreut) |
| `cheapest_block` | EIN im günstigsten zusammenhängenden Block von n Stunden — genau ein Ein- und Ausschalten, darf über Mitternacht gehen |
| `level` | EIN bei den ausgewählten Preisstufen (sehr günstig … sehr teuer) |

Schutzmechanismen (alle abschaltbar): Sicherheitsnetz (Zwangs-EIN nach x Stunden
aus), Mindestlaufzeit, Mindest-Aus-Zeit gegen Flattern, Automatikpause nach
Handbedienung — auch bei Schaltvorgängen aus der Hersteller-App oder am Gerät.

**Für Verbraucher, die am Stück laufen sollen:** `cheapest_block` zusammen mit
einer Mindestlaufzeit. Die verstreute Auswahl der billigsten Stunden ist
geringfügig günstiger, erzeugt aber bis zu n Unterbrechungen — und nicht jeder
Verbraucher nimmt den Betrieb selbsttätig wieder auf. Jedes Schalten unter Last
kostet zudem Relais-Lebensdauer.

## JSON-API

Zugriff per Session-Cookie oder Kopfzeile `X-API-Token` (Token unter Einstellungen).

| Endpunkt | Zweck |
|----------|-------|
| `GET /api/state` | kompletter Zustand: Messwerte, Schalter, Preis, Automatik |
| `GET /api/state?device=<id>` | dasselbe für ein bestimmtes Gerät |
| `GET /api/devices` | alle Geräte mit vollem Zustand |
| `POST /api/switch` | `{"code":"switch","value":true}` — schaltet und pausiert die Automatik |
| `GET /api/prices` | Strompreise roh |
| `GET /api/series?code=cur_power&hours=24` | Verlauf aus der Historie |
| `GET /api/events` | Ereignisprotokoll (Schaltvorgänge, Fehler) |

Ohne `device` gilt immer das erste Gerät — bestehende Anbindungen lesen also
unverändert weiter. Mehrere Geräte ansprechen: `?device=<Kennung>` an der URL,
beim Schalten `{"code":"switch_1","value":true,"device":"<Kennung>"}`.
| `GET /healthz` | Dienststatus, ohne Anmeldung — für Zabbix/Kuma |

## Tests

```bash
python -m unittest discover -s tests -v
```

Läuft ohne Zugangsdaten und ohne Netz.

## Veröffentlichen

```bash
./release.sh 1.4.0
```

Prüft, dass alles committet und gepusht ist, lässt die Tests laufen, baut für
amd64 und arm64, lädt nach GHCR hoch und setzt den Git-Tag.

Der Umweg über ein Skript hat einen Grund: Während der Entwicklung wird das Image
gern nur lokal gebaut, um es schnell auszuprobieren — die Registry bleibt dabei
stehen. Genau das ist einmal passiert, mit acht Commits Rückstand, darunter ein
Fehler, der die Automatik lahmlegte. Wer neu installiert hätte, hätte genau diese
Version bekommen.

Das Paket steht auf **public**, lässt sich also ohne Konto ziehen. Die
Sichtbarkeit ist nur über die Web-Oberfläche umstellbar, dafür gibt es keinen
REST-Endpunkt. Für den Push braucht es ein *classic* Personal Access Token mit
`write:packages` — fine-grained Tokens werden von GHCR abgelehnt.

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
- **Tuya-Abfragelimit — der eigentliche Engpass:** Die kostenlose *Trial Edition*
  erlaubt 26.000 API-Aufrufe im Monat, also einen alle 100 Sekunden. Das
  Abfrageintervall ist deshalb auf **180 s** voreingestellt (rund 60 % des
  Kontingents). Bei 10 s wäre es nach drei Tagen aufgebraucht. Die Einstellungsseite
  rechnet den Verbrauch beim Tippen mit. Für die Automatik ist das folgenlos —
  Preise wechseln stündlich; nur eine Schaltung am Gerät wird bis zu ein
  Intervall später bemerkt.

  Die übrigen Grenzen der Trial Edition sind unkritisch: 50 Geräte, davon 10
  steuerbare, ein Rechenzentrum.
- **Schaltkanal:** Die App bietet an, was das Gerät in seiner Spezifikation als
  Boolean meldet — bei den meisten Zählern `switch`.

## Datenschutz / Sicherheit

- Zugangsdaten liegen ausschließlich in `/config/config.json` (Rechte 600), nie im Image.
- Das Oberflächenpasswort wird als scrypt-Hash gespeichert.
- Kein Telemetrie- oder Fremdaufruf außer Tuya und der gewählten Preisquelle.
- Die App gehört nicht ungeschützt ins Internet — sie schaltet Strom. VPN nutzen.
