# Installation auf TrueNAS — Schritt für Schritt

Diese Anleitung ist für Leute gedacht, die TrueNAS bedienen können, aber keine
Docker-Kommandos tippen wollen. Alles läuft über die TrueNAS-Oberfläche und
danach über die Weboberfläche der App selbst.

Rechne mit etwa 30 Minuten, davon 20 für die Tuya-Anmeldung.

---

## Das Wichtigste zuerst

> ### Du brauchst **kein** Tuya-Entwicklerkonto.
>
> Ältere Anleitungen — auch frühere Versionen dieser hier — schicken dich auf
> `iot.tuya.com`, ein Projekt anlegen, APIs auswählen, Zeichenketten kopieren.
> Zwanzig Minuten, und das Ganze läuft nach einem Monat ab.
>
> **Das ist nicht mehr nötig.** Die App meldet sich per **QR-Code** mit dem
> Konto an, das du ohnehin in der Smart-Life-App hast. Zwei Minuten, keine
> Frist, nichts, was abläuft.
>
> Der alte Weg steht weiter zur Verfügung, falls du ihn brauchst — er ist in
> Teil 1 unter „Weg C" beschrieben. Nur dort gilt dann auch der Hinweis, dass
> `iot.tuya.com` ein eigenes Konto ist, getrennt von der App.

> **Und in keinem Fall gilt:** Du musst deine Geräte neu einrichten. Kein
> Zurücksetzen, kein Neuanlernen, nichts am Sicherungskasten. Deine App, deine
> Geräte, dein WLAN bleiben unangetastet.

---

## Was du brauchst

| Was | Wozu |
|-----|------|
| TrueNAS SCALE 24.10 oder neuer | ältere Versionen haben das neue Apps-System (Docker) noch nicht. Getestet mit 25.10 „Goldeye" |
| Tuya-/Smart-Life-Konto | das Konto, in dem der Zähler schon eingerichtet ist |
| Preisquelle | Börsenpreise gehen ohne Konto (aWATTar, Energy-Charts). Für den echten Endkundenpreis: Tibber-Konto mit aktivem Vertrag |
| ~15 Minuten Geduld bei Tuya | die Entwickler-Anmeldung ist etwas sperrig |

---

## Teil 1 — Gerätezugang

Die App kennt drei Wege zu deinem Zähler und nimmt automatisch den besten, der
funktioniert. Du richtest **einen** ein — am besten den ersten.

| | Weg | Aufwand | Läuft ab? | Tempo |
|---|-----|---------|-----------|-------|
| **A** | **QR-Anmeldung** | 2 Minuten | **nein** | ~1 s |
| **B** | Lokal im eigenen Netz | ergibt sich aus A | **nein** | ~30 ms |
| **C** | Entwicklerprojekt | 20 Minuten | **ja, 1 Monat** | ~1 s |

### Weg A — QR-Anmeldung (empfohlen)

Das machst du **nach** der Installation, in der Weboberfläche der App unter
**Zugang**. Hier nur, damit du weißt, was kommt:

1. In der **Smart-Life-App**: *Ich* → Zahnrad oben rechts → *Konto und
   Sicherheit*. Dort steht ein **Benutzercode** (*User Code*) — eine kurze
   Zeichenfolge, kein Passwort.
2. Den Code in der App-Oberfläche eintragen, **QR-Code erzeugen** drücken.
3. Den QR-Code mit der Smart-Life-App scannen (*oben rechts das Scan-Symbol*)
   und in der App bestätigen.
4. Zurück im Browser auf **Fertig, geprüft**.

Das war alles. Kein Konto anlegen, keine APIs auswählen, keine Zeichenketten
kopieren, kein Ablaufdatum.

### Weg B — lokal, ganz ohne Cloud

Der schnellste und unabhängigste Weg: Die App spricht direkt mit dem Gerät im
eigenen Netz. Kein Internet nötig, kein Abfragelimit, rund 30 Millisekunden
statt einer Sekunde — und bei diesem Zähler kommt sogar der **Zählerstand**
dazu, den die Cloud gar nicht herausgibt.

Dafür braucht es einen geräteeigenen Schlüssel, den **Weg A automatisch
mitliefert**. Du trägst in der Oberfläche nur noch die Adresse des Zählers im
Netz ein (steht im Router) und drückst auf Verbinden.

> **Wann das nicht geht:** Wenn Zähler und Server in getrennten Netzen hängen
> und die Trennung nicht durchlässig ist — etwa Zähler im FritzBox-Gastnetz,
> Server im Heimnetz. Dann bleibt es bei Weg A, der funktioniert immer.

### Weg C — Entwicklerprojekt (nur wenn nötig)

