"""Pure-logic tests for the publishing state machine, retry policy and signing.

These import nothing outside the standard library (via publishing.state /
errors / signing), so they run in CI, which has no Postgres and no provider
credentials. The rules that decide whether a real post goes out are exactly the
rules that must be verifiable without a network.
"""
import time

import pytest

from publishing import errors, signing, state


class TestAttemptTransitions:
    def test_happy_path(self):
        for src, dst in [
            (state.PENDING, state.IN_FLIGHT),
            (state.IN_FLIGHT, state.SUBMITTED),
            (state.SUBMITTED, state.SUCCEEDED),
        ]:
            state.assert_transition(src, dst)

    def test_terminal_states_are_dead_ends(self):
        for terminal in (state.SUCCEEDED, state.DEAD, state.BLOCKED,
                         state.UNKNOWN, state.SKIPPED, state.CANCELLED):
            assert state.is_terminal(terminal)
            with pytest.raises(ValueError):
                state.assert_transition(terminal, state.PENDING)

    def test_unknown_can_never_be_retried(self):
        # The whole point of `unknown`: the post may already be live, so no code
        # path may walk it back to pending and submit again.
        for dst in state.ATTEMPT_STATES:
            if dst == state.UNKNOWN:
                continue
            assert not state.can_transition(state.UNKNOWN, dst)

    def test_submitted_cannot_be_deferred(self):
        # Deferring a submitted post would re-dispatch something the provider
        # already has.
        assert not state.can_transition(state.SUBMITTED, state.DEFERRED)

    def test_same_state_is_a_noop(self):
        state.assert_transition(state.PENDING, state.PENDING)

    def test_unknown_state_name_rejected(self):
        with pytest.raises(ValueError):
            state.assert_transition(state.PENDING, "publishing")

    def test_live_states_match_the_db_partial_index(self):
        # models.py's uq_attempt_live_per_destination lists these four. If the
        # two ever drift, the DB would allow a second in-flight attempt for one
        # destination — i.e. a double post.
        assert state.LIVE_STATES == frozenset({
            state.PENDING, state.IN_FLIGHT, state.SUBMITTED, state.SUCCEEDED,
        })


class TestDerivedRequestStatus:
    def test_no_attempts_is_pending(self):
        assert state.derive_request_status([]) == state.REQ_PENDING

    def test_all_succeeded(self):
        assert state.derive_request_status(
            [state.SUCCEEDED] * 3) == state.REQ_SUCCEEDED

    def test_partial_is_not_collapsed_either_way(self):
        # One clip to 3 accounts, TikTok fails. This must be `partial`: reporting
        # `failed` hides two live posts, `succeeded` hides the one that needs a
        # human.
        got = state.derive_request_status(
            [state.SUCCEEDED, state.SUCCEEDED, state.DEAD])
        assert got == state.REQ_PARTIAL

    def test_all_failed(self):
        assert state.derive_request_status(
            [state.DEAD, state.BLOCKED]) == state.REQ_FAILED

    def test_in_flight_dominates(self):
        assert state.derive_request_status(
            [state.SUCCEEDED, state.IN_FLIGHT]) == state.REQ_IN_PROGRESS

    def test_deferred_only(self):
        assert state.derive_request_status(
            [state.DEFERRED, state.DEFERRED]) == state.REQ_DEFERRED

    def test_deferred_with_a_win_is_in_progress(self):
        assert state.derive_request_status(
            [state.SUCCEEDED, state.DEFERRED]) == state.REQ_IN_PROGRESS

    def test_skipped_never_makes_a_request_partial(self):
        assert state.derive_request_status(
            [state.SUCCEEDED, state.SKIPPED]) == state.REQ_SUCCEEDED

    def test_all_skipped_is_success(self):
        assert state.derive_request_status(
            [state.SKIPPED, state.SKIPPED]) == state.REQ_SUCCEEDED

    def test_all_cancelled(self):
        assert state.derive_request_status(
            [state.CANCELLED]) == state.REQ_CANCELLED

    def test_unknown_counts_as_needing_attention_not_success(self):
        assert state.derive_request_status(
            [state.SUCCEEDED, state.UNKNOWN]) == state.REQ_PARTIAL


