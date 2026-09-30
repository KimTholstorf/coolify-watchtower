FROM python:3.13-alpine

ARG TESTBED_VERSION=dev
ARG TESTBED_BROKEN=false
ENV TESTBED_VERSION=${TESTBED_VERSION} TESTBED_BROKEN=${TESTBED_BROKEN} PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py .
# The `testbed` command, for Coolify's terminal when the web page isn't reachable.
COPY cli.py /usr/local/bin/testbed
RUN chmod 755 /usr/local/bin/testbed && mkdir /data && chown nobody /data
USER nobody
EXPOSE 8080

# While the health check fails during the start period the container is "starting", not "unhealthy".
# A normal start passes within seconds; "slow start" keeps failing for a few minutes on purpose.
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=120s --start-interval=5s \
  CMD python -c "import urllib.request, sys; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1
CMD ["python", "/app/app.py"]
