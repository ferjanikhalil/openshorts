"""Inbound-callback verification: which header, whose verifier, whose group.

Two provider-specific decisions live in `webhooks.py` and neither may be
hardcoded — which header carries the signature, and how the digest is encoded.
Both fail *silently* when wrong: the callback is rejected as unsigned, no post is
ever confirmed, and each one ages into `unknown` for a human to resolve by hand.
Nothing raises, nothing appears in a diff, and the only symptom is that posts
stop completing. That is the class of bug adding a second provider introduces,
so it is pinned here.

The rest of the handler (persist-and-ack, replay guard, skew window, the drain
worker) needs real rows and is covered by the e2e suite. What is tested here is
the pure selection logic, with a stub session standing in for the credential
query — no Postgres, so it runs in CI.
"""
import base64
import hashlib
import hmac
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from starlette.datastructures import Headers  # noqa: E402

from publishing import crypto, signing  # noqa: E402
from publishing import webhooks  # noqa: E402
from publishing.providers import fake, status200, zernio  # noqa: E402

KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(reversed(range(32)))).decode()

SECRET = "whsec_testing_only_not_a_real_secret"
OTHER_SECRET = "whsec_a_second_zernio_account_secret"
BODY = b'{"id":"evt_1","event":"post.published","post":{"id":"p_abc"}}'

GROUP_A = "7c9f1e2a-0000-4000-8000-00000000000a"
GROUP_B = "7c9f1e2a-0000-4000-8000-00000000000b"


@pytest.fixture
def key_a(monkeypatch):
    monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_A)
    monkeypatch.delenv("PUBLISHING_MASTER_KEY_OLD", raising=False)


def request_with(**headers):
    """A stand-in for the FastAPI request, carrying only what is read.

    Starlette's own `Headers` is used rather than a dict: HTTP header lookup is
    case-insensitive, and a provider that sends `x-zernio-signature` in
    lowercase must be found. A plain dict would let a case bug pass here and
    reject every real callback.
    """
    return SimpleNamespace(headers=Headers(headers))


def hex_sig(secret=SECRET, body=BODY):
    return signing.compute_webhook_signature(secret, body)


