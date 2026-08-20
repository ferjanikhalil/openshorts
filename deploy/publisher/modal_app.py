"""Modal deployment: the publish clock, off the laptop, on free scheduled compute.

NOT THE CURRENT DEPLOYMENT — Modal requires a credit card at signup, which this
project's hosting constraint rules out. The live runbook deploys
``publishing/runner.py`` to a free Hugging Face Space with a Cloudflare Worker
cron keeping it awake (``deploy/publisher/README.md``); Modal is Appendix B there.
This file is kept, not deleted: it is a complete and verified deployment of the
same tick, and it is the shortest path back if a card ever stops being a problem
or the Space proves unreliable. Everything below still holds.

Why this file exists
--------------------
Nothing in the chain will hold a schedule for us. Status 200 accepts a
``post.scheduledFor`` field and posts immediately anyway — measured over three
real posts, see ``publishing/providers/status200.py`` — and TikTok's and
Instagram's APIs expose no scheduled publish at all. So a clock has to sit
between "the plan says 18:00" and "submit now". On a laptop-only deploy that
clock is the laptop's, which means a slot fires only if the laptop happens to be
awake at it: the plan is written, the rhythm is spread over days, and then every
post goes out in one burst the next time Docker starts.

What makes moving that clock cheap is that a submit reads no local bytes. The
provider downloads the clip from the staging bucket by presigned URL, registered
hours earlier, so at slot time this process needs three things: Postgres, the
master key, and outbound HTTPS. No clip files, no GPU, no models — which is why
``publishing/tick.py`` imports neither ``app`` nor ``main`` and this image
installs none of the video pipeline (enforced by
``tests/test_publishing_tick.py::TestImportIsolation``).

Two functions, deployed together
--------------------------------
``tick``   a cron. Reconciles, drains the due queue, sweeps, exits. One of the
           free tier's five allowed schedules.
``web``    an ASGI endpoint with a permanent HTTPS URL, so the provider's
           webhook has somewhere to land. This is the second half of the
           problem: a callback that arrives is the difference between a
           confirmed post and one that ages into ``unknown``, and ``unknown`` is
           terminal and never auto-retried. It replaces the ngrok tunnel, whose
           URL changed every restart.

Serverless is not a compromise for either. The tick is a batch job by nature.
The webhook handler *verifies, persists and acks* and does no correlation work
inline (``publishing/webhooks.py``), so a container that dies one second after
responding loses nothing — ``worker.reconcile_once`` applies the stored events on
the next tick.

Overlapping ticks are safe by construction, so nothing here limits concurrency.
Claims are taken with ``FOR UPDATE SKIP LOCKED``; the duplicate-post guard is a
partial unique index in the database; and ``dispatcher.dispatch_attempt``
re-checks that a claim is still ours before it submits anything. Those three
together are what let this share one queue with the laptop.

Deploy:
    modal deploy deploy/publisher/modal_app.py     # run from the repository root
Run one pass by hand:
    modal run deploy/publisher/modal_app.py

Full runbook, including the Supabase connection string and what belongs in the
secret: ``deploy/publisher/README.md``.
"""
import os
import pathlib
import sys

import modal

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

# So ``add_local_python_source`` below can resolve the packages no matter which
# directory ``modal deploy`` was invoked from.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# One Modal Secret holds the whole configuration. Create it before deploying:
#   modal secret create openshorts-publishing PUBLISHING_ENABLED=1 ...
# See the README for the full list and for why the master key must be copied
# from the app host rather than generated here.
SECRET_NAME = "openshorts-publishing"

# Every ten minutes. This interval IS the worst-case lateness of a slot: a post
# due at 18:03 goes out at 18:10, because a slot is honoured by the first tick
# after it. Ten minutes is invisible for "three clips a day on a rhythm" and
# costs ~4,300 sub-minute runs a month against the free tier's credit — but the
# free tier allows five schedules, so shortening this is a knob you have, not a
# rewrite.
TICK_SCHEDULE = "*/10 * * * *"

# Comfortably above publishing.tick.DEFAULT_BUDGET_SECONDS (600). The tick is
# built to stop on its own terms before the host kills it: a pass killed
# mid-dispatch strands a claim until recovery ages it out, whereas a pass that
# exits cleanly leaves the queue for the next one. That only works if the host's
# limit is the outer bound of the two.
TICK_TIMEOUT_SECONDS = 900