Der ursprüngliche Weg. Du brauchst ihn nur, wenn die QR-Anmeldung bei dir nicht
funktioniert.

> **Achtung, zwei Konten:** `iot.tuya.com` (inzwischen **platform.tuya.com**)
> und die Smart-Life-App sind getrennte Systeme. Dein App-Login funktioniert
> dort **nicht** — du registrierst dich neu. Deine Geräte musst du deswegen
> trotzdem nicht neu einrichten.

1. Auf **platform.tuya.com** ein Konto anlegen.
2. Links **Cloud** → **Cloud Project** → **Project Management** → rechts oben
   **Create Cloud Project**. Data Center: **Central Europe Data Center**.
3. Dienste: `IoT Core`, `Authorization Token Management`, `Smart Home Basic
   Service` (stehen später im Reiter **Service API**).
4. Reiter **Overview**, Block **Authorization Key**: **Access ID/Client ID**
   (20 Zeichen) und **Access Secret/Client Secret** (32 Zeichen). Das Secret ist
   als `••••••` verborgen — **erst auf das Augen-Symbol klicken**, dann kopieren.
5. Reiter **Devices** → **Link App Account** → **Add App Account** → QR-Code mit
   der Smart-Life-App scannen. Erst danach sieht das Projekt deine Geräte.

**Zum Ablaufdatum:** Ein neues Projekt hat genau einen Monat. Wo es steht, ist
gut versteckt: *Service API* → Zeile **IoT Core** → **View Details** → Tabelle
mit *Expiration Date*, darunter der Knopf **Extend Trial Period**. Einen Reiter
namens „Service" gibt es nicht.

Die Verlängerung ist ein **Antragsformular** (Dauer, Entwicklertyp, Gerätezahl,
Projektbeschreibung, Kontaktperson, Kontaktdaten) — kein Klick. Tuya antwortet
meist innerhalb eines Werktages, berichtet werden bis zu sechs Monate.