def b64_sig(secret=SECRET, body=BODY):
    """The same digest, base64-encoded — the encoding Zernio does not document."""
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# --- Which header ------------------------------------------------------------
class TestPresentedSignatureHeader:
    def test_status200_reads_the_generic_header(self):
        # It declares no header of its own, so it inherits the default — and that
        # default is the header it has always signed. Unchanged.
        assert status200.CAPABILITIES.signature_header == \
            signing.WEBHOOK_SIGNATURE_HEADER
        value, name = webhooks._presented_signature(
            status200.PROVIDER, request_with(**{"X-Webhook-Signature": "sha256=aa"}))
        assert value == "sha256=aa"
        assert name == "X-Webhook-Signature"

    def test_zernio_reads_its_own_header(self):
        """The bug this exists to prevent.

        Zernio signs `X-Zernio-Signature`. Reading a fixed `X-Webhook-Signature`
        would find nothing on every single callback and 401 it as unsigned.
        """
        value, name = webhooks._presented_signature(
            zernio.PROVIDER, request_with(**{"X-Zernio-Signature": "deadbeef"}))
        assert value == "deadbeef"
        assert name == "X-Zernio-Signature"

    def test_the_lookup_is_case_insensitive(self):
        value, _ = webhooks._presented_signature(
            zernio.PROVIDER, request_with(**{"x-zernio-signature": "deadbeef"}))
        assert value == "deadbeef"

    def test_a_declared_header_provider_still_accepts_the_generic_one(self):
        # If Zernio quietly renames its header, this degrades to "still
        # verified" instead of "every callback rejected". Same secret, same
        # preimage — a fallback match is no weaker than a declared one.
        value, name = webhooks._presented_signature(
            zernio.PROVIDER, request_with(**{"X-Webhook-Signature": "deadbeef"}))
        assert value == "deadbeef"
        # ...but the name reported back is still the one we expect them to send,
        # because that name goes into the operator-facing rejection log.
        assert name == "X-Zernio-Signature"

    def test_the_declared_header_wins_when_both_are_present(self):
        value, _ = webhooks._presented_signature(
            zernio.PROVIDER, request_with(**{
                "X-Zernio-Signature": "the-real-one",
                "X-Webhook-Signature": "some-other-thing"}))
        assert value == "the-real-one"

    def test_a_missing_header_is_empty_string_not_none(self):
        # The handler does `bool(signature)` and puts the name in a log message;
        # None would read as "no header configured" in the wrong place.
        value, name = webhooks._presented_signature(
            zernio.PROVIDER, request_with(**{"X-Other": "x"}))
        assert value == ""
        assert name == "X-Zernio-Signature"

    def test_whitespace_is_stripped(self):
        value, _ = webhooks._presented_signature(
            status200.PROVIDER,
            request_with(**{"X-Webhook-Signature": "  sha256=aa\t"}))
        assert value == "sha256=aa"

    def test_a_whitespace_only_header_counts_as_absent(self):
        # Otherwise it would be presented as a signature, fail to verify, and be
        # logged as "did not match" instead of "the header never arrived".
        value, _ = webhooks._presented_signature(
            status200.PROVIDER, request_with(**{"X-Webhook-Signature": "   "}))
        assert value == ""

    def test_a_provider_declaring_nothing_falls_back_to_the_generic_name(self):
        # An adapter that forgets the field, or sets it empty, must not end up
        # looking for a header named "".
        blank = SimpleNamespace(capabilities=SimpleNamespace(signature_header=""))
        _, name = webhooks._presented_signature(blank, request_with())
        assert name == signing.WEBHOOK_SIGNATURE_HEADER

    def test_a_provider_without_capabilities_does_not_crash(self):
        _, name = webhooks._presented_signature(object(), request_with())
        assert name == signing.WEBHOOK_SIGNATURE_HEADER

    def test_the_fake_mirrors_status200_here_too(self):
        # Dry run must exercise the same header branch production takes.
        assert fake.CAPABILITIES.signature_header == \
            status200.CAPABILITIES.signature_header


# --- Whose verifier ----------------------------------------------------------
class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    """Stands in for the credential query only.

    The WHERE clause is not honoured — it is a database concern and the e2e
    suite covers it against real rows. `statements` is kept so one test can
    assert the query is at least scoped to the right provider and kind, which is
    the part a refactor could plausibly drop.
    """

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self.rows)


def cred(secret, *, group_id=GROUP_A, aad="whsec", cred_id="c1"):
    blob = crypto.encrypt(secret, aad=aad)
    return SimpleNamespace(
        id=cred_id, publish_group_id=group_id,
        key_version=blob["key_version"], nonce_b64=blob["nonce_b64"],
        ciphertext_b64=blob["ciphertext_b64"], aad=blob["aad"])


def verify_with(provider, rows, signature, *, provider_name="zernio",
                body=BODY):
    session = _Session(rows)
    import asyncio
    result = asyncio.run(webhooks._verify_against_groups(
        session, provider_name, body, signature, provider))
    return result, session


