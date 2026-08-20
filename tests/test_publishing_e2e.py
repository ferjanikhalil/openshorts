"""End-to-end dry run: the real dispatcher, the real tables, the fake provider.

Everything else in the publishing suite is pure or transport-mocked. This file
covers the one thing those cannot: that the orchestration actually holds together
against a live Postgres — that the partial unique index really does refuse a
second live attempt, that a retry row really does slot in after a failure, that
`FOR UPDATE SKIP LOCKED` really does hand one attempt to one worker.

It needs a database, so it SKIPS unless `PUBLISHING_TEST_DATABASE_URL` points at
a throwaway Postgres. CI has none and skips the whole module; a developer runs:

    docker run -d --rm --name pgtest -e POSTGRES_PASSWORD=p -e POSTGRES_USER=p \
        -e POSTGRES_DB=p -p 55432:5432 postgres:16-alpine
    PUBLISHING_TEST_DATABASE_URL=postgresql+asyncpg://p:p@localhost:55432/p \
        pytest tests/test_publishing_e2e.py -v

Nothing here can reach a real provider: `PUBLISHING_DRY_RUN` forces every lookup
to the in-repo fake, and the api key is a made-up `rl_test_…` string.

Platform note: asyncpg's connection pool holds connections open until the
event loop that created them is torn down, so the loop that calls
`pdb.init()` must also run the tests. Rather than a fresh `asyncio.run`
per test, one module-scoped loop is created once and shared — the sessionmaker
and the pool were born inside it, and every coroutine runs in it.
"""
import asyncio
import base64
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DB_URL = os.getenv("PUBLISHING_TEST_DATABASE_URL", "")
if not DB_URL:
    pytest.skip("set PUBLISHING_TEST_DATABASE_URL to run the e2e dry run",
                allow_module_level=True)

pytest.importorskip("sqlalchemy")
pytest.importorskip("asyncpg")
pytest.importorskip("cryptography")

MASTER_KEY = base64.b64encode(bytes(range(32))).decode()
API_KEY = "rl_test_0000111122223333"

os.environ.update(
    PUBLISHING_ENABLED="1",
    PUBLISHING_DRY_RUN="1",
    PUBLISHING_MASTER_KEY=MASTER_KEY,
    PUBLISHING_PUBLIC_BASE_URL="https://clips.test.invalid",
    # The pacer's transfer gap is real protection in production and pure
    # slowness here: many tests register media back to back. It has its own
    # dedicated test below with the gap set explicitly.
    PUBLISHING_MEDIA_REGISTRATION_GAP="0",
    DATABASE_URL=DB_URL,
)

from sqlalchemy import delete, func, select, text  # noqa: E402

from cloud import database as cloud_db  # noqa: E402
from publishing import clips, crypto, dispatcher, planner, service, state  # noqa: E402
from publishing import db as pdb  # noqa: E402
from publishing import models as m  # noqa: E402
from publishing.config import ORPHAN_CLAIM_MIN_AGE_SECONDS  # noqa: E402
from publishing.providers import fake  # noqa: E402

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


# --- fixtures ---------------------------------------------------------------
CLIPS = {}


def _resolver(job_id, clip_index):
    """Stands in for app._resolve_clip_for_publishing. No file is ever read."""
    return CLIPS.get((job_id, clip_index))


def clip(caption="a normal clip", size_bytes=20 * 1024 * 1024):
    return {
        "output_dir": "/tmp/does-not-matter",
        "filename": "clip.mp4",
        "user_id": None,
        "title": "title",
        "caption": caption,
        "duration": 42.0,
        "size_bytes": size_bytes,
        "mtime": 0.0,
        "fingerprint": uuid.uuid4().hex,
    }


async def _truncate():
    async with pdb.session() as s:
        for table in ("publish_events", "provider_webhook_events",
                      "publish_attempts", "publish_media", "publish_requests",
                      "publish_assignments", "publish_destinations",
                      "publish_credentials", "publish_groups"):
            await s.execute(text(f"DELETE FROM {table}"))
        await s.commit()


@pytest.fixture(scope="module", autouse=True)
def database():
    clips.set_resolver(_resolver)
    run(pdb.init())
    run(_truncate())
    yield
    run(_truncate())
    # Return the pooled connections to the loop that opened them, then close it.
    run(cloud_db._engine.dispose())
    _LOOP.close()


@pytest.fixture(autouse=True)
def clean():
    fake.reset()
    CLIPS.clear()
    run(_truncate())
    yield


async def _group(name="batch-1", platforms=("youtube", "instagram", "tiktok"),
                 api_key=API_KEY):
    """A group with a sealed credential and one destination per platform."""
    async with pdb.session() as s:
        g = m.PublishGroup(name=name, provider="status200", enabled=True)
        s.add(g)
        await s.flush()

        aad = f"publish_credentials:api_key:{g.id}"
        blob = crypto.encrypt(api_key, aad=aad)
        s.add(m.PublishCredential(
            publish_group_id=g.id, provider="status200", kind="api_key",
            active=True, **blob))

        dests = []
        for p in platforms:
            d = m.PublishDestination(
                publish_group_id=g.id, provider="status200", platform=p,
                provider_account_ref=f"acct_{name}_{p}", enabled=True,
                health="unverified", display_name=f"{name} {p}")
            s.add(d)
            dests.append(d)
        await s.flush()
        await s.commit()
        return g.id, [d.id for d in dests]