def _publisher_pins() -> list:
    """The extra pins from ``deploy/publisher/requirements.txt``, minus includes.

    Read from that file rather than restated here, so the container image and
    this deployment cannot drift onto different SQLAlchemy or httpx versions
    while sharing one database. Its ``-r`` include is skipped because Modal
    installs the included file separately below — pip would look for a relative
    path that does not exist inside the image.
    """
    out = []
    for line in (HERE / "requirements.txt").read_text(
            encoding="utf-8").splitlines():
        spec = line.split("#", 1)[0].strip()
        if not spec or spec.startswith(("-r", "-e", "--")):
            continue
        out.append(spec)
    return out


image = (
    modal.Image.debian_slim(python_version="3.11")
    # The DB driver and AES-GCM, shared with the app host.
    .pip_install_from_requirements(str(REPO_ROOT / "requirements-publishing.txt"))
    # FastAPI (the webhook route), httpx (the provider client) and boto3 (which
    # presigns the URL the provider downloads from — not optional here, because
    # without it the media strategy falls back to serving clip bytes from this
    # host, and this host has no clip bytes).
    .pip_install(*_publisher_pins())
    # ``cloud`` comes along because publishing/db.py builds its engine through
    # cloud/database.py. Neither package pulls in the video pipeline.
    .add_local_python_source("publishing", "cloud")
)

app = modal.App("openshorts-publisher")
secret = modal.Secret.from_name(SECRET_NAME)


def _prepare() -> None:
    """Per-container setup that must happen before publishing prints anything."""
    import publishing

    publishing.make_stdio_utf8_safe()
    # Declared, not inferred: health has to tell "no clip files by design" apart
    # from "the resolver failed to register", and only the operator knows which.
    os.environ.setdefault("PUBLISHING_ROLE", "publisher")


@app.function(
    image=image,
    secrets=[secret],
    schedule=modal.Cron(TICK_SCHEDULE),
    timeout=TICK_TIMEOUT_SECONDS,
    # No platform retries. A failed pass needs no retry: the queue is durable,
    # every claim is recoverable, and the next tick is ten minutes away. An
    # immediate retry would only re-enter a database that is probably still
    # unreachable, and would make a transient outage look like a crash loop.
    retries=0,
)
async def tick():
    """One publishing pass. The whole point of this deployment."""
    _prepare()
    from publishing import tick as publishing_tick

    result = await publishing_tick.run_once()
    print(publishing_tick.summarize(result))
    return result


@app.function(image=image, secrets=[secret])
@modal.asgi_app()
def web():
    """Webhook + health, on a URL that does not change between restarts.

    Deliberately NOT ``publishing.runner:app``. The runner starts the dispatch,
    reconcile and transfer loops in its lifespan, which is right for an always-on
    process and wrong here: a container that exists for the length of one webhook
    POST would begin a dispatch it cannot finish, and the claim it took would sit
    ``in_flight`` until recovery aged it out. The cron above owns the work; this
    endpoint only receives and stores.

    That split is safe precisely because ``webhooks.receive_webhook`` verifies,
    persists and acks without interpreting anything. The event is applied by
    ``worker.reconcile_once`` on the next tick.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    _prepare()
    import publishing

    @asynccontextmanager
    async def lifespan(api):
        from publishing import db
        # Engine + create_all. Idempotent, and a few catalogue queries per cold
        # start is a fair price for an endpoint that can never be the reason the
        # schema is missing.
        await db.init()
        yield

    api = FastAPI(
        title="OpenShorts publisher (Modal)",
        description=(
            "Receives provider webhooks and reports health. The publish clock "
            "runs in the scheduled `tick` function, not here."
        ),
        lifespan=lifespan,
    )
    # Mounts /api/publishing/* — including webhook/{provider_name}. The admin
    # router comes with it IF an admin identity is configured in the secret;
    # leave PUBLISHING_ADMIN_TOKEN out of the secret and it stays unmounted. See
    # the README: that choice is the whole exposure decision for this URL.
    publishing.setup_sync(api)
    return api


@app.local_entrypoint()
def main():
    """Run one pass on demand: ``modal run deploy/publisher/modal_app.py``.

    The verification step in the README. It exercises the real secret, the real
    database and the real bucket, so a green line here means a slot arriving at
    03:00 will be handled the same way.
    """
    result = tick.remote()
    print(f"tick result: {result}")