class TestVerifierSelection:
    def test_status200_uses_the_proven_hex_path(self, key_a):
        # No `verify_signature` on the adapter, so the module default runs — the
        # exact function that has been verifying production callbacks.
        assert not hasattr(status200.PROVIDER, "verify_signature")
        (group, matched, any_secret), _ = verify_with(
            status200.PROVIDER, [cred(SECRET)], hex_sig(),
            provider_name="status200")
        assert matched is True
        assert group == GROUP_A
        assert any_secret is True

    def test_zernio_opts_into_the_encoding_tolerant_verifier(self, key_a):
        assert zernio.PROVIDER.verify_signature is \
            signing.verify_webhook_signature_any_encoding
        (_, matched, _), _ = verify_with(
            zernio.PROVIDER, [cred(SECRET)], b64_sig())
        assert matched is True

    def test_a_base64_digest_is_what_the_opt_in_buys(self, key_a):
        """The same body and secret, verified by one adapter and not the other.

        Zernio documents "HMAC-SHA256 of the raw body" and never says how it is
        encoded. If the base64 spelling had to go through Status 200's hex-only
        path, every Zernio callback would be rejected — so this asymmetry is the
        feature, not an inconsistency.
        """
        sig = b64_sig()
        (_, zernio_matched, _), _ = verify_with(
            zernio.PROVIDER, [cred(SECRET)], sig)
        (_, s200_matched, _), _ = verify_with(
            status200.PROVIDER, [cred(SECRET)], sig, provider_name="status200")
        assert zernio_matched is True
        assert s200_matched is False

    def test_hex_still_verifies_for_zernio(self, key_a):
        # The tolerant verifier is a superset: if Zernio sends hex after all,
        # nothing has to change.
        (_, matched, _), _ = verify_with(
            zernio.PROVIDER, [cred(SECRET)], hex_sig())
        assert matched is True

    def test_a_wrong_secret_fails_for_the_tolerant_verifier_too(self, key_a):
        # Accepting two encodings is not accepting two secrets.
        (_, matched, _), _ = verify_with(
            zernio.PROVIDER, [cred(SECRET)], b64_sig(secret="not-the-secret"))
        assert matched is False

    def test_a_tampered_body_fails(self, key_a):
        (_, matched, _), _ = verify_with(
            zernio.PROVIDER, [cred(SECRET)], b64_sig(body=BODY + b" "))
        assert matched is False

    def test_no_provider_falls_back_to_the_module_verifier(self, key_a):
        # The drain path and any caller that has not resolved an adapter must
        # still verify rather than accept everything.
        (_, matched, _), _ = verify_with(None, [cred(SECRET)], hex_sig())
        assert matched is True
        (_, matched, _), _ = verify_with(None, [cred(SECRET)], b64_sig())
        assert matched is False


class TestWhichGroupMatched:
    def test_the_matching_secret_names_the_group(self, key_a):
        rows = [cred(OTHER_SECRET, group_id=GROUP_B, cred_id="b"),
                cred(SECRET, group_id=GROUP_A, cred_id="a")]
        (group, matched, _), _ = verify_with(zernio.PROVIDER, rows, hex_sig())
        assert matched is True
        assert group == GROUP_A

    def test_a_multi_account_group_verifies_on_either_secret(self, key_a):
        """One group, two provider accounts, two signing secrets.

        This is the whole multi-credential case and it needs no special handling
        here: both of the group's secrets are candidates, so a callback from
        either Zernio account resolves to the same group.
        """
        rows = [cred(SECRET, group_id=GROUP_A, cred_id="a1"),
                cred(OTHER_SECRET, group_id=GROUP_A, cred_id="a2")]
        for secret in (SECRET, OTHER_SECRET):
            (group, matched, _), _ = verify_with(
                zernio.PROVIDER, rows, hex_sig(secret=secret))
            assert matched is True, secret
            assert group == GROUP_A

    def test_the_first_match_wins_and_is_not_overwritten(self, key_a):
        # Two groups sharing a secret is an operator mistake, not a crash: the
        # reported group is deterministic (the first row) rather than whichever
        # happened to be iterated last.
        rows = [cred(SECRET, group_id=GROUP_A, cred_id="a"),
                cred(SECRET, group_id=GROUP_B, cred_id="b")]
        (group, matched, _), _ = verify_with(zernio.PROVIDER, rows, hex_sig())
        assert matched is True
        assert group == GROUP_A

    def test_every_secret_is_tried_even_after_a_match(self, key_a, monkeypatch):
        """No early exit — the work must not depend on which group matched.

        Returning as soon as a secret verifies would make the response time a
        side channel revealing which of the operator's groups a probed body
        belongs to.
        """
        seen = []
        real = crypto.decrypt

        def counting(blob, aad=None):
            seen.append(blob["nonce_b64"])
            return real(blob, aad=aad)

        monkeypatch.setattr(webhooks.crypto, "decrypt", counting)
        rows = [cred(SECRET, cred_id="a"),
                cred(OTHER_SECRET, cred_id="b"),
                cred(OTHER_SECRET, cred_id="c")]
        (_, matched, _), _ = verify_with(zernio.PROVIDER, rows, hex_sig())
        assert matched is True
        assert len(seen) == 3


