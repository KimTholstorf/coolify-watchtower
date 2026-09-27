FROM python:3.13-alpine

# Timezone data so TZ works without mounting the host's /usr/share/zoneinfo.
RUN apk add --no-cache tzdata

WORKDIR /app
COPY updater.py .

ENV PYTHONUNBUFFERED=1
# Docker is reached over TCP through socket-proxy, so no socket access is needed here.
USER nobody
CMD ["python", "/app/updater.py"]