async def _publish(job_id, clip_index, dest_ids, caption="a normal clip",
                   **over):
    CLIPS[(job_id, clip_index)] = clip(caption=caption, **over)
    async with pdb.session() as s:
        dests = await service.expand_destinations(s, destination_ids=dest_ids)
        req = await service.create_request(
            s, job_id=job_id, clip_index=clip_index, destinations=dests,
            payload={"caption": caption},
            content_fingerprint=CLIPS[(job_id, clip_index)]["fingerprint"],
            mode="multi", actor="test")
        await s.commit()
        return req.id, [d.id for d in dests]


async def _drain(limit=20, rounds=4):
    """Claim and dispatch until nothing is due. Returns every outcome string."""
    outcomes = []
    for _ in range(rounds):
        async with pdb.session() as s:
            attempts = await service.claim_due_attempts(s, "worker-test",
                                                        limit=limit)
            await s.commit()
            if not attempts:
                break
            for a in attempts:
                outcomes.append(await dispatcher.dispatch_attempt(s, a))
            await s.commit()
    return outcomes


async def _attempts(request_id):
    async with pdb.session() as s:
        return (await s.execute(
            select(m.PublishAttempt)
            .where(m.PublishAttempt.publish_request_id == request_id)
            .order_by(m.PublishAttempt.created_at,
                      m.PublishAttempt.attempt_number))).scalars().all()


async def _request(request_id):
    async with pdb.session() as s:
        return await s.get(m.PublishRequest, request_id)


# --- the happy fan-out ------------------------------------------------------
class TestFanOut:
    def test_one_clip_to_three_accounts_publishes_three_times(self):
        async def go():
            _, dest_ids = await _group()
            req_id, _ = await _publish("job-a", 0, dest_ids)

            outcomes = await _drain()
            assert outcomes == ["succeeded"] * 3

            rows = await _attempts(req_id)
            assert len(rows) == 3
            assert {r.status for r in rows} == {state.SUCCEEDED}
            assert all(r.provider_post_ref for r in rows)
            # Derived, never written.
            assert (await _request(req_id)).status == state.REQ_SUCCEEDED
            # One platform per submit, three distinct accounts.
            assert len(fake.submissions) == 3
            assert len({s["account"] for s in fake.submissions}) == 3
        run(go())

    def test_media_is_uploaded_once_and_reused_across_platforms(self):
        async def go():
            _, dest_ids = await _group()
            await _publish("job-b", 0, dest_ids)
            await _drain()
            # THE point of the media cache: one upload, three posts.
            assert len(fake.uploads) == 1
            assert len(fake.submissions) == 3
            assert len({s["media_ref"] for s in fake.submissions}) == 1
        run(go())

    def test_first_publish_verifies_an_unverified_destination(self):
        async def go():
            _, dest_ids = await _group(platforms=("youtube",))
            await _publish("job-c", 0, dest_ids)
            await _drain()
            async with pdb.session() as s:
                d = await s.get(m.PublishDestination, dest_ids[0])
                assert d.health == "ok"
                assert d.verified_at is not None
        run(go())

    def test_quota_is_recorded_from_the_response(self):
        async def go():
            _, dest_ids = await _group(platforms=("tiktok",))
            await _publish("job-d", 0, dest_ids)
            await _drain()
            async with pdb.session() as s:
                d = await s.get(m.PublishDestination, dest_ids[0])
                assert d.quota_limit == 5 and d.quota_remaining == 4
        run(go())
# --- the guard that matters -------------------------------------------------
class TestNeverPostTwice:
    def test_the_database_refuses_a_second_live_attempt(self):
        # Not application logic: the partial unique index. Asserted directly,
        # because every other protection in the system sits on top of it.
        async def go():
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-e", 0, dest_ids)
            from sqlalchemy.exc import IntegrityError
            with pytest.raises(IntegrityError):
                async with pdb.session() as s:
                    s.add(m.PublishAttempt(
                        publish_request_id=req_id,
                        publish_destination_id=dest_ids[0],
                        provider="status200", platform="youtube",
                        attempt_number=1, status=state.PENDING))
                    await s.commit()
        run(go())

    def test_a_failed_row_does_not_block_its_retry(self):
        # The index is partial for exactly this reason.
        async def go():
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-f", 0, dest_ids,
                                       caption="fail-transient please")
            await _drain(rounds=1)

            rows = await _attempts(req_id)
            assert len(rows) == 2, "a retry row should exist"
            assert rows[0].status == state.FAILED
            assert rows[1].status == state.DEFERRED
            assert rows[1].attempt_number == 2
            assert rows[1].deferred_until is not None
            # Request is not failed while a retry is pending.
            assert (await _request(req_id)).status != state.REQ_FAILED
        run(go())

    def test_double_publish_of_the_same_selection_is_one_request(self):
        async def go():
            _, dest_ids = await _group()
            req_a, _ = await _publish("job-g", 0, dest_ids)
            req_b, _ = await _publish("job-g", 0, dest_ids)
            assert req_a == req_b
            assert len(await _attempts(req_a)) == 3
        run(go())

    def test_losing_the_idempotency_race_returns_the_winner_not_a_500(self):
        """The SELECT in create_request is a fast path, not the guarantee.

        Two concurrent callers both pass it, and the UNIQUE column decides. This
        forces that interleaving deterministically by making the fast-path
        lookup miss once, the way it would for a caller whose SELECT ran before
        the winner committed.
        """
        async def go():
            _, dest_ids = await _group()
            req_a, _ = await _publish("job-race", 0, dest_ids)

            real = service._request_by_key
            calls = {"n": 0}

            async def miss_once(session, key):
                calls["n"] += 1
                if calls["n"] == 1:
                    return None          # pretend the winner hasn't committed
                return await real(session, key)

            service._request_by_key = miss_once
            try:
                req_b, _ = await _publish("job-race", 0, dest_ids)
            finally:
                service._request_by_key = real

            assert calls["n"] == 2, "the recovery lookup should have run"
            assert req_b == req_a
            # And no orphaned second set of attempts.
            assert len(await _attempts(req_a)) == 3
            async with pdb.session() as s:
                total = (await s.execute(
                    select(func.count()).select_from(m.PublishAttempt)
                )).scalar_one()
                assert total == 3
        run(go())

    def test_a_different_account_set_is_a_different_request(self):
        async def go():
            _, dest_ids = await _group()
            req_a, _ = await _publish("job-h", 0, dest_ids)
            req_b, _ = await _publish("job-h", 0, dest_ids[:1])
            assert req_a != req_b
        run(go())

    def test_naming_a_group_and_its_member_does_not_double_post(self):
        async def go():
            gid, dest_ids = await _group()
            CLIPS[("job-i", 0)] = clip()
            async with pdb.session() as s:
                dests = await service.expand_destinations(
                    s, destination_ids=[dest_ids[0]], group_ids=[gid])
                assert len(dests) == 3
        run(go())

    def test_skip_locked_hands_each_attempt_to_one_worker(self):
        async def go():
            _, dest_ids = await _group()
            await _publish("job-j", 0, dest_ids)
            # Two overlapping claims; the second must see nothing left.
            async with pdb.session() as a, pdb.session() as b:
                first = await service.claim_due_attempts(a, "w1", limit=10)
                second = await service.claim_due_attempts(b, "w2", limit=10)
                assert len(first) == 3
                assert second == []
                await a.commit()
                await b.commit()
        run(go())