class TestBackoff:
    def test_grows_exponentially(self):
        d1 = state.backoff_seconds(1, base=60, cap=3600)
        d2 = state.backoff_seconds(2, base=60, cap=3600)
        d3 = state.backoff_seconds(3, base=60, cap=3600)
        assert d1 < d2 < d3

    def test_respects_the_cap(self):
        assert state.backoff_seconds(99, base=60, cap=3600) <= 3600

    def test_never_zero(self):
        assert state.backoff_seconds(1, base=1, cap=10) >= 1

    def test_jitter_is_deterministic_per_seed(self):
        a = state.backoff_seconds(3, jitter_seed="attempt-abc")
        b = state.backoff_seconds(3, jitter_seed="attempt-abc")
        assert a == b

    def test_jitter_desynchronizes_different_attempts(self):
        # 27 posts failing at once must not all retry in the same second.
        delays = {state.backoff_seconds(3, jitter_seed=f"a{i}")
                  for i in range(20)}
        assert len(delays) > 1

    def test_jitter_stays_within_20_percent(self):
        plain = state.backoff_seconds(4, base=60, cap=3600)
        for i in range(50):
            d = state.backoff_seconds(4, base=60, cap=3600,
                                      jitter_seed=f"seed{i}")
            assert 0.75 * plain <= d <= 1.25 * plain

    def test_should_retry_respects_max_attempts(self):
        assert state.should_retry(1, 5, True)
        assert not state.should_retry(5, 5, True)
        assert not state.should_retry(1, 5, False)


class TestIdempotency:
    def test_fingerprint_is_stable(self):
        a = state.content_fingerprint("job1", 0, 1024, 1000.0)
        b = state.content_fingerprint("job1", 0, 1024, 1000.9)
        assert a == b  # sub-second mtime noise must not change identity

    def test_fingerprint_changes_when_bytes_change(self):
        a = state.content_fingerprint("job1", 0, 1024, 1000.0)
        b = state.content_fingerprint("job1", 0, 2048, 1000.0)
        assert a != b  # a re-styled clip must not reuse the old media ref

    def test_fingerprint_separates_clips(self):
        assert (state.content_fingerprint("job1", 0)
                != state.content_fingerprint("job1", 1))

    def test_idempotency_key_ignores_destination_order(self):
        a = state.derive_idempotency_key("j", 1, ["d2", "d1"])
        b = state.derive_idempotency_key("j", 1, ["d1", "d2"])
        assert a == b  # a double-clicked button collapses into one request

    def test_idempotency_key_distinguishes_destination_sets(self):
        a = state.derive_idempotency_key("j", 1, ["d1"])
        b = state.derive_idempotency_key("j", 1, ["d1", "d2"])
        assert a != b  # publishing to more accounts is a different operation

    def test_idempotency_key_distinguishes_schedules(self):
        a = state.derive_idempotency_key("j", 1, ["d1"], "2026-08-10T09:00:00Z")
        b = state.derive_idempotency_key("j", 1, ["d1"], "2026-08-11T09:00:00Z")
        assert a != b


class TestErrorClassification:
    def test_permanent_errors_are_not_retried(self):
        for code in errors.PERMANENT:
            assert not errors.is_retryable(code)

    def test_transient_and_capacity_are_retried(self):
        for code in list(errors.TRANSIENT) + list(errors.CAPACITY):
            assert errors.is_retryable(code)

    def test_ambiguous_is_never_retryable(self):
        err = errors.ProviderError(errors.E_UNKNOWN, "no response")
        assert err.is_ambiguous
        assert not err.retryable

    def test_quota_is_capacity_not_failure(self):
        err = errors.ProviderError(errors.E_QUOTA_EXHAUSTED, defer_seconds=3600)
        assert err.is_capacity and err.retryable
        assert err.defer_seconds == 3600

    def test_auth_marks_the_credential_not_the_post(self):
        assert errors.E_AUTH in errors.CREDENTIAL_FATAL
        assert not errors.ProviderError(errors.E_AUTH).retryable

    def test_not_connected_marks_the_destination(self):
        assert errors.E_NOT_CONNECTED in errors.DESTINATION_FATAL

    @pytest.mark.parametrize("status,expected", [
        (401, errors.E_AUTH),
        (403, errors.E_NOT_CONNECTED),
        (413, errors.E_MEDIA_TOO_LARGE),
        (422, errors.E_MEDIA_UNFETCHABLE),
        (429, errors.E_RATE_LIMITED),
        (400, errors.E_VALIDATION),
        (500, errors.E_PROVIDER_5XX),
        (503, errors.E_PROVIDER_5XX),
    ])
    def test_http_status_mapping(self, status, expected):
        assert errors.classify_http_status(status) == expected


