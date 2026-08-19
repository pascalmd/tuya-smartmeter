FROM python:3.12-slim

# Beim Bauen mitgegeben (siehe release.sh), damit in der Oberflaeche steht,
# welche Version laeuft — sonst weiss niemand, ob eine Korrektur schon drin ist.
ARG APP_VERSION=dev
ARG BUILD_DATE=unbekannt
ARG GIT_COMMIT=unbekannt

# Herkunftsangaben nach OCI-Standard. Die erste Zeile ist die wichtige:
# Anhand von image.source ordnet GitHub das Paket dem Repo zu -- unabhaengig
# davon, wo gebaut wurde. Ohne sie steht das Paket dort ohne Bezug, ohne
# Beschreibung und ohne Verweis auf den Quelltext.
LABEL org.opencontainers.image.source="https://github.com/pascalmd/tuya-smartmeter" \
      org.opencontainers.image.title="tuya-smartmeter" \
      org.opencontainers.image.description="Schaltet Tuya-Geraete nach dem aktuellen Strompreis" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$GIT_COMMIT" \
      org.opencontainers.image.created="$BUILD_DATE"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    APP_VERSION=$APP_VERSION \
    BUILD_DATE=$BUILD_DATE \
    GIT_COMMIT=$GIT_COMMIT

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Konfiguration und Messwert-Historie liegen im Volume, nicht im Image.
VOLUME ["/config"]
EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=5).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--proxy-headers", "--forwarded-allow-ips", "*"]