Trag das Ablaufdatum in der App unter *Einstellungen* ein, dann warnt sie dich
zehn Tage vorher.

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
**Discover Apps** — in neueren Versionen (25.10 „Goldeye" und später) sitzt der
Knopf oben rechts. Such nach „Custom".

Es gibt dort zwei Wege. Der YAML-Weg ist weniger Klickerei, der Formular-Weg
ist übersichtlicher. Beide führen zum selben Ergebnis — nimm einen.

> **Beide Wege wurden an einer laufenden TrueNAS 25.10.5 „Goldeye" komplett
> durchgeklickt** — bis zur laufenden App, jeweils mit anschließendem Rückbau.
> Die Bezeichnungen stammen aus der Oberfläche selbst.

### Weg 1 — Install via YAML (empfohlen)

Zwei Felder, fertig. Unempfindlich gegen Umbenennungen.

1. Links im Menü **Apps**.
   (Die Überschrift der Seite lautet *Applications* — der Menüpunkt heißt **Apps**.)

2. Seite **Discover**.

3. Oben rechts steht ein blauer Knopf **Custom App**. Der ist es **nicht**.
   Direkt daneben ist ein **⋮**-Symbol — dort hinein, Eintrag
   **Install via YAML**.

4. Von rechts fährt ein Fenster **Install via YAML** herein mit zwei Feldern:

   | Feld | Wert |
   |------|------|
   | **Name** | `tuya-smartmeter` |
   | **Custom Config** | der YAML-Text unten |

   Zum Namen: Kleinbuchstaben und Ziffern, Bindestrich erlaubt, aber nicht am
   Anfang oder Ende.

5. **Custom Config** ist ein Editor mit Zeilennummern. Hier hinein:

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

   Der Text **muss** mit `services:` beginnen — seit 25.10 Pflicht. Anzupassen
   ist nur `/mnt/DEIN-POOL/…` auf ein vorhandenes Dataset.

6. Der Knopf heißt **Save**, nicht „Install".

Nach etwa einer Minute steht die App unter **Apps → Installed** auf `Running`.

### Weg 2 — Custom App (das Formular)

Derselbe blaue Knopf **Custom App** auf der Discover-Seite führt zu einer Seite
namens **Install Custom App**. Rechts gibt es eine Sprungliste über alle
Abschnitte. Auszufüllen ist:

| Abschnitt | Feld | Wert |
|-----------|------|------|
| Application name | **Application Name** | `tuya-smartmeter` |
| | **Version** | vorgegeben lassen |
| Image Configuration | **Repository** | `ghcr.io/pascalmd/tuya-smartmeter` |
| | **Tag** | `latest` (steht schon da) |
| | **Pull Policy** | Vorgabe lassen |
| Container Configuration | **Timezone** | `Europe/Berlin` |
| | **Restart Policy** | `unless-stopped` |
| Network Configuration | **Ports** → *Add* | **Host Port** `8099`, **Container Port** `8099` |
| Storage Configuration | **Storage** → *Add* | **Type**: `ixVolume` (Vorgabe), **Mount Path** `/config` |

Abschließen mit **Install** — der Knopf sitzt ganz unten.

Was in den aufklappenden Blöcken sonst noch steht, kann so bleiben:

- **Ports**: *Port Bind Mode* („Publish port on the host for external access"),
  *Protocol* (TCP), *Host IPs* — alles vorbelegt.
- **Storage**: *Read Only* (aus), *ixVolume Configuration* mit *Dataset Name*
  (vorbelegt) — nur der **Mount Path** muss eingetragen werden.
- **Timezone** und **Restart Policy** haben brauchbare Vorgaben.

> **Die häufigste Stolperstelle:** Bei *Ports*, *Storage* und *Environment
> Variables* steht zunächst „No items have been added yet." — die Eingabefelder
> erscheinen erst nach einem Klick auf **Add**. Wer den übersieht, sucht Felder,
> die es scheinbar nicht gibt.

> **Zum Speicher:** Die Vorgabe **ixVolume** bedeutet, dass TrueNAS das Dataset
> selbst anlegt und verwaltet — dann musst du vorher keines erstellen. Wer den
> Ordner lieber selbst bestimmt, stellt **Type** auf *Host Path* und wählt sein
> Dataset.

Beide Wege führen zum selben Ergebnis — beide wurden geprüft, die App lief
danach jeweils auf `Running` und antwortete. Der YAML-Weg ist kürzer und weniger
fehleranfällig: zwei Felder statt sieben, verteilt über sechs Abschnitte.

> **Ein Unterschied, der irritieren kann:** Beim YAML-Weg heißt der
> Abschluss-Knopf **Save**, beim Formular **Install**. Und intern führt TrueNAS
> die beiden verschieden: über YAML angelegte Apps gelten als *custom app*, über
> das Formular angelegte nicht. Für den Betrieb macht das keinen Unterschied.

> **Wichtig:** Der Ordner unter `/config` muss dauerhaft sein. Dort liegen deine
> Zugangsdaten und die Messwert-Historie. Ohne ihn ist nach jedem Neustart alles weg.

---

## Teil 4 — Ersteinrichtung im Browser

Die App meldet sich nach etwa einer halben Minute unter:

```
http://<IP-deines-TrueNAS>:8099
```

Dann führt sie dich durch vier Schritte (weitere Geräte kommen später dazu,
siehe Teil 6):

1. **Passwort festlegen** — damit meldest du dich künftig hier an.
   Dazu Access ID und Access Secret von oben eintragen, Rechenzentrum
   *Central Europe*. Die App prüft die Daten sofort.
2. **Gerät übernehmen** — die Liste kommt aus deinem Tuya-Konto. Den Zähler
   anklicken. Weitere Geräte — eine Schaltsteckdose etwa — kannst du später
   unter *Einstellungen → Geräte* dazunehmen; jedes bekommt seine eigene Regel.
3. **Preisquelle wählen** — Börsenpreise ohne Konto, oder Tibber mit Token.
4. **Automatik einstellen** — siehe unten.

Danach läuft die App dauerhaft weiter, auch wenn du den Browser schließt.

---

## Teil 5 — Automatik einstellen

Vier Regeln stehen zur Wahl:

**Preisschwelle** — am einfachsten zu verstehen.
Du gibst an: einschalten bis z. B. 25 ct/kWh. Liegt der aktuelle Preis darunter,
ist der Zähler ein, sonst aus.

**Günstigste Stunden** — gut für planbaren Verbrauch.
Du gibst an: die 6 günstigsten Stunden des Tages. Die App sucht sie aus den
Tagespreisen heraus und schaltet nur dann ein.

**Günstigster Block am Stück** — für alles, was durchlaufen soll.
Statt der billigsten Stunden einzeln sucht die App den günstigsten
*zusammenhängenden* Block: genau ein Einschalten, genau ein Ausschalten. Für
Geräte, die eine Unterbrechung nicht vertragen oder danach nicht von selbst
weiterlaufen — und jedes Schalten unter Last kostet Relais-Lebensdauer. Kostet
meist nichts extra: Strompreise bilden ein Tal, die billigen Stunden liegen also
ohnehin beieinander.

**Preisstufen** — teilt die Stunden in fünf Stufen ein, gemessen am
Tagesdurchschnitt: sehr günstig, günstig, normal, teuer, sehr teuer. Du hakst an,
bei welchen Stufen eingeschaltet werden soll.

Dazu vier Schutzeinstellungen:

- **Sicherheitsnetz** — nie länger als x Stunden aus. Verhindert, dass ein teurer
  Tag das Gerät dauerhaft abschaltet. Wenn etwas dranhängt, das nicht beliebig
  lange aus sein darf, stell das ein.
- **Mindestlaufzeit** — einmal eingeschaltet, bleibt es so lange an, auch wenn der
  Preis zwischendurch steigt. Wichtig für alles, was nicht nach ein paar Minuten
  wieder abgewürgt werden soll.
- **Mindest-Aus-Zeit** — verhindert schnelles Hin- und Herschalten an der Preisgrenze.
- **Pause nach Handbedienung** — schaltest du selbst auf der Übersicht, hält sich
  die Automatik so lange zurück. Sonst würde sie sofort zurückschalten.

Unten auf der Automatik-Seite siehst du eine **Vorschau der nächsten Stunden** —
dort steht für jede Stunde Preis und ob der Schalter an wäre. Damit kannst du
eine Einstellung prüfen, bevor sie scharf geschaltet wird.

---

## Teil 6 — Mehrere Geräte

Die App steuert beliebig viele Tuya-Geräte nebeneinander: einen Zähler, eine
Schaltsteckdose, beides zusammen. Neue Geräte kommen unter
**Einstellungen → Geräte** dazu — die Liste stammt aus deinem Tuya-Konto, ein
Klick auf *Übernehmen* genügt. Steht ein Gerät nicht in der Liste, kannst du es
unten auf derselben Seite über seine Kennung von Hand eintragen.

**Es gibt genau eine Schaltregel — für alle Geräte.** Das ist Absicht: Der
Strompreis hängt nicht am Gerät. Ist Strom billig, ist er es für jedes.

Je Gerät legst du in der Geräteliste nur zwei Dinge fest:

| Häkchen | Bedeutung |
|---------|-----------|
| **folgt der Regel** | Das Gerät wird von der Automatik geschaltet. Ohne Häkchen bleibt es unberührt und wird nur auf der Übersicht von Hand ein- und ausgeschaltet |
| **abfragen** | Aus heißt: Das Gerät ruht. Es wird nicht abgefragt und erscheint nicht als Störung. Praktisch für eine Steckdose, die erst noch kommt oder über den Winter abgeklemmt ist — Name, Einstellungen und Verlauf bleiben erhalten |

Dazu kommt **Messwerte aufzeichnen**: Eine reine Schaltsteckdose ohne Messung
braucht das nicht, ein Zähler schon.

Welchen Ausgang ein Gerät schaltet, erkennt die App selbst — Zähler nennen ihn
meist `switch`, Steckdosen `switch_1`. Auf der **Übersicht** stehen alle Geräte
mit ihrem Zustand und je einem Ein/Aus-Knopf untereinander; **Verlauf** und
**Preise** zeigen oben eine Leiste zum Umschalten zwischen den Geräten.

> **Wenn du das Entwicklerprojekt benutzt (Weg C):** Jedes Gerät verbraucht sein
> eigenes Abfragekontingent. Mit einem Gerät liegt der Standardtakt bei rund 60 %
> des kostenlosen Monatskontingents, mit zwei Geräten wäre er darüber. Dann unter
> *Einstellungen* das Abfrageintervall erhöhen (300 Sekunden reichen für zwei
> Geräte) — oder besser den lokalen Weg oder die QR-Anmeldung nutzen, die kosten
> gar nichts.

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
| Steckdose lässt sich nicht schalten | Meldet sie einen Ausgang? Auf der Übersicht muss ein Schalter erscheinen. Fehlt er, ist das Gerät vermutlich gar nicht schaltbar (reiner Sensor) |
| Ein Gerät ist dauernd „offline" | Wenn es noch nicht angeschlossen ist: in der Geräteliste **abfragen** ausschalten, dann ruht es, statt als Störung zu erscheinen |
| Healthcheck meldet „degraded" | Ein Gerät antwortet nicht. Welches, steht in der Antwort unter `degraded_devices` |

Detailfehler stehen im Container-Log: **Apps → tuya-smartmeter → Logs**.

### Wenn du nicht weiterkommst

Unter **Einstellungen → Diagnosebericht erstellen** erzeugt die App einen
Bericht über ihren Zustand: Version, Zugangswege, Geräte, Regel, die letzten
Ereignisse, dazu eine Prüfung von Netz und Uhrzeit. Den kannst du als Datei
herunterladen und demjenigen schicken, der dir hilft.

Was **nicht** drinsteht: keine Zugangsdaten — von Secret, Schlüsseln und Token
steht nur da, *ob* sie gesetzt sind und wie lang sie sind. Und kein einziger
Messwert; man sieht dem Bericht also nicht an, wann jemand zu Hause war.

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