# --- planner reports existing requests, does not fake-queue ------------------
class TestRepeatedJobPlan:
    def test_repeated_plan_reports_existing_instead_of_created(self):
        """The Auto Post false-success: a repeat call reused the idempotency
        key, so service.create_request returns the old request. The planner
        must put it under ``existing`` (not ``created``) so the dashboard can
        say "already published" rather than "1 post queued" with no work done.
        """
        async def go():
            _, dest_ids = await _group(name="plan-dup", platforms=("tiktok",))
            CLIPS[("job-plan-duplicate", 0)] = clip()
            plan = {"destination_ids": [str(dest_ids[0])], "immediate": True}

            async with pdb.session() as s:
                async with s.begin():
                    first = await planner.plan_job(
                        s, job_id="job-plan-duplicate", clip_count=1,
                        plan=plan, actor="test")
            async with pdb.session() as s:
                async with s.begin():
                    second = await planner.plan_job(
                        s, job_id="job-plan-duplicate", clip_count=1,
                        plan=plan, actor="test")

            assert len(first["created"]) == 1
            assert first["existing"] == []
            # The repeat must NOT advertise new work.
            assert second["created"] == []
            assert len(second["existing"]) == 1
            assert (second["existing"][0]["request_id"]
                    == first["created"][0]["request_id"])
            # And no extra attempt may appear for the reused request.
            assert len(await _attempts(
                uuid.UUID(first["created"][0]["request_id"]))) == 1

        run(go())


