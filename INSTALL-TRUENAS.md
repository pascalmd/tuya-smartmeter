# Installation auf TrueNAS — Schritt für Schritt

Diese Anleitung ist für Leute gedacht, die TrueNAS bedienen können, aber keine
Docker-Kommandos tippen wollen. Alles läuft über die TrueNAS-Oberfläche und
danach über die Weboberfläche der App selbst.

Rechne mit etwa 30 Minuten, davon 20 für die Tuya-Anmeldung.

---

## Das Wichtigste zuerst

> ### Du legst gleich ein **zweites Tuya-Konto** an. Das ist normal und richtig.
>
> `iot.tuya.com` (Entwicklerplattform) und die **Smart-Life-App** sind zwei
> getrennte Systeme mit getrennten Benutzerdatenbanken. Dein App-Login
> funktioniert auf `iot.tuya.com` **nicht** — nicht weil etwas kaputt ist,
> sondern weil es das Konto dort schlicht nicht gibt. Du registrierst dich dort
> also neu.
>
> ### Und jetzt der Satz, auf den es ankommt:
>
> **Du musst deine Geräte deswegen NICHT neu einrichten.**
>
> Kein Zurücksetzen, kein neues Anlernen, kein Umverkabeln, nichts am
> Sicherungskasten. Das Entwicklerkonto ist nur ein *Schlüssel*, damit Programme
> mit der Tuya-Cloud reden dürfen. Die Verbindung zu deinen vorhandenen Geräten
> entsteht später durch **einen QR-Code**, den du mit deiner gewohnten
> Smart-Life-App scannst (Teil 1, Schritt 5). Danach sieht das Entwicklerkonto
> deine Geräte — sie bleiben dabei genau da, wo sie sind.
>
> Deine App, deine Geräte, dein WLAN bleiben unangetastet.

> **Zu den Klickpfaden:** Tuya und TrueNAS ändern die Bezeichnungen ihrer Menüs
> regelmäßig. Wenn ein Punkt hier anders heißt als auf deinem Bildschirm, ist das
> kein Fehler in der Anleitung, sondern eine neuere Version. Deshalb steht
> unten meist dabei, *wonach* du suchst, nicht nur wo es letztes Jahr stand.

---

## Was du brauchst

| Was | Wozu |
|-----|------|
| TrueNAS SCALE 24.10 oder neuer | ältere Versionen haben das neue Apps-System (Docker) noch nicht. Getestet mit 25.10 „Goldeye" |
| Tuya-/Smart-Life-Konto | das Konto, in dem der Zähler schon eingerichtet ist |
| Preisquelle | Börsenpreise gehen ohne Konto (aWATTar, Energy-Charts). Für den echten Endkundenpreis: Tibber-Konto mit aktivem Vertrag |
| ~15 Minuten Geduld bei Tuya | die Entwickler-Anmeldung ist etwas sperrig |

---

## Teil 1 — Tuya-Zugang einrichten

Tuya lässt fremde Programme **nicht** mit E-Mail und Passwort der App arbeiten.
Stattdessen brauchst du ein kostenloses Entwickler-Projekt. Das klingt schlimmer,
als es ist — du klickst dich einmal durch und kopierst dann zwei Zeichenketten.

1. Auf **iot.tuya.com** ein Konto anlegen (kostenlos, siehe Kasten oben — das ist
   nicht dein App-Konto).

2. **Ein Cloud-Projekt anlegen.**
   Im Menü links den Bereich **Cloud** öffnen, dort den Punkt zum Anlegen eines
   Projekts — je nach Version *Development*, *Projects* oder direkt ein Knopf
   **Create Cloud Project**. Beim Anlegen:
   - Name: frei wählbar, z. B. `Smartmeter`
   - Industry / Development Method: Standardwerte lassen
   - **Data Center: Central Europe** ← für Deutschland wichtig

3. **Die nötigen Dienste (APIs) müssen aktiv sein.**
   Beim Anlegen wird meist danach gefragt; sonst findest du es im Projekt unter
   **Service API**, *API-Verwaltung* oder ähnlich. Diese drei müssen dabei sein:
   - `IoT Core`
   - `Authorization`
   - `Smart Home Scene Linkage`

