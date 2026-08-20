"""Publisher-only entrypoint: the always-on half of a split deployment.

Why this exists
---------------
Nothing in the chain will hold a schedule for us. Status 200 accepts a
``scheduledFor`` field and discards it — measured three times over real posts,
see ``providers/status200.py`` — and TikTok's Content Posting API and
Instagram's Graph API expose no scheduled publish at all. So a clock has to sit
between "the plan says 18:00" and "submit now", and on a single-machine deploy
that clock is the laptop's: a slot fires only if the laptop happens to be awake
at it. This entrypoint moves the clock to a host that never sleeps and leaves
the video pipeline — GPU, models, clip files — at home.

What makes that separation possible is that a submit needs no bytes. The
provider downloads the clip from object storage using a presigned URL registered
hours earlier, so at slot time this process needs Postgres, the master key and
outbound HTTPS. Nothing else. It imports neither ``app`` nor ``main``, so torch,
mediapipe, ultralytics and faster-whisper are absent from its image — which is
what lets it run on a free-tier VM.

Sharing the queue needs no new machinery: ``service.claim_due_attempts`` claims
with ``FOR UPDATE SKIP LOCKED``, so this instance and the laptop can both poll
the same table with no broker and no double-submit. The duplicate-post guard is
a partial unique index, so it holds across processes by construction.

Deliberately NOT registered here: a clip resolver
-------------------------------------------------
``clips.set_resolver`` is what ``app.py`` calls to map (job_id, clip_index) to a
file on disk. There are no clip files on this host, so nothing registers one,
and ``dispatcher._staged_info`` covers the gap by answering "where are this
clip's bytes?" from the ``publish_media`` table. That path is load-bearing: the
guard it bypasses is permanent, so a mistake there would not delay a post, it
would destroy it. It is unit-tested in
``tests/test_publishing_media.py::TestPublisherWithoutClipFiles``.

Two of the three background loops idle here rather than being switched off. The
media transfer loop finds no local bytes and skips each candidate without
recording anything (``worker.transfer_once``), and the store sweeper it drives
reads only the database and object ages, so it behaves identically on both
hosts. Leaving them running keeps one code path, and means this same image works
unmodified as a second replica of a host that *does* hold clips.

Run:
    uvicorn publishing.runner:app --host 0.0.0.0 --port 8000
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

import publishing


def _make_stdio_utf8_safe() -> None:
    """Kept as a name here because this entrypoint's boot order depends on it.

    The implementation moved to ``publishing.make_stdio_utf8_safe`` when the
    scheduled entrypoint (``tick.py``) needed the same fix; both call it before
    printing anything.
    """
    publishing.make_stdio_utf8_safe()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """DB engine, boot recovery and the dispatch/reconcile/transfer loops."""
    await publishing.setup_async(app)
    yield
    # No teardown: the loops are asyncio tasks on the loop being closed, and the
    # engine's pool is released with the process. A publisher holds nothing that
    # needs flushing — an in-flight submit is already recorded as `in_flight` and
    # is recovered by `worker.recover_stale_on_boot` on the way back up.


def build_app() -> FastAPI:
    """Assemble the publisher app, refusing to boot in a shape that does nothing.

    The refusals matter more than the assembly. A publisher that boots with
    publishing disabled, or without the master key it needs to read the provider
    credential, looks perfectly healthy while silently publishing nothing — and
    the symptom (posts that never go out) is indistinguishable from the symptom
    this whole deployment exists to fix.
    """
    _make_stdio_utf8_safe()
    if not publishing.is_enabled():
        raise RuntimeError(
            "PUBLISHING_ENABLED is not set, so this publisher would run with "
            "every loop dormant and still report itself up. Set "
            "PUBLISHING_ENABLED=1, or do not run this image."
        )

    # Declared, not inferred: health has to tell "no clip files by design" apart
    # from "the resolver failed to register", and only the operator knows which.
    os.environ.setdefault("PUBLISHING_ROLE", "publisher")

    app = FastAPI(
        title="OpenShorts publisher",
        description=(
            "Always-on publishing worker. Shares one Postgres with the "
            "OpenShorts app; holds the schedule clock and no clip files."
        ),
        lifespan=lifespan,
    )
    # Mounts /api/publishing/* — including the provider webhook route, which is
    # the real second reason to run this on a public host: a callback that lands
    # is the difference between a confirmed post and one that ages into
    # `unknown`, and `unknown` is terminal and never auto-retried.
    publishing.setup_sync(app)
    return app


app = build_app()
