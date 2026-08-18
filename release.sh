#!/usr/bin/env bash
#
# Veroeffentlicht den aktuellen Stand: Tests, Multi-Arch-Build, Push nach GHCR.
#
#   ./release.sh 1.4.0
#
# Hintergrund: Waehrend der Entwicklung wird das Image gern nur lokal gebaut, um
# es schnell auszuprobieren. Die Registry bleibt dabei stehen — und wer neu
# installiert, bekommt wochenalten Stand. Genau das ist einmal passiert (acht
# Commits Rueckstand, darunter ein Fehler, der die Automatik lahmlegte). Dieses
# Skript macht den vollstaendigen Weg zum Einzeiler.

set -euo pipefail

VERSION="${1:-}"
BUILD_HOST="${BUILD_HOST:-docker02}"     # dort sind buildx und QEMU eingerichtet
IMAGE="ghcr.io/pascalmd/tuya-smartmeter"
BW_ITEM="GitHub PAT packages (classic)"  # classic PAT, GHCR nimmt keine fine-grained

# Die Tests brauchen die Abhaengigkeiten. Ein vorhandenes venv wird bevorzugt,
# sonst das System-Python (das httpx & Co. meist nicht hat).
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for kandidat in .venv/bin/python venv/bin/python \
      /tmp/claude-*/*/*/scratchpad/venv/bin/python; do
    [ -x "$kandidat" ] && PYTHON="$kandidat" && break
  done
fi
PYTHON="${PYTHON:-python3}"

rot=$'\e[31m'; gruen=$'\e[32m'; fett=$'\e[1m'; aus=$'\e[0m'
info() { echo "${fett}==>${aus} $*"; }
ok()   { echo "  ${gruen}✓${aus} $*"; }
fehler() { echo "${rot}Abbruch:${aus} $*" >&2; exit 1; }

[ -n "$VERSION" ] || fehler "Version angeben, z. B.: ./release.sh 1.4.0"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fehler "Version muss X.Y.Z sein."

cd "$(dirname "$0")"

info "Arbeitsverzeichnis pruefen"
[ -z "$(git status --porcelain)" ] || fehler "Es gibt uncommittete Aenderungen. Erst committen."
[ -z "$(git log origin/main..HEAD --oneline)" ] || fehler "Es gibt ungepushte Commits. Erst pushen."
ok "sauber, $(git log --format='%h %s' -1)"

info "Tests ($PYTHON)"
if ! "$PYTHON" -c "import httpx" 2>/dev/null; then
  fehler "Die Testumgebung fehlt. Mit PYTHON=/pfad/zum/python erneut aufrufen,
         oder: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi
# Alle drei Ebenen, und ein Fehlschlag bricht ab. Vorher lief nur die
# Schaltlogik -- die war jedes Mal in Ordnung, waehrend die Oberflaeche
# Zustaende falsch anzeigte. Genau die pruefen test_ui und test_browser.
for pruefung in tests/test_logic.py tests/test_ui.py tests/test_browser.py tests/test_doku.py; do
  [ -f "$pruefung" ] || continue
  printf '  %-24s' "$(basename "$pruefung")"
  if ausgabe=$("$PYTHON" "$pruefung" 2>&1); then
    echo "$(echo "$ausgabe" | grep -oE 'Ran [0-9]+ tests?' | tail -1) — bestanden"
  else
    echo
    echo "$ausgabe" | grep -vE "INFO|httpx|Deprecation|warnings.warn" | tail -25
    fehler "$(basename "$pruefung") fehlgeschlagen"
  fi
done
ok "bestanden"

info "Auf $BUILD_HOST uebertragen"
rsync -a --delete --exclude '__pycache__' --exclude '.git' ./ "$BUILD_HOST:/tmp/tuya-build/"
ok "uebertragen"

info "Anmelden"
export BW_SESSION="${BW_SESSION:-$(cat ~/.config/bw-claude/session)}"
bw get password "$BW_ITEM" | ssh "$BUILD_HOST" 'docker login ghcr.io -u pascalmd --password-stdin' >/dev/null
ok "angemeldet"

info "Bauen fuer amd64 und arm64, dann hochladen (dauert einige Minuten)"
BUILD_DATE="$(date -u +%Y-%m-%d)"
GIT_COMMIT="$(git rev-parse --short HEAD)"
ssh "$BUILD_HOST" "cd /tmp/tuya-build && docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --build-arg APP_VERSION=$VERSION \
  --build-arg BUILD_DATE=$BUILD_DATE \
  --build-arg GIT_COMMIT=$GIT_COMMIT \
  -t $IMAGE:latest -t $IMAGE:$VERSION --push ." 2>&1 | tail -3

info "Abmelden"
ssh "$BUILD_HOST" 'docker logout ghcr.io' >/dev/null 2>&1 || true

info "Ergebnis pruefen"
ssh "$BUILD_HOST" "docker buildx imagetools inspect $IMAGE:$VERSION" 2>&1 | grep -E "Platform: linux" | sort -u

git tag -a "v$VERSION" -m "Version $VERSION" 2>/dev/null && git push -q origin "v$VERSION" \
  && ok "Git-Tag v$VERSION gesetzt" || echo "  (Tag v$VERSION existiert schon)"

cat <<ENDE

${gruen}${fett}Veroeffentlicht: $IMAGE:$VERSION${aus}

  Bestehende Installationen aktualisieren:
    docker compose pull && docker compose up -d

ENDE