# --- failure modes ----------------------------------------------------------
class TestFailureModes:
    def test_unknown_is_terminal_and_never_auto_retried(self):
        # The one that would double-post a real audience if retried blindly.
        async def go():
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-k", 0, dest_ids,
                                       caption="fail-unknown case")
            await _drain()
            rows = await _attempts(req_id)
            assert len(rows) == 1, "no retry row may be created for unknown"
            assert rows[0].status == state.UNKNOWN
            assert rows[0].status in state.NEEDS_ATTENTION
            # There is no request-level "needs attention" status: attention is a
            # property of the ATTEMPT (state.NEEDS_ATTENTION), and a request
            # whose every attempt is lost derives to failed.
            assert (await _request(req_id)).status == state.REQ_FAILED
        run(go())

    def test_a_403_blocks_the_destination_not_just_the_post(self):
        async def go():
            _, dest_ids = await _group(platforms=("instagram",))
            req_id, _ = await _publish("job-l", 0, dest_ids,
                                       caption="fail-blocked account")
            await _drain()
            rows = await _attempts(req_id)
            assert rows[-1].status in (state.DEAD, state.FAILED, state.BLOCKED)
            async with pdb.session() as s:
                d = await s.get(m.PublishDestination, dest_ids[0])
                assert d.health == "blocked"
        run(go())

    def test_a_401_invalidates_the_credential_so_the_rest_stop_early(self):
        async def go():
            gid, dest_ids = await _group()
            req_id, _ = await _publish("job-m", 0, dest_ids,
                                       caption="fail-auth here")
            await _drain()
            async with pdb.session() as s:
                cred = (await s.execute(select(m.PublishCredential).where(
                    m.PublishCredential.publish_group_id == gid
                ))).scalar_one()
                assert cred.invalid_at is not None
            rows = await _attempts(req_id)
            # The attempt that saw the 401 is DEAD — a bad key is never retried.
            dead = [r for r in rows if r.status == state.DEAD]
            assert len(dead) == 1
            assert dead[0].error_code == "auth_invalid"
            # "Stop early" is the point: with the credential now invalid,
            # active_credential() returns nothing, so the two remaining
            # destinations defer without ever reaching the provider. Deferred,
            # not dead — the operator can still paste a working key.
            assert sum(r.status == state.DEFERRED for r in rows) == 2
            assert len(fake.submissions) == 1
        run(go())

    def test_an_account_401_disconnects_that_destination_only(self):
        # The 2026-08-17 regression, end to end. One connected account's platform
        # token expires; the provider answers 401. Before the fix that 401 was
        # read as "bad API key", the group's credential was invalidated, and the
        # two healthy platforms then parked every post every 15 minutes forever.
        async def go():
            gid, dest_ids = await _group(name="acct-auth")
            bad_id = dest_ids[1]
            # Scope the failure to ONE destination: _marker() reads the account
            # ref, so only this platform's submit raises — exactly like a single
            # expired token at the provider.
            async with pdb.session() as s:
                d = await s.get(m.PublishDestination, bad_id)
                d.provider_account_ref = "acct_fail-account-auth"
                await s.commit()

            req_id, _ = await _publish("job-m2", 0, dest_ids)
            await _drain()

            async with pdb.session() as s:
                cred = (await s.execute(select(m.PublishCredential).where(
                    m.PublishCredential.publish_group_id == gid
                ))).scalar_one()
                # THE guard: one account's dead token must not disable the key.
                assert cred.invalid_at is None
                bad = await s.get(m.PublishDestination, bad_id)
                assert bad.health == "disconnected"
                assert bad.health_detail
                others = [await s.get(m.PublishDestination, i)
                          for i in dest_ids if i != bad_id]
                assert all(o.health == "ok" for o in others)

            rows = await _attempts(req_id)
            # Destination-fatal, so BLOCKED and no retry row — but only for the
            # one destination whose token died.
            blocked = [r for r in rows if r.status == state.BLOCKED]
            assert len(blocked) == 1
            assert blocked[0].error_code == "account_reauth_required"
            assert blocked[0].publish_destination_id == bad_id
            assert len(rows) == 3, "a destination-fatal error creates no retry"
            # The other two really published; nothing deferred, nothing parked.
            assert sum(r.status == state.SUCCEEDED for r in rows) == 2
            assert not [r for r in rows if r.status == state.DEFERRED]
        run(go())


        # The provider parked the post (daily cap reached): the post EXISTS on
        # their side, so the row must be `submitted` with a window, NOT deferred —
        # a deferred row gets re-claimed and re-submitted at timer expiry, which
        # publishes a duplicate once the window opens.
        async def go():
            _, dest_ids = await _group(platforms=("tiktok",))
            req_id, _ = await _publish("job-n", 0, dest_ids,
                                       caption="quota reached")
            await _drain(rounds=1)
            rows = await _attempts(req_id)
            assert rows[-1].status == state.SUBMITTED
            assert rows[-1].deferred_until is not None
            assert rows[-1].provider_post_ref
            # Still in progress from the operator's point of view: the webhook
            # that confirms it can still arrive.
            assert (await _request(req_id)).status == state.REQ_IN_PROGRESS
            async with pdb.session() as s:
                d = await s.get(m.PublishDestination, dest_ids[0])
                assert d.quota_remaining == 0
        run(go())

    def test_an_exhausted_quota_defers_without_submitting(self):
        # The whole reason dispatch is quota-aware: not spending an attempt (or
        # earning a 202 to reconcile) on a cap we already know is reached.
        async def go():
            _, dest_ids = await _group(platforms=("tiktok",))
            await _publish("job-o", 0, dest_ids, caption="quota reached")
            await _drain(rounds=1)
            fake.reset()
            await _publish("job-o", 1, dest_ids)
            outcomes = await _drain(rounds=1)
            assert outcomes == ["quota"]
            assert fake.submissions == [], "must not reach the provider"
        run(go())

    def test_an_oversize_clip_is_refused_before_the_provider_sees_it(self):
        async def go():
            _, dest_ids = await _group(platforms=("instagram",))
            req_id, _ = await _publish("job-p", 0, dest_ids,
                                       size_bytes=2 * 1024 ** 3)
            outcomes = await _drain()
            assert outcomes[0] == "too_large"
            assert fake.submissions == []
            assert fake.uploads == []
            rows = await _attempts(req_id)
            assert rows[-1].status == state.DEAD
        run(go())

    def test_a_missing_clip_fails_permanently_with_a_reason(self):
        async def go():
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-q", 0, dest_ids)
            CLIPS.clear()  # job retention removed the file
            outcomes = await _drain()
            assert outcomes == ["clip_missing"]
            rows = await _attempts(req_id)
            assert rows[-1].status == state.DEAD
            assert "retention" in (rows[-1].error_message or "")
        run(go())

    def test_a_disabled_destination_is_dropped_at_expansion(self):
        async def go():
            _, dest_ids = await _group()
            async with pdb.session() as s:
                d = await s.get(m.PublishDestination, dest_ids[0])
                d.enabled = False
                await s.commit()
            async with pdb.session() as s:
                dests = await service.expand_destinations(
                    s, destination_ids=dest_ids)
                assert len(dests) == 2
        run(go())
