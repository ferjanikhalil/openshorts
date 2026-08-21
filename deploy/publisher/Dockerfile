# Always-on publisher: the schedule clock, without the video pipeline.
#
# BUILD FROM THE REPOSITORY ROOT, not from this directory:
#     docker build -f deploy/publisher/Dockerfile -t openshorts-publisher .
# (docker-compose.yml here already sets `context: ../..` for that reason.)
# The requirements file beside this one includes `-r ../../requirements-publishing.txt`
# so both halves of a split deployment resolve identical DB pins, and pip only
# resolves that relative include if the two files keep their relative positions.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Declared in the IMAGE, not per-deployment. This image is only ever the
    # clock half of a split deployment, so "no clip files here" is a property of
    # what it is rather than a choice an operator makes — and forgetting it is
    # not a visible failure: health reports role `full`, and the heartbeat Worker
    # treats role != publisher as DEAD and alerts every ten minutes forever while
    # the publisher is in fact working fine.
    PUBLISHING_ROLE=publisher

WORKDIR /app

# No apt layer at all: no ffmpeg, no libGL, no build toolchain. Every wheel below
# ships prebuilt for manylinux x86_64 and aarch64, which is what lets this image
# build on a free-tier ARM VM with no compiler and 1 GB of build memory.
COPY requirements-publishing.txt /app/requirements-publishing.txt
COPY deploy/publisher/requirements.txt /app/deploy/publisher/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/publisher/requirements.txt

# Only what the publisher imports. `cloud/` comes along because publishing
# borrows its async engine and declarative Base (publishing/db.py); its billing
# modules are imported lazily and stay dormant with BILLING_ENABLED unset.
# Neither app.py nor main.py is copied — importing either would drag in the CV
# stack this image deliberately does not have.
COPY publishing/ /app/publishing/
COPY cloud/ /app/cloud/

RUN useradd --create-home --uid 10001 publisher \
    && chown -R publisher:publisher /app
USER publisher

# Documentation for a local run. A managed host assigns its own port through
# $PORT and routes to that instead; see the CMD below.
EXPOSE 8000

# LIVENESS, not configuration — and the distinction is load-bearing. `ok` is
# permanently false on this deployment BY DESIGN: the admin router is
# deliberately left unmounted so the credential endpoints are unreachable from a
# public URL, which leaves one standing warning from
# `admin_auth.config_warnings()`, and `ok` is `not warnings and …`. A healthcheck
# on `ok` therefore marks a correct publisher unhealthy for its entire life, and
# on a managed host that means a restart loop rather than a red dot.
#
# So this checks exactly what the heartbeat Worker checks: HTTP 200, a parseable
# body, and `role == publisher` — which also proves the response came from our
# app and not from a platform holding page. Surfacing `ok`/`warnings` is the
# heartbeat's job (deploy/publisher/heartbeat/src/worker.js), where an operator
# can act on it.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import json,os,sys,urllib.request; \
p=os.environ.get('PORT','8000'); \
d=json.load(urllib.request.urlopen(f'http://127.0.0.1:{p}/api/publishing/health', timeout=8)); \
sys.exit(0 if d.get('role')=='publisher' else 1)"

# `${PORT:-8000}` because every managed container host assigns the port and
# expects the process to bind it (Render injects 10000, Koyeb 8000); 8000 stays
# the default for compose and local runs. Shell form is required — exec form does
# not expand ${PORT} — and `exec` keeps uvicorn as PID 1 so SIGTERM reaches it
# and a claimed attempt is released instead of stranded until orphan recovery.
#
# --proxy-headers so the TLS terminator in front of this (every host in the
# runbook has one) does not turn https callbacks into http in generated URLs.
CMD ["sh", "-c", "exec uvicorn publishing.runner:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
