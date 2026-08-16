#!/usr/bin/env bash
#
# Installiert tuya-smartmeter auf einem Linux-Rechner (Raspberry Pi, Mini-PC, VM).
#
#   curl -sSL https://raw.githubusercontent.com/pascalmd/tuya-smartmeter/main/install.sh | sudo bash
#
# Das Skript ist absichtlich vorsichtig: Es fasst nichts an, was schon läuft,
# installiert Docker nur wenn es fehlt, und weicht auf einen freien Port aus,
# falls der Standardport belegt ist. Ein zweiter Aufruf aktualisiert die
# bestehende Installation, statt sie zu ersetzen.

set -euo pipefail

IMAGE="ghcr.io/pascalmd/tuya-smartmeter:latest"
NAME="tuya-smartmeter"
ZIEL="/opt/tuya-smartmeter"
PORT_WUNSCH="${PORT:-8099}"

rot=$'\e[31m'; gruen=$'\e[32m'; gelb=$'\e[33m'; fett=$'\e[1m'; aus=$'\e[0m'

info()  { echo "${fett}==>${aus} $*"; }
ok()    { echo "  ${gruen}✓${aus} $*"; }
warn()  { echo "  ${gelb}!${aus} $*"; }
fehler() { echo "${rot}Abbruch:${aus} $*" >&2; exit 1; }

# ----------------------------------------------------------------- Vorprüfung

[ "$(id -u)" -eq 0 ] || fehler "Bitte mit sudo ausführen."

info "System prüfen"

case "$(uname -m)" in
  x86_64|amd64)  ok "Architektur: x86_64" ;;
  aarch64|arm64) ok "Architektur: ARM 64-bit" ;;
  armv7l|armv6l)
    fehler "32-bit-System erkannt. Es wird ein 64-bit-Betriebssystem gebraucht.
         Auf einem Raspberry Pi: mit dem Raspberry Pi Imager
         'Raspberry Pi OS Lite (64-bit)' neu aufsetzen." ;;
  *) fehler "Unbekannte Architektur $(uname -m)." ;;
esac

speicher_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$speicher_mb" -lt 400 ]; then
  fehler "Nur ${speicher_mb} MB Arbeitsspeicher — zu wenig. Mindestens 512 MB nötig."
elif [ "$speicher_mb" -lt 900 ]; then
  warn "${speicher_mb} MB Arbeitsspeicher — knapp, sollte aber reichen."
else
  ok "Arbeitsspeicher: ${speicher_mb} MB"
fi

# --------------------------------------------------------------------- Docker

if command -v docker >/dev/null 2>&1; then
  ok "Docker ist bereits installiert ($(docker --version | cut -d' ' -f3 | tr -d ,))"
else
  info "Docker installieren (dauert ein paar Minuten)"
  curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 \
    || fehler "Docker-Installation fehlgeschlagen. Läuft hier ein unterstütztes Linux?"
  systemctl enable --now docker >/dev/null 2>&1 || true
  ok "Docker installiert"
fi

docker compose version >/dev/null 2>&1 \
  || fehler "Docker Compose fehlt. Bitte Docker neu installieren (get.docker.com)."

# Den normalen Benutzer in die docker-Gruppe nehmen, damit er später ohne sudo
# nachsehen kann. Wirkt erst nach dem nächsten Anmelden.
benutzer="${SUDO_USER:-}"
if [ -n "$benutzer" ] && [ "$benutzer" != "root" ]; then
  if ! id -nG "$benutzer" | grep -qw docker; then
    usermod -aG docker "$benutzer" && ok "Benutzer '$benutzer' zur Gruppe docker hinzugefügt"
  fi
fi

# ----------------------------------------------------------------------- Port

port_frei() {
  ! (ss -tlnH "sport = :$1" 2>/dev/null | grep -q . ) 2>/dev/null
}

PORT_GEWAEHLT=""
for kandidat in $(seq "$PORT_WUNSCH" $((PORT_WUNSCH + 20))); do
  # Der eigene Container darf den Port natürlich behalten.
  if docker ps --filter "name=^${NAME}$" --format '{{.Ports}}' 2>/dev/null | grep -q ":${kandidat}->"; then
    PORT_GEWAEHLT="$kandidat"; break
  fi
  if port_frei "$kandidat"; then PORT_GEWAEHLT="$kandidat"; break; fi
done
[ -n "$PORT_GEWAEHLT" ] || fehler "Kein freier Port zwischen $PORT_WUNSCH und $((PORT_WUNSCH + 20)) gefunden."

if [ "$PORT_GEWAEHLT" != "$PORT_WUNSCH" ]; then
  warn "Port $PORT_WUNSCH ist belegt — es wird $PORT_GEWAEHLT verwendet."
else
  ok "Port $PORT_GEWAEHLT ist frei"
fi

# -------------------------------------------------------------- Installieren

info "Dienst einrichten"
mkdir -p "$ZIEL/config"

zeitzone="$(timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Berlin)"

cat > "$ZIEL/docker-compose.yml" <<YAML
services:
  tuya-smartmeter:
    image: ${IMAGE}
    container_name: ${NAME}
    restart: unless-stopped
    ports:
      - "${PORT_GEWAEHLT}:8099"
    volumes:
      - ${ZIEL}/config:/config
    environment:
      TZ: ${zeitzone}
YAML
ok "Konfiguration unter $ZIEL abgelegt"

info "Programm herunterladen und starten"
cd "$ZIEL"
docker compose pull -q 2>/dev/null || docker compose pull
docker compose up -d

# ------------------------------------------------------------------- Prüfung

info "Auf den Dienst warten"
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT_GEWAEHLT}/healthz" >/dev/null 2>&1; then
    ok "Dienst antwortet"
    bereit=1; break
  fi
  sleep 2
done

if [ -z "${bereit:-}" ]; then
  echo
  fehler "Der Dienst antwortet nicht. Was das Protokoll sagt:
         cd $ZIEL && docker compose logs"
fi

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$ip" ] || ip="<IP-dieses-Rechners>"

cat <<ENDE

${gruen}${fett}Fertig.${aus}

  Weiter geht es im Browser:  ${fett}http://${ip}:${PORT_GEWAEHLT}${aus}

  Dort wirst du durch die Einrichtung geführt — Passwort festlegen,
  Tuya-Zugangsdaten eintragen, Gerät wählen, Preisquelle und Automatik.

  Der Dienst läuft ab sofort dauerhaft und startet nach einem Neustart
  von selbst wieder.

  Nützliche Befehle:
    cd ${ZIEL}
    docker compose logs -f      Protokoll ansehen
    docker compose pull && docker compose up -d      aktualisieren
    docker compose down         anhalten

  Deine Daten liegen in ${ZIEL}/config — dort steht alles drin, was du
  eingerichtet hast. Diesen Ordner sichern heißt: alles gesichert.

ENDE
