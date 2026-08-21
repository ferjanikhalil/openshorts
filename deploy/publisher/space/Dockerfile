# OpenShorts publisher — Hugging Face Docker Space
#
# UNUSED as of 2026-08-20, kept only in case the gate lifts: `/new-space` now
# requires PRO for every SDK that runs compute ("Gradio and Docker Spaces require
# a paid plan"), so this file cannot be deployed on a free account. The live
# artifact is the sibling `../Dockerfile`, which builds from a repo root — which is
# how every host that clones a repository builds. This one exists only because a
# Space has no repository to build from, hence the tarball fetch below.
#
# Copy THIS FILE and README.md into the Space repository. Nothing else: the
# source is fetched from the public GitHub repo at build time, so the Space holds
# no second copy of `publishing/` to drift out of date. Rebuilding the Space is
# how you deploy a code change ("Factory rebuild" in Space settings).
#
# THE CORRESPONDING TRAP: the build takes what is on the GitHub branch, not what
# is on your laptop. Commit and push before rebuilding, or you will deploy code
# older than the runbook you are following.
FROM python:3.11-slim

# A branch, tag, or commit SHA. Spaces build with no custom build args, so this
# default is what actually gets used — pin a SHA by editing this line in the
# Space repo when you want a build you can reproduce.
#
# THIS MUST BE THE REPO YOU ACTUALLY PUSH TO. The working tree has two remotes —
# `khalil` (ferjanikhalil/openshorts, which the branch tracks) and `origin`
# (mutonby/openshorts, a divergent line with no publishing/ at all). Both are
# public, so a wrong value here fails at the `cp` below rather than at the
# download, which reads like a Dockerfile bug instead of a wrong source. Confirm
# with the API check in the runbook's Step 0 before a first build.
ARG REPO=ferjanikhalil/openshorts
ARG REF=main

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Same reason as the sibling Dockerfile: this image is only ever the clock
    # half of a split deployment, so declare it here rather than leaving it to a
    # secret an operator can forget. The default is `full`, and a publisher
    # reporting `full` is judged DEAD by the heartbeat Worker — which then alerts
    # every ten minutes forever about a service that is publishing fine.
    PUBLISHING_ROLE=publisher

WORKDIR /app

# Fetched with Python's own urllib and unpacked with tar, both already in the
# base image, so this Dockerfile needs no apt layer at all — no git, no curl, no
# build toolchain. That is the same property the sibling Dockerfile protects, and
# here it also keeps the Space's build inside a free builder's time budget.
#
# Only `publishing/` and `cloud/` survive the copy. `cloud/` comes along because
# publishing borrows its async engine and declarative Base (publishing/db.py);
# its billing modules are imported lazily and stay dormant with BILLING_ENABLED
# unset. Neither app.py nor main.py is kept — importing either would drag in
# torch, mediapipe and the rest of the CV stack this image deliberately lacks,
# and tests/test_publishing_runner.py fails if that ever becomes possible.
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://codeload.github.com/${REPO}/tar.gz/${REF}', '/tmp/src.tar.gz')" \
 && mkdir -p /tmp/src /app/deploy/publisher \
 && tar -xzf /tmp/src.tar.gz -C /tmp/src --strip-components=1 \
 && cp -r /tmp/src/publishing /tmp/src/cloud /app/ \
 && cp /tmp/src/requirements-publishing.txt /app/ \
 && cp /tmp/src/deploy/publisher/requirements.txt /app/deploy/publisher/ \
 && rm -rf /tmp/src /tmp/src.tar.gz

# Resolves the `-r ../../requirements-publishing.txt` include inside that file,
# which is why the two requirements files were copied into the same relative
# positions they occupy in the repo. Both halves of the split deployment then
# resolve identical SQLAlchemy and asyncpg pins while sharing one database.
RUN pip install --no-cache-dir -r /app/deploy/publisher/requirements.txt

# UID 1000 named `user` is the Spaces convention; HOME and PATH follow it so
# anything that writes to a home directory lands somewhere writable.
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Must match `app_port` in README.md's frontmatter. Spaces route the public
# hostname to this port and terminate TLS in front, hence --proxy-headers: the
# provider's webhook arrives over https and generated URLs must say so.
EXPOSE 7860

CMD ["uvicorn", "publishing.runner:app", "--host", "0.0.0.0", "--port", "7860", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