# --- partial outcomes, webhooks, sweeper ------------------------------------
class TestPartialAndReconciliation:
    def test_one_failed_platform_makes_the_request_partial(self):
        # The state that matters operationally: collapsing it either way loses
        # the only fact the operator needs.
        async def go():
            gid, dest_ids = await _group()
            async with pdb.session() as s:
                d = await s.get(m.PublishDestination, dest_ids[2])
                d.provider_account_ref = "acct_fail-blocked_tiktok"
                await s.commit()
            req_id, _ = await _publish("job-r", 0, dest_ids)
            await _drain()
            rows = await _attempts(req_id)
            assert sum(r.status == state.SUCCEEDED for r in rows) == 2
            assert (await _request(req_id)).status == state.REQ_PARTIAL
        run(go())

    def test_a_webhook_completes_a_submitted_attempt(self):
        async def go():
            from publishing import webhooks
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-s", 0, dest_ids,
                                       caption="slow clip")
            await _drain(rounds=1)
            rows = await _attempts(req_id)
            assert rows[-1].status == state.SUBMITTED
            post_ref = rows[-1].provider_post_ref

            async with pdb.session() as s:
                # The row carries only the raw envelope; every provider-specific
                # field is read back out of `payload` by parse_webhook.
                s.add(m.ProviderWebhookEvent(
                    provider="status200", provider_event_id="evt_1",
                    event_type="post.published", signature_valid=True,
                    processed=False,
                    payload={"id": "evt_1", "type": "post.published",
                             "data": {"post_id": post_ref,
                                      "permalink": "https://example.invalid/x"}}))
                await s.commit()
            # drain_pending opens its own session and transaction.
            assert await webhooks.drain_pending() >= 1

            rows = await _attempts(req_id)
            assert rows[-1].status == state.SUCCEEDED
            assert (await _request(req_id)).status == state.REQ_SUCCEEDED
        run(go())

    def test_an_ambiguous_failed_webhook_leaves_an_unknown_attempt_unknown(self):
        # The 2026-08-11 incident end to end: the post is already `unknown`
        # (no confirmation arrived) and the provider's failure is "Timeout" —
        # a second admission of ignorance. Staying unknown keeps it out of the
        # retry queue; only a human may decide.
        async def go():
            from publishing import webhooks
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-amb", 0, dest_ids,
                                       caption="slow clip")
            await _drain(rounds=1)
            rows = await _attempts(req_id)
            post_ref = rows[-1].provider_post_ref
            async with pdb.session() as s:
                rows[-1].status = state.UNKNOWN
                await s.commit()
                s.add(m.ProviderWebhookEvent(
                    provider="status200", provider_event_id="evt_amb",
                    event_type="post.failed", signature_valid=True,
                    processed=False,
                    payload={"id": "evt_amb", "type": "post.failed",
                             "data": {"post_id": post_ref,
                                      "error": "Timeout",
                                      "error_code": "unknown"}}))
                await s.commit()
            assert await webhooks.drain_pending() >= 1
            rows = await _attempts(req_id)
            assert rows[-1].status == state.UNKNOWN
            # No retry attempt may exist — that is the whole point.
            assert len(rows) == 1
            assert (await _request(req_id)).status == state.REQ_FAILED
        run(go())

    def test_a_definite_failed_webhook_makes_an_unknown_attempt_retryable(self):
        # The mirror case: the webhook tells us the post is DEFINITELY not live,
        # so the ambiguity resolves in the safe direction and a retry becomes
        # legitimate.
        async def go():
            from publishing import webhooks
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-def", 0, dest_ids,
                                       caption="slow clip")
            await _drain(rounds=1)
            rows = await _attempts(req_id)
            post_ref = rows[-1].provider_post_ref
            async with pdb.session() as s:
                rows[-1].status = state.UNKNOWN
                await s.commit()
                s.add(m.ProviderWebhookEvent(
                    provider="status200", provider_event_id="evt_def",
                    event_type="post.failed", signature_valid=True,
                    processed=False,
                    payload={"id": "evt_def", "type": "post.failed",
                             "data": {"post_id": post_ref,
                                      "error": "video too long for platform",
                                      "error_code": "provider_error"}}))
                await s.commit()
            assert await webhooks.drain_pending() >= 1
            rows = await _attempts(req_id)
            assert rows[0].status == state.FAILED
            assert rows[0].error_code == "provider_error"
            # A legitimate retry was scheduled.
            assert rows[1].attempt_number == 2
            assert rows[1].status == state.DEFERRED
            assert rows[1].deferred_until is not None
            assert (await _request(req_id)).status == state.REQ_DEFERRED
        run(go())

    def test_a_replayed_webhook_is_a_no_op(self):
        async def go():
            from sqlalchemy.exc import IntegrityError
            _, dest_ids = await _group(platforms=("youtube",))
            await _publish("job-t", 0, dest_ids, caption="slow clip")
            await _drain(rounds=1)
            row = dict(provider="status200", provider_event_id="evt_dup",
                       event_type="post.published", signature_valid=True,
                       processed=False, payload={"id": "evt_dup"})
            async with pdb.session() as s:
                s.add(m.ProviderWebhookEvent(**row))
                await s.commit()
            # The UNIQUE on (provider, provider_event_id) is the replay guard —
            # the signature itself carries no timestamp or nonce.
            with pytest.raises(IntegrityError):
                async with pdb.session() as s:
                    s.add(m.ProviderWebhookEvent(**row))
                    await s.commit()
        run(go())

    def test_the_stale_sweeper_moves_a_lost_submit_to_unknown(self):
        async def go():
            from datetime import datetime, timedelta, timezone
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-u", 0, dest_ids,
                                       caption="slow clip")
            await _drain(rounds=1)

            async with pdb.session() as s:
                rows = (await s.execute(select(m.PublishAttempt).where(
                    m.PublishAttempt.publish_request_id == req_id
                ))).scalars().all()
                rows[-1].submitted_at = (datetime.now(timezone.utc)
                                         - timedelta(hours=3))
                await s.commit()
                swept = await service.sweep_stale_submitted(s)
                await s.commit()
            assert swept >= 1
            rows = await _attempts(req_id)
            assert rows[-1].status == state.UNKNOWN
            assert rows[-1].status in state.NEEDS_ATTENTION
            assert (await _request(req_id)).status == state.REQ_FAILED
        run(go())

    def test_boot_recovery_reclaims_an_orphaned_claim(self):
        async def go():
            _, dest_ids = await _group(platforms=("youtube",))
            req_id, _ = await _publish("job-v", 0, dest_ids)
            async with pdb.session() as s:
                attempts = await service.claim_due_attempts(s, "dead-worker")
                assert len(attempts) == 1
                await s.commit()

            # A claim made a moment ago is NOT an orphan. Two processes share
            # this queue — the app host and the always-on publisher — so a
            # recovery pass regularly runs while the other one is mid-batch, and
            # re-queuing a claim someone is still working through is how one clip
            # becomes two posts.
            async with pdb.session() as s:
                assert await service.recover_orphaned_claims(s) == 0
                await s.commit()

            # Age the claim past the bound: now the worker really is gone.
            async with pdb.session() as s:
                rows = await _attempts(req_id)
                attempt = await s.get(m.PublishAttempt, rows[-1].id)
                attempt.claimed_at = (
                    datetime.now(timezone.utc)
                    - timedelta(seconds=ORPHAN_CLAIM_MIN_AGE_SECONDS + 60))
                await s.commit()
            async with pdb.session() as s:
                assert await service.recover_orphaned_claims(s) >= 1
                await s.commit()
            outcomes = await _drain()
            assert outcomes == ["succeeded"]
        run(go())


