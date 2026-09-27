FROM python:3.13-alpine

# Timezone data so TZ works without mounting the host's /usr/share/zoneinfo.
RUN apk add --no-cache tzdata

WORKDIR /app
COPY updater.py .

ENV PYTHONUNBUFFERED=1
# Docker is reached over TCP through socket-proxy, so no socket access is needed here.
USER nobody
# Healthy while the main loop keeps touching /tmp/heartbeat (every ~60 s).
HEALTHCHECK --interval=60s --timeout=10s --start-period=5m --retries=3 \
  CMD python -c "import os, sys, time; sys.exit(time.time() - os.path.getmtime('/tmp/heartbeat') > 180)"
CMD ["python", "/app/updater.py"]
