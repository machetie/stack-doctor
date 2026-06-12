FROM python:3.12-slim

LABEL org.opencontainers.image.title="stack-doctor" \
      org.opencontainers.image.description="Auto-detect and fix recurring Sonarr/Radarr stuck-queue issues (cron or webhook)" \
      org.opencontainers.image.source="https://github.com/Neoo-Blue/stack-doctor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    DOCTOR_STATE_FILE=/data/state.json

WORKDIR /app
COPY doctor.py /app/doctor.py

# no third-party dependencies (Python standard library only).
# Runs as root so a bind-mounted /data (and an optional rw /mnt/library for the
# janitor) is always writable regardless of host ownership.
RUN mkdir -p /data
VOLUME /data

# webhook port (event mode)
EXPOSE 8088

ENTRYPOINT ["python3", "/app/doctor.py"]
