"""Status polling: when a submitted post may be asked about, and what an answer
is allowed to change.

Polling exists because until now a lost callback had exactly one ending. The only
provider had no status endpoint, so a post whose webhook never arrived was aged
into ``unknown`` by the stale sweeper after 30 minutes and a human had to open the
account and look. Zernio declares ``supports_status_lookup``, so that post can
simply be asked about.

Two things are pinned here, and both fail quietly when wrong:

  * the cadence (``TestPollIsDue``). It is the entire rate limit on traffic aimed
    at a provider's status endpoint, it lives in a SQL ``WHERE`` that CI cannot
    execute, and getting it wrong produces a request flood rather than an error —
    hence ``state.poll_is_due`` as the pure authority the dispatcher re-checks.
  * what an answer may do to an attempt (``TestPollOutcomes``). A status lookup
    must never be able to invent a state transition, and above all must never
    resolve an ambiguous answer into a definite one: a post that may be live and
    gets retried is a double publish to a real audience.

Everything runs with a stub session and a stub provider — no Postgres, no
credential, no network, so it runs in CI alongside the other pure suites.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

from publishing import dispatcher, errors, state  # noqa: E402
from publishing.errors import ProviderError  # noqa: E402
from publishing.providers.base import SubmitResult  # noqa: E402

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
MIN_AGE = 120
INTERVAL = 300

GROUP = "7c9f1e2a-0000-4000-8000-0000000000aa"
REQUEST = "7c9f1e2a-0000-4000-8000-0000000000bb"
DEST = "7c9f1e2a-0000-4000-8000-0000000000cc"
ATTEMPT = "7c9f1e2a-0000-4000-8000-0000000000dd"
POST_REF = "post_abc123"


def run(coro):
    return asyncio.run(coro)


def due(**over):
    """Arguments to ``poll_is_due`` for a post that is comfortably pollable."""
    args = {
        "submitted_at": NOW - timedelta(seconds=MIN_AGE * 3),
        "last_polled_at": None,
        "deferred_until": None,
        "min_age_seconds": MIN_AGE,
        "interval_seconds": INTERVAL,
    }
    args.update(over)
    return args


# --- Cadence ----------------------------------------------------------------
class TestPollIsDue:
    def test_a_settled_unpolled_post_is_due(self):
        assert state.poll_is_due(NOW, **due()) is True

    def test_a_post_younger_than_the_floor_is_not(self):
        """The floor is not politeness, it is correctness of cost.

        A poll at t+2s spends a request to be told exactly what the submit
        response already said, while the provider is still working on the post.
        """
        assert state.poll_is_due(
            NOW, **due(submitted_at=NOW - timedelta(seconds=MIN_AGE - 1))) is False

    def test_the_floor_is_inclusive(self):
        assert state.poll_is_due(
            NOW, **due(submitted_at=NOW - timedelta(seconds=MIN_AGE))) is True

    def test_a_recently_polled_post_is_not_due(self):
        """The interval is the whole rate limit.

        Reconciliation runs every 60s. Without this gate a post pending for an
        hour costs 60 requests instead of 12, per post.
        """
        assert state.poll_is_due(
            NOW, **due(last_polled_at=NOW - timedelta(seconds=INTERVAL - 1))) \
            is False

    def test_the_interval_is_inclusive(self):
        assert state.poll_is_due(
            NOW, **due(last_polled_at=NOW - timedelta(seconds=INTERVAL))) is True

    def test_never_polled_beats_the_interval(self):
        assert state.poll_is_due(NOW, **due(last_polled_at=None)) is True

    def test_a_provider_parked_window_is_left_alone(self):
        """A future `deferred_until` on a SUBMITTED row is the provider's own
        window (a daily-cap 202). Silence before it is expected, not suspicious —
        the same reading `sweep_stale_submitted` takes of the same field."""
        assert state.poll_is_due(
            NOW, **due(deferred_until=NOW + timedelta(hours=2))) is False

    def test_a_past_window_does_not_block(self):
        assert state.poll_is_due(
            NOW, **due(deferred_until=NOW - timedelta(seconds=1))) is True

    def test_a_post_never_submitted_is_never_due(self):
        # No submit timestamp means nothing was handed over that we know of, so
        # there is nothing to ask about and no clock to measure the floor against.
        assert state.poll_is_due(NOW, **due(submitted_at=None)) is False

    def test_naive_timestamps_do_not_raise(self):
        """A naive value must not take the reconcile loop down.

        Comparing naive to aware raises TypeError, and this runs inside the
        reconciliation pass — one bad row would stop every other post from being
        polled, swept or scheduled that tick.
        """
        assert state.poll_is_due(
            NOW, **due(submitted_at=datetime(2026, 8, 24, 11, 0, 0),
                       last_polled_at=datetime(2026, 8, 24, 11, 30, 0))) is True

    def test_a_zero_interval_polls_every_pass(self):
        # The knob bottoms out rather than misbehaving: an operator debugging a
        # stuck post can set the interval to 0 and get a poll every tick.
        assert state.poll_is_due(
            NOW, **due(last_polled_at=NOW, interval_seconds=0)) is True


# --- Stubs ------------------------------------------------------------------
class StubProvider:
    """Minimal adapter: only what the poller reaches for."""

    def __init__(self, *, pollable=True, answer=None, raises=None):
        self.capabilities = SimpleNamespace(supports_status_lookup=pollable)
        self._answer = answer
        self._raises = raises
        self.asked = []

    async def fetch_status(self, api_key, provider_post_ref):
        self.asked.append((api_key, provider_post_ref))
        if self._raises is not None:
            raise self._raises
        return self._answer


class StubSession:
    """Stands in for the async session over the one query the poller makes."""

    def __init__(self, attempts, destination):
        self._attempts = attempts
        self._destination = destination
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        rows = list(self._attempts)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, _model, ident):
        return self._destination if str(ident) == str(DEST) else None


def make_attempt(**over):
    row = SimpleNamespace(
        id=ATTEMPT,
        publish_request_id=REQUEST,
        publish_destination_id=DEST,
        publish_group_id=GROUP,
        provider="zernio",
        platform="tiktok",
        attempt_number=1,
        status=state.SUBMITTED,
        provider_post_ref=POST_REF,
        provider_native_post_ref=None,
        permalink=None,
        submitted_at=NOW - timedelta(seconds=MIN_AGE * 3),
        last_polled_at=None,
        deferred_until=None,
        completed_at=None,
        error_code=None,
        error_message=None,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def make_destination(**over):
    row = SimpleNamespace(
        id=DEST,
        publish_group_id=GROUP,
        provider="zernio",
        platform="tiktok",
        credential_slot="account-1",
        health="ok",
        health_detail="",
        verified_at=None,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


@pytest.fixture
def harness(monkeypatch):
    """Wire the poller to stubs and capture what it did.

    Everything faked here is already covered elsewhere — credential resolution in
    the e2e suite, decryption in the crypto suite, ``record_failure`` in the state
    and e2e suites. What is under test is only which of them the poller calls.
    """
    calls = {"failures": [], "events": [], "refreshed": []}
    credential = SimpleNamespace(id="cred-1", key_version=1, nonce_b64="n",
                                 ciphertext_b64="c", aad="a")

    async def _credential_for_destination(session, dest, **kw):
        return credential

    def _decrypt(_blob):
        return SimpleNamespace(reveal=lambda: "zr_test_key")

    async def _record_failure(session, attempt, err, credential_id=None):
        calls["failures"].append((attempt, err, credential_id))
        return None

    async def _log_event(session, kind, **kw):
        calls["events"].append((kind, kw.get("message", "")))

    async def _refresh(session, request_id):
        calls["refreshed"].append(request_id)

    monkeypatch.setattr(dispatcher.service, "credential_for_destination",
                        _credential_for_destination)
    monkeypatch.setattr(dispatcher.crypto, "decrypt", _decrypt)
    monkeypatch.setattr(dispatcher.service, "record_failure", _record_failure)
    monkeypatch.setattr(dispatcher.service, "log_event", _log_event)
    monkeypatch.setattr(dispatcher.service, "refresh_request_status", _refresh)
    monkeypatch.setattr(dispatcher, "_now", lambda: NOW)
    monkeypatch.setenv("PUBLISHING_STATUS_POLL", "true")
    monkeypatch.setenv("PUBLISHING_STATUS_POLL_MIN_AGE", str(MIN_AGE))
    monkeypatch.setenv("PUBLISHING_STATUS_POLL_INTERVAL", str(INTERVAL))
    return SimpleNamespace(calls=calls, credential=credential,
                           monkeypatch=monkeypatch)


def poll(harness, provider, attempts, destination=None):
    dest = destination if destination is not None else make_destination()
    harness.monkeypatch.setattr(dispatcher.providers, "get",
                                lambda _name: provider)
    session = StubSession(attempts, dest)
    count = run(dispatcher.poll_submitted_attempts(session))
    return count, session, dest


# --- What an answer may change ----------------------------------------------
class TestPollOutcomes:
    def test_a_published_post_is_resolved(self, harness):
        attempt = make_attempt()
        provider = StubProvider(answer=SubmitResult(
            status="succeeded", provider_post_ref=POST_REF,
            provider_native_post_ref="tt_999",
            permalink="https://example.invalid/p/999"))

        count, _, _ = poll(harness, provider, [attempt])

        assert count == 1
        assert provider.asked == [("zr_test_key", POST_REF)]
        assert attempt.status == state.SUCCEEDED
        assert attempt.completed_at == NOW
        assert attempt.provider_native_post_ref == "tt_999"
        assert attempt.permalink == "https://example.invalid/p/999"
        assert ("attempt.succeeded", "confirmed by status poll") \
            in harness.calls["events"]
        assert harness.calls["refreshed"] == [REQUEST]

    def test_resolving_does_not_rewrite_submitted_at(self, harness):
        """`submitted_at` is when the post was handed over, not when we asked.

        It is the timestamp an operator uses to reconstruct a lost callback, and
        the one `record_success` would clobber — which is why the poller applies
        the confirmation the way the webhook handler does instead of calling it.
        """
        submitted = NOW - timedelta(minutes=20)
        attempt = make_attempt(submitted_at=submitted)
        poll(harness, StubProvider(answer=SubmitResult(
            status="succeeded", provider_post_ref=POST_REF)), [attempt])
        assert attempt.submitted_at == submitted

    def test_a_confirmed_publish_verifies_an_unverified_destination(self, harness):
        # Same reasoning as the submit path: there is no listing or dry-run
        # endpoint, so a publish that demonstrably landed IS the verification.
        attempt = make_attempt()
        dest = make_destination(health="unverified")
        poll(harness, StubProvider(answer=SubmitResult(
            status="succeeded", provider_post_ref=POST_REF)), [attempt], dest)
        assert dest.health == "ok"
        assert dest.verified_at == NOW

    def test_a_still_pending_post_is_only_stamped(self, harness):
        attempt = make_attempt()
        count, _, _ = poll(harness, StubProvider(answer=SubmitResult(
            status="submitted", provider_post_ref=POST_REF)), [attempt])
        assert count == 1
        assert attempt.status == state.SUBMITTED
        assert attempt.last_polled_at == NOW
        assert harness.calls["failures"] == []

    def test_no_information_is_not_failure(self, harness):
        """`None` covers an unreachable provider AND a 404.

        A post deleted from the provider's dashboard and a ref that was never
        valid look identical, and neither is evidence the video is not live on the
        platform. Treating it as failure would retry a live post.
        """
        attempt = make_attempt()
        count, _, _ = poll(harness, StubProvider(answer=None), [attempt])
        assert count == 1
        assert attempt.status == state.SUBMITTED
        assert attempt.last_polled_at == NOW
        assert harness.calls["failures"] == []

    def test_a_definite_failure_goes_through_record_failure(self, harness):
        """A lookup reporting the post as failed is real news about the post.

        Routed through the same helper the submit path uses, so the retry budget,
        backoff and destination-fatal handling are the ones already tested rather
        than a second implementation.
        """
        attempt = make_attempt()
        err = ProviderError(errors.E_VALIDATION, "caption rejected",
                            provider_post_ref=POST_REF)
        count, _, _ = poll(harness, StubProvider(raises=err), [attempt])

        assert count == 1
        assert len(harness.calls["failures"]) == 1
        recorded, seen_err, cred_id = harness.calls["failures"][0]
        assert recorded is attempt
        assert seen_err is err
        assert cred_id == "cred-1"

    def test_an_ambiguous_answer_never_becomes_a_failure(self, harness):
        """The one that must not regress.

        E_UNKNOWN means the provider does not know whether the post went out.
        Recording it as a failure would let `record_failure` schedule a retry on a
        post that may be live — a second video on a real audience's feed. It stays
        submitted, and the sweeper's `unknown` remains its ending.
        """
        attempt = make_attempt()
        count, _, _ = poll(harness, StubProvider(raises=ProviderError(
            errors.E_UNKNOWN, "provider cannot say")), [attempt])

        assert count == 1
        assert attempt.status == state.SUBMITTED
        assert harness.calls["failures"] == []
        assert attempt.last_polled_at == NOW

    def test_an_unclassified_exception_is_swallowed(self, harness):
        """One broken row must not abort the pass.

        The poller runs inside reconciliation; an exception escaping here would
        roll back the transaction and skip every other post's poll that tick.
        """
        attempt = make_attempt()
        count, _, _ = poll(harness, StubProvider(
            raises=RuntimeError("adapter bug")), [attempt])
        assert count == 1
        assert attempt.status == state.SUBMITTED
        assert attempt.last_polled_at == NOW
        assert harness.calls["failures"] == []

    def test_the_stamp_lands_even_when_the_lookup_errors(self, harness):
        """Stamped before the call, not after.

        A provider 500ing every status lookup must still be rate-limited. Stamping
        afterwards would leave `last_polled_at` NULL on every failure, and the same
        post would be re-asked on every 60s tick forever.
        """
        attempt = make_attempt()
        poll(harness, StubProvider(raises=ProviderError(
            errors.E_PROVIDER_5XX, "status endpoint down",
            provider_post_ref=POST_REF)), [attempt])
        assert attempt.last_polled_at == NOW


# --- Who gets polled at all -------------------------------------------------
class TestPollScope:
    def test_a_provider_without_the_capability_is_never_asked(self, harness):
        """Status 200's whole situation: no status endpoint.

        It must not be asked, and it must not be stamped either — a stamp would
        make the row look freshly polled if the capability ever appeared.
        """
        attempt = make_attempt(provider="status200")
        provider = StubProvider(pollable=False, answer=SubmitResult(
            status="succeeded", provider_post_ref=POST_REF))

        count, _, _ = poll(harness, provider, [attempt])

        assert count == 0
        assert provider.asked == []
        assert attempt.last_polled_at is None
        assert attempt.status == state.SUBMITTED

    def test_the_off_switch_does_no_database_work(self, harness):
        harness.monkeypatch.setenv("PUBLISHING_STATUS_POLL", "0")
        provider = StubProvider(answer=None)
        harness.monkeypatch.setattr(dispatcher.providers, "get",
                                    lambda _name: provider)
        session = StubSession([make_attempt()], make_destination())

        assert run(dispatcher.poll_submitted_attempts(session)) == 0
        assert session.executed == 0
        assert provider.asked == []

    def test_a_row_the_query_should_not_have_returned_is_re_checked(self, harness):
        """The Python guard is the authority, the SQL is the pre-filter.

        If the two ever drift the disagreement must cost a skipped poll, never an
        extra request — so a row inside its interval is dropped here even though
        the stub query handed it over.
        """
        attempt = make_attempt(last_polled_at=NOW - timedelta(seconds=5))
        provider = StubProvider(answer=SubmitResult(
            status="succeeded", provider_post_ref=POST_REF))

        count, _, _ = poll(harness, provider, [attempt])

        assert count == 0
        assert provider.asked == []
        assert attempt.status == state.SUBMITTED

    def test_a_missing_destination_is_skipped_not_stamped(self, harness):
        attempt = make_attempt(publish_destination_id="nonexistent")
        provider = StubProvider(answer=None)
        count, _, _ = poll(harness, provider, [attempt])
        assert count == 0
        assert provider.asked == []
        assert attempt.last_polled_at is None

    def test_no_usable_credential_is_skipped_not_stamped(self, harness):
        """Stamping here would make a fixed key wait out the interval.

        Dispatch's `no_credential` path already reports this on the row; the
        poller has nothing to add and should be ready the moment a key lands.
        """
        async def _none(session, dest, **kw):
            return None

        harness.monkeypatch.setattr(dispatcher.service,
                                    "credential_for_destination", _none)
        attempt = make_attempt()
        provider = StubProvider(answer=None)

        count, _, _ = poll(harness, provider, [attempt])

        assert count == 0
        assert provider.asked == []
        assert attempt.last_polled_at is None

    def test_an_unreadable_key_is_skipped_not_stamped(self, harness):
        def _boom(_blob):
            raise ValueError("master key changed")

        harness.monkeypatch.setattr(dispatcher.crypto, "decrypt", _boom)
        attempt = make_attempt()
        provider = StubProvider(answer=None)

        count, _, _ = poll(harness, provider, [attempt])

        assert count == 0
        assert provider.asked == []
        assert attempt.last_polled_at is None

    def test_an_unregistered_provider_is_skipped(self, harness):
        attempt = make_attempt(provider="gone")

        def _raise(_name):
            raise KeyError("unknown publishing provider 'gone'")

        harness.monkeypatch.setattr(dispatcher.providers, "get", _raise)
        session = StubSession([attempt], make_destination())

        assert run(dispatcher.poll_submitted_attempts(session)) == 0
        assert attempt.last_polled_at is None

    def test_several_posts_are_polled_in_one_pass(self, harness):
        """One multi-account group's fan-out resolves together.

        The batch is what makes a 3-platform post cost one pass instead of three
        reconciliation ticks.
        """
        attempts = [make_attempt(id=f"a{i}", provider_post_ref=f"post_{i}")
                    for i in range(3)]
        provider = StubProvider(answer=SubmitResult(
            status="submitted", provider_post_ref=POST_REF))

        count, _, _ = poll(harness, provider, attempts)

        assert count == 3
        assert [ref for _key, ref in provider.asked] == [
            "post_0", "post_1", "post_2"]
        assert all(a.last_polled_at == NOW for a in attempts)


# --- The transition the poller depends on -----------------------------------
class TestSubmittedMayBlock:
    def test_submitted_can_reach_blocked(self):
        """Added for the poller, but it fixes the webhook path too.

        A provider reports "this account needs re-linking" as the OUTCOME of a
        post it already accepted, so it arrives after the submit — via
        `post.failed` or via a poll. `record_failure` maps E_ACCOUNT_AUTH to
        BLOCKED, and while the state machine forbade that move the webhook drain
        caught the ValueError, wrote `process_error`, and swallowed the completion
        signal: the post aged into `unknown` with the destination still marked
        healthy, so the day's remaining posts each rediscovered the same dead
        token.
        """
        assert state.can_transition(state.SUBMITTED, state.BLOCKED)
        state.assert_transition(state.SUBMITTED, state.BLOCKED)

    def test_submitted_still_cannot_be_deferred_or_requeued(self):
        # Widening SUBMITTED must not open a path back to a re-dispatch: that
        # would hand the provider a post it already has.
        for dst in (state.PENDING, state.IN_FLIGHT, state.DEFERRED):
            assert not state.can_transition(state.SUBMITTED, dst)

    def test_blocked_is_still_terminal(self):
        assert state.is_terminal(state.BLOCKED)
        with pytest.raises(ValueError):
            state.assert_transition(state.BLOCKED, state.PENDING)
