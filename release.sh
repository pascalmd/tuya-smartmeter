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

info "Tests"
python3 tests/test_logic.py 2>&1 | tail -3
ok "bestanden"

info "Auf $BUILD_HOST uebertragen"
rsync -a --delete --exclude '__pycache__' --exclude '.git' ./ "$BUILD_HOST:/tmp/tuya-build/"
ok "uebertragen"

info "Anmelden"
export BW_SESSION="${BW_SESSION:-$(cat ~/.config/bw-claude/session)}"
bw get password "$BW_ITEM" | ssh "$BUILD_HOST" 'docker login ghcr.io -u pascalmd --password-stdin' >/dev/null
ok "angemeldet"

info "Bauen fuer amd64 und arm64, dann hochladen (dauert einige Minuten)"
ssh "$BUILD_HOST" "cd /tmp/tuya-build && docker buildx build \
  --platform linux/amd64,linux/arm64 \
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