4. **Die beiden Zeichenketten abholen.**
   In der Projektübersicht (*Overview*, *Project Overview*) gibt es einen Block
   namens **Authorization Key** oder **Access Key**. Darin stehen:
   - **Access ID** (auch *Client ID*) — 20 Zeichen
   - **Access Secret** (auch *Client Secret*) — 32 Zeichen

   Das Secret ist standardmäßig als `••••••` verborgen. **Erst auf das
   Augen-Symbol klicken**, dann kopieren — sonst erwischt du nur die Punkte.

5. **Der entscheidende Schritt — hier werden die beiden Konten verbunden:**

   Im Projekt auf den Reiter **Devices**. Darin gibt es einen Punkt, der je nach
   Version **Link App Account**, **Link Tuya App Account** oder **Link My App**
   heißt — such nach „Link". Dahinter **Add App Account** bzw. **Add Apps**.

   Es erscheint ein **QR-Code**. Den scannst du mit deiner gewohnten
   **Smart-Life-App**: *Ich → oben rechts das Scan-Symbol*.

   Damit erlaubst du deinem Entwicklerprojekt, deine vorhandenen Geräte zu sehen.
   **An den Geräten selbst ändert sich dabei nichts** — sie werden nicht neu
   angelernt, nicht zurückgesetzt, nicht verschoben.

   Erst nach diesem Schritt sieht das Programm deinen Zähler.

> **Wenn die Menüs anders heißen:** Tuya baut seine Oberfläche regelmäßig um, und
> zwar schneller, als Anleitungen nachgezogen werden. Halte dich an die
> Schlüsselwörter (*Cloud*, *Create*, *Authorization Key*, *Link*), nicht an den
> genauen Wortlaut. Tuyas eigene, stets aktuelle Beschreibung des
> Verknüpfungsschritts steht hier:
> <https://developer.tuya.com/en/docs/iot/link-devices> — auf Englisch, dafür
> immer passend zur aktuellen Oberfläche, meist mit Bildern.

> **Hinweis zum Testzeitraum:** Tuya befristet neue Projekte. Läuft die Frist ab,
> hört der Zugriff auf zu funktionieren. Im Projekt unter **Service → Extend Trial**
> lässt sie sich kostenlos verlängern. Ein Kalendereintrag lohnt sich.

---

## Teil 2 — Preisquelle

Hier gibt es zwei Wege.

**Ohne Konto (Voreinstellung):** aWATTar oder Energy-Charts liefern die
Börsenpreise für Deutschland beziehungsweise Österreich — ohne Anmeldung, ohne
Schlüssel. Du musst für diesen Teil gar nichts vorbereiten. In der App stellst
du später nur ein, wie hoch dein Aufschlag ist (Netzentgelte, Umlagen, Steuern),
damit aus dem Börsenpreis dein realistischer Endpreis wird.

**Mit Tibber-Konto:** Dann bekommst du deinen echten Endkundenpreis, ohne rechnen
zu müssen.

1. **developer.tibber.com** öffnen, mit dem normalen Tibber-Konto anmelden.
2. Unter **Access Token** den persönlichen Token kopieren.

Der Token darf nur lesen — schalten kann damit niemand.

> Wichtig zu wissen: Preisgesteuertes Schalten spart nur dann Geld, wenn dein
> Stromtarif die Stundenpreise tatsächlich weitergibt. Bei einem Festpreistarif
> zahlst du rund um die Uhr dasselbe — dann ist die Automatik reine Spielerei.

---

## Teil 3 — App in TrueNAS installieren

