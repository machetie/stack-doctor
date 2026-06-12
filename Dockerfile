FROM python:3.12-slim

LABEL org.opencontainers.image.title="arr-sentinel" \
      org.opencontainers.image.description="Auto-detect and fix recurring Sonarr/Radarr stuck-queue issues (cron or webhook)" \
      org.opencontainers.image.source="https://github.com/Neoo-Blue/arr-sentinel" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    SENTINEL_STATE_FILE=/data/state.json

WORKDIR /app
COPY sentinel.py /app/sentinel.py

# no third-party dependencies (Python standard library only)
RUN useradd -r -u 1000 sentinel && mkdir -p /data && chown sentinel:sentinel /data
VOLUME /data
USER sentinel

# webhook port (event mode)
EXPOSE 8088

ENTRYPOINT ["python3", "/app/sentinel.py"]
