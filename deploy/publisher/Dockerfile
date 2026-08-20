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
    PIP_NO_CACHE_DIR=1

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

EXPOSE 8000

# `ok` is false while anything is misconfigured, which is the condition worth
# alerting on: a publisher whose media origin or admin identity is missing looks
# up but posts nothing. This reports healthy on a clip-less host only because
# api.health treats a declared `publisher` role as legitimately resolver-less.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import json,sys,urllib.request; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/publishing/health', timeout=8)); \
sys.exit(0 if d.get('ok') else 1)"

# --proxy-headers so a TLS terminator in front of this (see the runbook's webhook
# section) does not turn https callbacks into http ones in generated URLs.
CMD ["uvicorn", "publishing.runner:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