# --- credential handling ----------------------------------------------------
class TestCredentials:
    def test_the_plaintext_key_is_in_no_column(self):
        async def go():
            gid, _ = await _group()
            async with pdb.session() as s:
                cred = (await s.execute(select(m.PublishCredential).where(
                    m.PublishCredential.publish_group_id == gid
                ))).scalar_one()
                for col in cred.__table__.columns:
                    assert API_KEY not in str(getattr(cred, col.name))
                assert cred.last4 == API_KEY[-4:]
        run(go())

    def test_each_group_uses_its_own_credential(self):
        async def go():
            _, a_dests = await _group(name="batch-a", platforms=("youtube",),
                                      api_key="rl_test_aaaaaaaaaaaaaaaa")
            _, b_dests = await _group(name="batch-b", platforms=("youtube",),
                                      api_key="rl_test_bbbbbbbbbbbbbbbb")
            await _publish("job-w", 0, a_dests + b_dests)
            await _drain()
            assert len(fake.submissions) == 2
        run(go())

    def test_a_group_with_no_credential_defers_rather_than_failing(self):
        async def go():
            gid, dest_ids = await _group(platforms=("youtube",))
            async with pdb.session() as s:
                await s.execute(delete(m.PublishCredential).where(
                    m.PublishCredential.publish_group_id == gid))
                await s.commit()
            req_id, _ = await _publish("job-x", 0, dest_ids)
            outcomes = await _drain(rounds=1)
            assert outcomes == ["no_credential"]
            rows = await _attempts(req_id)
            # Deferred, not failed: the operator can still paste the key.
            assert rows[-1].status == state.DEFERRED
            # ...but it must SAY so. This park consumes no try and repeats every
            # 15 minutes indefinitely, so with no reason on the row the board
            # showed nothing but "Scheduled, next 12:38" while nothing was ever
            # going to be sent. Silence here is what made 2026-08-17 invisible.
            assert rows[-1].error_code == "no_credential"
            assert "no API key" in rows[-1].error_message
            assert fake.submissions == []
        run(go())

    def test_a_rejected_key_parks_with_a_reason_that_names_the_rejection(self):
        # Same park, different cause, and the advice has to differ: "add a key"
        # is wrong when a key is sitting there rejected.
        async def go():
            from datetime import datetime, timezone
            gid, dest_ids = await _group(name="rejected-key",
                                         platforms=("youtube",))
            async with pdb.session() as s:
                cred = (await s.execute(select(m.PublishCredential).where(
                    m.PublishCredential.publish_group_id == gid
                ))).scalar_one()
                cred.invalid_at = datetime.now(timezone.utc)
                cred.invalid_reason = "provider said: key revoked"
                await s.commit()

            req_id, _ = await _publish("job-x2", 0, dest_ids)
            assert await _drain(rounds=1) == ["no_credential"]
            rows = await _attempts(req_id)
            assert rows[-1].status == state.DEFERRED
            assert "rejected" in rows[-1].error_message
            assert "key revoked" in rows[-1].error_message
            assert fake.submissions == []
        run(go())


# --- posting rhythms + remote scheduling ------------------------------------
def _remote_schedule_capability(monkeypatch, value):
    """Override the fake's declared remote-schedule support for one test.

    The fake mirrors Status 200, which declares it True. Setting False here is
    how the "provider cannot hold the clock" branch gets covered without
    inventing a second fake provider.
    """
    import dataclasses
    monkeypatch.setattr(
        fake.FakeProvider, "capabilities",
        dataclasses.replace(fake.FakeProvider.capabilities,
                            supports_remote_schedule=value))