class TestWebhookSignature:
    SECRET = "whsec_testing_only_not_a_real_secret"

    def test_round_trip(self):
        body = b'{"id":"evt_1","type":"post.published"}'
        sig = signing.compute_webhook_signature(self.SECRET, body)
        assert sig.startswith("sha256=")
        assert signing.verify_webhook_signature(self.SECRET, body, sig)

    def test_tampered_body_fails(self):
        body = b'{"id":"evt_1","type":"post.published"}'
        sig = signing.compute_webhook_signature(self.SECRET, body)
        assert not signing.verify_webhook_signature(
            self.SECRET, body + b" ", sig)

    def test_wrong_secret_fails(self):
        body = b'{"id":"evt_1"}'
        sig = signing.compute_webhook_signature("other", body)
        assert not signing.verify_webhook_signature(self.SECRET, body, sig)

    def test_bare_hex_accepted(self):
        body = b'{"id":"evt_1"}'
        sig = signing.compute_webhook_signature(self.SECRET, body)
        assert signing.verify_webhook_signature(
            self.SECRET, body, sig[len("sha256="):])

    def test_missing_signature_fails(self):
        assert not signing.verify_webhook_signature(self.SECRET, b"{}", None)
        assert not signing.verify_webhook_signature(self.SECRET, b"{}", "")

    def test_no_secret_fails_closed(self):
        assert not signing.verify_webhook_signature("", b"{}", "sha256=abc")

    def test_skew_window(self):
        now = 1_000_000.0
        assert signing.within_skew(now, 900, now=now)
        assert signing.within_skew(now - 899, 900, now=now)
        assert not signing.within_skew(now - 901, 900, now=now)
        # Future-dated beyond tolerance is equally rejected.
        assert not signing.within_skew(now + 901, 900, now=now)

    def test_missing_timestamp_is_allowed(self):
        # Gated by the signature + provider_event_id UNIQUE instead.
        assert signing.within_skew(None, 900)


class TestMediaTokens:
    SECRET = "media_signing_secret_for_tests_only"

    def test_round_trip(self):
        exp = int(time.time()) + 600
        tok = signing.sign_media_token(self.SECRET, "job1", 2, "clip_2.mp4", exp)
        ok, payload, reason = signing.verify_media_token(self.SECRET, tok)
        assert ok and reason == ""
        assert payload["j"] == "job1"
        assert payload["c"] == 2
        assert payload["f"] == "clip_2.mp4"

    def test_expired_token_rejected(self):
        exp = int(time.time()) - 1
        tok = signing.sign_media_token(self.SECRET, "job1", 0, "c.mp4", exp)
        ok, _, reason = signing.verify_media_token(self.SECRET, tok)
        assert not ok and reason == "expired"

    def test_tampered_filename_rejected(self):
        # The filename is inside the signed payload, so it cannot be swapped for
        # another path — this is what makes the token a capability for one clip.
        exp = int(time.time()) + 600
        tok = signing.sign_media_token(self.SECRET, "job1", 0,
                                       "clip_0.mp4", exp)
        body, sig = tok.rsplit(".", 1)
        forged = signing.sign_media_token(self.SECRET, "job1", 0,
                                          "../../.env", exp)
        forged_body = forged.rsplit(".", 1)[0]
        ok, _, reason = signing.verify_media_token(
            self.SECRET, f"{forged_body}.{sig}")
        assert not ok and reason == "bad_signature"

    def test_wrong_secret_rejected(self):
        exp = int(time.time()) + 600
        tok = signing.sign_media_token("other-secret", "j", 0, "c.mp4", exp)
        ok, _, reason = signing.verify_media_token(self.SECRET, tok)
        assert not ok and reason == "bad_signature"

    def test_malformed_never_raises(self):
        for bad in ("", "nodot", "a.b.c", "!!!.???"):
            ok, _, _ = signing.verify_media_token(self.SECRET, bad)
            assert not ok