class TestMisconfigurationIsDistinguishable:
    def test_no_secret_stored_at_all(self, key_a):
        # The operator-visible difference: "nobody can ever verify" is our bug,
        # not theirs, and the handler logs a different message for it.
        (group, matched, any_secret), _ = verify_with(
            zernio.PROVIDER, [], hex_sig())
        assert (group, matched, any_secret) == (None, False, False)

    def test_no_signature_presented_but_secrets_exist(self, key_a):
        (group, matched, any_secret), _ = verify_with(
            zernio.PROVIDER, [cred(SECRET)], "")
        assert (group, matched, any_secret) == (None, False, True)

    def test_a_bad_signature_with_secrets_configured(self, key_a):
        (group, matched, any_secret), _ = verify_with(
            zernio.PROVIDER, [cred(SECRET)], "sha256=" + "0" * 64)
        assert (group, matched, any_secret) == (None, False, True)

    def test_an_unreadable_secret_does_not_abort_the_others(self, monkeypatch):
        """One row sealed under a retired master key must not blind the rest.

        A half-finished rotation would otherwise take down every group's
        callbacks, not just the group whose row is stale.
        """
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_B)
        monkeypatch.delenv("PUBLISHING_MASTER_KEY_OLD", raising=False)
        stale = cred(SECRET, cred_id="stale")  # sealed under B
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_A)
        readable = cred(OTHER_SECRET, group_id=GROUP_B, cred_id="ok")

        (group, matched, any_secret), _ = verify_with(
            zernio.PROVIDER, [stale, readable], hex_sig(secret=OTHER_SECRET))
        assert matched is True
        assert group == GROUP_B
        assert any_secret is True

    def test_an_unreadable_secret_is_still_counted_as_configured(self, monkeypatch):
        # `any_secret` is about rows, not readability: with one unreadable row
        # and nothing else, the honest report is "a secret exists and did not
        # verify", which is what sends the operator to the rotation window
        # instead of to the credential form.
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_B)
        monkeypatch.delenv("PUBLISHING_MASTER_KEY_OLD", raising=False)
        stale = cred(SECRET, cred_id="stale")
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_A)

        (group, matched, any_secret), _ = verify_with(
            zernio.PROVIDER, [stale], hex_sig())
        assert (group, matched, any_secret) == (None, False, True)

    def test_the_stored_aad_is_what_decrypts(self, key_a):
        # The call site passes the row's own `aad` rather than rebuilding it, so
        # a change to the AAD formula cannot make existing secrets unreadable.
        row = cred(SECRET, aad=f"group:{GROUP_A}|kind:webhook_secret|"
                              f"provider:zernio|slot:zernio-a")
        (group, matched, _), _ = verify_with(zernio.PROVIDER, [row], hex_sig())
        assert matched is True
        assert group == GROUP_A


class TestTheCredentialQuery:
    def where_clause(self):
        _, session = verify_with(zernio.PROVIDER, [cred(SECRET)], hex_sig())
        # Newlines collapsed: SQLAlchemy renders the clauses on separate lines.
        sql = " ".join(str(session.statements[0]).lower().split())
        # Only the predicate — every model column appears in the SELECT list, so
        # asserting against the whole statement would prove nothing.
        assert " where " in sql
        return sql.split(" where ", 1)[1]

    def test_it_is_scoped_to_webhook_secrets_for_this_provider(self, key_a):
        where = self.where_clause()
        # An unscoped query would try another provider's api_key rows as signing
        # secrets: every one fails to verify, and the log says "did not match".
        assert "provider" in where and "kind" in where
        assert "active" in where and "revoked_at" in where

    def test_it_does_not_filter_by_slot(self, key_a):
        """Deliberate: the callback carries no slot hint.

        Which provider account sent it is *discovered* by which secret verifies.
        Filtering by slot would need an answer before the question could be
        asked.
        """
        assert "credential_slot" not in self.where_clause()