class TestRhythmAndRemoteSchedule:
    PLAN = {"mode": "rhythm", "start_time": "06:00", "interval_hours": 6,
            "max_per_day": 2, "timezone": "UTC"}

    async def _rhythm_group(self, name="rhythm-1", platforms=("youtube",)):
        gid, dest_ids = await _group(name=name, platforms=platforms)
        async with pdb.session() as s:
            g = await s.get(m.PublishGroup, gid)
            g.settings = {"plan": dict(self.PLAN)}
            await s.commit()
        return gid, dest_ids

    def test_rhythm_plan_earmarks_then_slots_then_converts(self):
        """The whole autonomous path: plan_job with schedule=rhythm creates
        ASSIGNMENTS (not requests), the assigner places them on the group's
        grid without exceeding the daily cap, and the scheduler converts them
        to requests carrying the slot — which remote scheduling then submits
        early with the timestamp."""
        async def go():
            from datetime import timezone as tz
            gid, _ = await self._rhythm_group()
            for i in range(3):
                CLIPS[("job-rhythm", i)] = clip()
            plan = {"group_ids": [str(gid)], "schedule": "rhythm"}

            async with pdb.session() as s:
                async with s.begin():
                    report = await planner.plan_job(
                        s, job_id="job-rhythm", clip_count=3, plan=plan,
                        actor="test")
            assert len(report["created"]) == 3
            assert report["mode"] == "rhythm"

            async with pdb.session() as s:
                async with s.begin():
                    assigned = await planner.assign_rhythm_slots(s)
            assert assigned == 3

            async with pdb.session() as s:
                rows = (await s.execute(
                    select(m.PublishAssignment).where(
                        m.PublishAssignment.publish_group_id == gid)
                    .order_by(m.PublishAssignment.scheduled_for))).scalars().all()
            slots = [r.scheduled_for for r in rows]
            assert all(t is not None for t in slots)
            assert slots == sorted(slots)
            # On the rhythm's interval, never closer — a larger gap means the
            # daily cap pushed the next slot to a later grid position.
            gaps = {(b - a).total_seconds() for a, b in zip(slots, slots[1:])}
            assert all(g >= 6 * 3600 for g in gaps)
            assert all(g % (6 * 3600) == 0 for g in gaps)
            per_day = {}
            for t in slots:
                day = t.astimezone(tz.utc).date()
                per_day[day] = per_day.get(day, 0) + 1
            assert max(per_day.values()) <= 2

            async with pdb.session() as s:
                async with s.begin():
                    converted = await planner.run_due_assignments(s)
            assert converted == 3
            async with pdb.session() as s:
                statuses = {r.status for r in (await s.execute(
                    select(m.PublishAssignment).where(
                        m.PublishAssignment.publish_group_id == gid))
                ).scalars().all()}
            assert statuses == {"requested"}
        run(go())

    def test_second_rhythm_run_books_around_the_first(self):
        """Batch-wide coordination: a second autopilot run must land on FREE
        slots, not collide with the first — the exact failure mode of planning
        each job independently."""
        async def go():
            gid, _ = await self._rhythm_group(name="rhythm-2")
            for run_n in ("first", "second"):
                for i in range(2):
                    CLIPS[(f"job-{run_n}", i)] = clip()
                async with pdb.session() as s:
                    async with s.begin():
                        await planner.plan_job(
                            s, job_id=f"job-{run_n}", clip_count=2,
                            plan={"group_ids": [str(gid)],
                                  "schedule": "rhythm"}, actor="test")
                async with pdb.session() as s:
                    async with s.begin():
                        await planner.assign_rhythm_slots(s)
            async with pdb.session() as s:
                slots = (await s.execute(
                    select(m.PublishAssignment.scheduled_for).where(
                        m.PublishAssignment.publish_group_id == gid)
                )).scalars().all()
            assert len(slots) == 4
            assert len(set(slots)) == 4, "no two clips may share a slot"
        run(go())

    def test_future_slot_is_promoted_and_submitted_with_the_timestamp(
            self, monkeypatch):
        # Opts in: no shipped provider honours a timestamp (Status 200 accepts
        # and discards it, measured 2026-08-19), so the orchestration this test
        # covers is dormant until a provider that does honour one arrives. The
        # machinery is still worth testing — it is provider-agnostic.
        _remote_schedule_capability(monkeypatch, True)
        async def go():
            from datetime import datetime, timedelta, timezone
            _, dest_ids = await _group(name="remote-1", platforms=("youtube",))
            when = datetime.now(timezone.utc) + timedelta(hours=6)
            CLIPS[("job-remote", 0)] = clip()
            async with pdb.session() as s:
                dests = await service.expand_destinations(s,
                                                          destination_ids=dest_ids)
                req = await service.create_request(
                    s, job_id="job-remote", clip_index=0, destinations=dests,
                    payload={"caption": "a normal clip"},
                    scheduled_for=when, mode="scheduled",
                    content_fingerprint=CLIPS[("job-remote", 0)]["fingerprint"])
                await s.commit()

                async with s.begin():
                    promoted = await dispatcher.promote_remote_schedules(s)
            assert promoted == 1
            rows = await _attempts(req.id)
            assert rows[0].deferred_until is None, "released for early submit"

            outcomes = await _drain()
            # The fake parks a remote-scheduled post as submitted, live at the
            # appointed time — never 'succeeded' on the spot.
            assert outcomes == ["submitted"]
            assert fake.submissions[0]["scheduled_for"] == when.isoformat()
            rows = await _attempts(req.id)
            assert rows[-1].status == state.SUBMITTED
            assert rows[-1].provider_post_ref
        run(go())

    def test_field_rejection_falls_back_to_the_local_clock(self, monkeypatch):
        _remote_schedule_capability(monkeypatch, True)
        async def go():
            from datetime import datetime, timedelta, timezone
            _, dest_ids = await _group(name="remote-2", platforms=("youtube",))
            when = datetime.now(timezone.utc) + timedelta(hours=6)
            CLIPS[("job-fallback", 0)] = clip(caption="fail-schedule case")
            async with pdb.session() as s:
                dests = await service.expand_destinations(s,
                                                          destination_ids=dest_ids)
                req = await service.create_request(
                    s, job_id="job-fallback", clip_index=0,
                    destinations=dests,
                    payload={"caption": "fail-schedule case"},
                    scheduled_for=when, mode="scheduled",
                    content_fingerprint=CLIPS[("job-fallback", 0)]["fingerprint"])
                await s.commit()
                async with s.begin():
                    await dispatcher.promote_remote_schedules(s)

            outcomes = await _drain(rounds=1)
            assert outcomes == ["remote_fallback"]
            rows = await _attempts(req.id)
            # Parked on the SLOT, not failed: the local clock takes over and
            # dispatch resubmits (without the field) when the slot arrives.
            assert rows[-1].status == state.DEFERRED
            assert rows[-1].deferred_until is not None
            assert rows[-1].deferred_until >= when - timedelta(minutes=1)
            # The field is off for the process, so nothing else is promoted.
            from publishing.providers import fake as fake_mod
            assert fake_mod.remote_schedule_available() is False
            async with pdb.session() as s:
                async with s.begin():
                    again = await dispatcher.promote_remote_schedules(s)
            assert again == 0
        run(go())

    def test_promotion_respects_the_off_switch(self, monkeypatch):
        # Opt in to the capability, or this asserts nothing: with the shipped
        # (False) capability, promoted == 0 for a reason that has nothing to do
        # with the off switch this test is named after.
        _remote_schedule_capability(monkeypatch, True)

        async def go():
            from datetime import datetime, timedelta, timezone
            monkeypatch.setenv("PUBLISHING_REMOTE_SCHEDULE", "off")
            _, dest_ids = await _group(name="remote-3", platforms=("youtube",))
            when = datetime.now(timezone.utc) + timedelta(hours=6)
            CLIPS[("job-off", 0)] = clip()
            async with pdb.session() as s:
                dests = await service.expand_destinations(s,
                                                          destination_ids=dest_ids)
                await service.create_request(
                    s, job_id="job-off", clip_index=0, destinations=dests,
                    payload={"caption": "a normal clip"}, scheduled_for=when,
                    mode="scheduled",
                    content_fingerprint=CLIPS[("job-off", 0)]["fingerprint"])
                await s.commit()
                async with s.begin():
                    promoted = await dispatcher.promote_remote_schedules(s)
            assert promoted == 0
        run(go())

    def test_a_provider_that_cannot_schedule_keeps_the_local_clock(
            self, monkeypatch):
        """The fallback shape, and the bug this test exists to prevent.

        With `supports_remote_schedule=False` and the mode left at `auto`,
        nothing may be released early: the attempt has to stay parked on its own
        slot so THIS machine submits when the slot arrives. When the promote pass
        and the payload build disagreed about the capability, the attempt was
        released hours early and then submitted with no timestamp at all —
        published immediately, spacing gone.
        """
        _remote_schedule_capability(monkeypatch, False)

        async def go():
            from datetime import datetime, timedelta, timezone
            _, dest_ids = await _group(name="remote-4", platforms=("youtube",))
            when = datetime.now(timezone.utc) + timedelta(hours=6)
            CLIPS[("job-local", 0)] = clip()
            async with pdb.session() as s:
                dests = await service.expand_destinations(s,
                                                          destination_ids=dest_ids)
                req = await service.create_request(
                    s, job_id="job-local", clip_index=0, destinations=dests,
                    payload={"caption": "a normal clip"}, scheduled_for=when,
                    mode="scheduled",
                    content_fingerprint=CLIPS[("job-local", 0)]["fingerprint"])
                await s.commit()
                async with s.begin():
                    promoted = await dispatcher.promote_remote_schedules(s)
            assert promoted == 0
            rows = await _attempts(req.id)
            assert rows[0].deferred_until is not None, "still parked"
            assert rows[0].deferred_until >= when - timedelta(minutes=1), \
                "parked on its own slot, not released"
            # And nothing is claimable, so a dispatch tick cannot post it early.
            before = len(fake.submissions)
            await _drain(rounds=1)
            assert len(fake.submissions) == before
        run(go())

    def test_media_registration_paces_transfers(self, monkeypatch):
        """The congestion fix from 2026-08-15: two registrations cannot start
        closer together than the configured gap, whatever else is happening."""
        async def go():
            import time as _time
            monkeypatch.setenv("PUBLISHING_MEDIA_REGISTRATION_GAP", "1")
            dispatcher._last_media_registration = 0.0
            async def one_registration():
                await dispatcher._media_pacer()
                try:
                    await asyncio.sleep(0)
                finally:
                    dispatcher._media_pacer_done()
            started = _time.monotonic()
            await one_registration()
            await one_registration()
            elapsed = _time.monotonic() - started
            assert elapsed >= 0.9, f"gap not enforced ({elapsed:.2f}s)"
        run(go())