In TrueNAS: **Apps**, dort die Möglichkeit, eine eigene Anwendung anzulegen. Sie
heißt je nach Version **Custom App**, **Add Custom App** oder versteckt sich hinter
**Discover Apps** — in neueren Fassungen (25.10 „Goldeye" und später) sitzt der
Knopf oben rechts. Such nach „Custom".

Es gibt dort zwei Wege. Der YAML-Weg ist weniger Klickerei, der Formular-Weg
ist übersichtlicher. Beide führen zum selben Ergebnis — nimm einen.

### Weg A — per YAML (empfohlen, weniger Fehlerquellen)

Im Custom-App-Fenster auf **Install via YAML** umschalten und das hier einfügen:

```yaml
services:
  tuya-smartmeter:
    image: ghcr.io/pascalmd/tuya-smartmeter:latest
    restart: unless-stopped
    ports:
      - "8099:8099"
    volumes:
      - /mnt/DEIN-POOL/apps/tuya-smartmeter:/config
    environment:
      TZ: Europe/Berlin
```

Eine Zeile musst du anpassen:

- `/mnt/DEIN-POOL/apps/tuya-smartmeter` — ein Ordner auf deinem Pool.
  Lege ihn vorher unter **Datasets** an, sonst legt TrueNAS ihn als Verzeichnis an.

Die Image-Adresse stimmt so — das Image liegt fertig auf GitHub und muss nicht
selbst gebaut werden.

### Weg B — über das Formular

| Feld | Wert |
|------|------|
| Application Name | `tuya-smartmeter` |
| Image Repository | `ghcr.io/pascalmd/tuya-smartmeter` |
| Image Tag | `latest` |
| Port Forwarding → Container Port | `8099` |
| Port Forwarding → Node Port | `8099` (oder ein freier Port deiner Wahl) |
| Storage → Mount Path | `/config` |
| Storage → Host Path | dein Ordner, z. B. `/mnt/tank/apps/tuya-smartmeter` |
| Environment → Name / Value | `TZ` / `Europe/Berlin` |
| Restart Policy | `Unless Stopped` |

Alles andere kann auf Standard bleiben. Dann **Install**.

> **Wichtig:** Der Ordner unter `/config` muss dauerhaft sein. Dort liegen deine
> Zugangsdaten und die Messwert-Historie. Ohne ihn ist nach jedem Neustart alles weg.

---

## Teil 4 — Ersteinrichtung im Browser

Die App meldet sich nach etwa einer halben Minute unter:

```
http://<IP-deines-TrueNAS>:8099
```

Dann führt sie dich durch vier Schritte:

1. **Passwort festlegen** — damit meldest du dich künftig hier an.
   Dazu Access ID und Access Secret von oben eintragen, Rechenzentrum
   *Central Europe*. Die App prüft die Daten sofort.
2. **Gerät wählen** — die Liste kommt aus deinem Tuya-Konto. Den Zähler anklicken.
3. **Preisquelle wählen** — Börsenpreise ohne Konto, oder Tibber mit Token.
4. **Automatik einstellen** — siehe unten.

Danach läuft die App dauerhaft weiter, auch wenn du den Browser schließt.

---

## Teil 5 — Automatik einstellen

Drei Regeln stehen zur Wahl:

**Preisschwelle** — am einfachsten zu verstehen.
Du gibst an: einschalten bis z. B. 25 ct/kWh. Liegt der aktuelle Preis darunter,
ist der Zähler ein, sonst aus.

**Günstigste Stunden** — gut für planbaren Verbrauch.
Du gibst an: die 6 günstigsten Stunden des Tages. Die App sucht sie aus den
Tagespreisen heraus und schaltet nur dann ein.

**Preisstufen** — teilt die Stunden in fünf Stufen ein, gemessen am
Tagesdurchschnitt: sehr günstig, günstig, normal, teuer, sehr teuer. Du hakst an,
bei welchen Stufen eingeschaltet werden soll.

Dazu drei Schutzeinstellungen:

- **Sicherheitsnetz** — nie länger als x Stunden aus. Verhindert, dass ein teurer
  Tag das Gerät dauerhaft abschaltet. Wenn etwas dranhängt, das nicht beliebig
  lange aus sein darf, stell das ein.
- **Mindest-Aus-Zeit** — verhindert schnelles Hin- und Herschalten an der Preisgrenze.
- **Pause nach Handbedienung** — schaltest du selbst auf der Übersicht, hält sich
  die Automatik so lange zurück. Sonst würde sie sofort zurückschalten.

Unten auf der Automatik-Seite siehst du eine **Vorschau der nächsten Stunden** —
dort steht für jede Stunde Preis und ob der Schalter an wäre. Damit kannst du
eine Einstellung prüfen, bevor sie scharf geschaltet wird.

---

## Wenn etwas nicht klappt

| Symptom | Ursache und Abhilfe |
|---------|---------------------|
| Mein App-Login geht auf iot.tuya.com nicht | Richtig so — das ist ein anderes System. Dort neu registrieren, siehe „Das Wichtigste zuerst". Deine Geräte musst du deswegen **nicht** neu einrichten |
| Menüpunkt heißt anders als hier beschrieben | Tuya und TrueNAS benennen ihre Menüs häufig um. Nach dem Schlüsselwort suchen („Link", „Custom"), nicht nach dem genauen Wortlaut |
| „clientId is invalid" | Access ID oder Secret vertippt, oder falsches Rechenzentrum |
| Geräteliste ist leer | „Link App Account" fehlt (Teil 1, Schritt 5), oder App-Konto liegt in einer anderen Region |
| „No permissions" / Code 1106 | Im Tuya-Projekt fehlt eine der drei APIs, oder der Testzeitraum ist abgelaufen |
| Tibber meldet 401 | Token abgelaufen — auf developer.tibber.com neu erzeugen |
| Preise fehlen bei aWATTar | Die Preise für morgen kommen erst nachmittags gegen 14 Uhr |
| Preise fehlen, Rest läuft | Vertrag ohne stündliche Preise, oder falsches Zuhause ausgewählt |
| Seite nicht erreichbar | Port in TrueNAS geprüft? Manche Ports sind belegt — dann Node Port ändern |
| Nach Neustart alles weg | Der `/config`-Ordner war nicht dauerhaft eingebunden |

Detailfehler stehen im Container-Log: **Apps → tuya-smartmeter → Logs**.

---

## Statt NAS: Raspberry Pi

Wenn kein NAS zur Verfügung steht oder es nicht rund um die Uhr läuft, ist ein
Raspberry Pi die naheliegende Alternative — er braucht 3 bis 5 Watt und kostet
im Jahr etwa so viel Strom wie zwei Tassen Kaffee.

**Wenn der Pi noch leer ist**, ist es sogar einfacher als mit einem bestehenden
System. Der *Raspberry Pi Imager* ist eine normale Windows-Anwendung und nimmt
einem die ganze Vorbereitung ab:

1. Imager installieren, SD-Karte einlegen
2. Betriebssystem: **Raspberry Pi OS Lite (64-bit)** — ohne Desktop, 64-bit ist Pflicht
3. Vor dem Schreiben auf das **Zahnrad** klicken und ausfüllen: Hostname,
   SSH aktivieren mit Benutzername und Passwort, WLAN-Zugang, Zeitzone
4. Karte schreiben, in den Pi stecken, Strom anschließen

Weil WLAN und SSH schon auf der Karte stehen, braucht der Pi **weder Bildschirm
noch Tastatur**. Nach ein bis zwei Minuten ist er im Netz erreichbar.

**Dann per SSH verbinden und eine Zeile eingeben:**

```bash
curl -sSL https://raw.githubusercontent.com/pascalmd/tuya-smartmeter/main/install.sh | sudo bash
```

Das Skript prüft das System, installiert Docker falls nötig, richtet den Dienst
ein und nennt am Ende die Adresse für den Browser. Was auf dem Pi schon läuft,
bleibt unangetastet; ist Port 8099 belegt, weicht es selbstständig aus.

Ab da geht es weiter mit **Teil 4** — Ersteinrichtung im Browser.

> **Zur SD-Karte:** Die App zeichnet Messwerte auf und schreibt dafür regelmäßig
> auf die Karte. Ab Werk geschieht das einmal pro Minute, was auch billige Karten
> jahrelang aushalten. Wer ganz sichergehen will, stellt den Wert unter
> *Einstellungen* höher oder auf 0 — geschaltet wird davon unabhängig weiter im
> vollen Takt.

---

## Zugriff von unterwegs

Die App ist absichtlich nur im eigenen Netz erreichbar. Wenn du von außen
drauf willst, ist ein VPN (WireGuard/Tailscale) der sichere Weg.
Die App direkt ins Internet zu stellen, ist nicht zu empfehlen — sie schaltet Strom.
Wenn es doch sein muss: nur hinter einem Reverse Proxy mit HTTPS.
