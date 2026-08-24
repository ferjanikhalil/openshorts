"""Inbound provider callbacks.

For a provider with no status-lookup endpoint, webhooks are the ONLY completion
signal for a submitted post — there is no polling fallback to lean on. Status 200
is such a provider, which is what makes this module load-bearing rather than an
optimization. (Zernio does expose a lookup, so there a lost callback degrades to
"confirmed a minute later by the reconciler" instead of "aged into unknown".)

That shapes two decisions:

**Persist and ack, interpret later.** The provider requires a 2xx within ~5
seconds and retries at 1m/5m/30m. Doing the correlation work inline would risk a
timeout, which turns one event into four deliveries. So the handler verifies the
signature, writes the raw event, and returns — a drain worker does the rest.

**Replay protection is not in the signature.** The signed preimage is the body
alone: no timestamp, no nonce, so a captured request stays valid forever. Two
mechanisms cover that gap and both are required — a UNIQUE constraint on
``(provider, provider_event_id)`` makes a replay a no-op, and a ``created_at``
skew window bounds how old a first-time event may be.

Signature verification is per-credential, because each provider account has its
own signing secret. With no group hint in the payload, the handler tries every
stored secret for the provider; the one that verifies also identifies the group,
which is exactly the property a shared secret gives you. A multi-account group
therefore works without special-casing: it simply has more than one candidate.

Nothing here knows a provider name. Which header carries the signature and how
the digest is encoded are both read from the adapter — hardcoding either would
reject a second provider's callbacks as unsigned, silently.
"""
from datetime import datetime, timezone
import time

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from . import crypto, db, service, state
from .config import WEBHOOK_MAX_SKEW_SECONDS
from .errors import E_PROVIDER_5XX, E_UNKNOWN, ProviderError
from .models import (
    PublishAttempt, PublishCredential, PublishDestination, ProviderWebhookEvent,
)
from . import providers
from .signing import (
    WEBHOOK_SIGNATURE_HEADER, verify_webhook_signature, within_skew,
)

router = APIRouter()

# A rejected callback is logged, but at most this often: the body is
# attacker-controlled, so an unthrottled log line is a way to fill the events
# table. One line per minute is enough to notice a misconfiguration, which is the
# only thing this is for.
_REJECT_LOG_INTERVAL_SECONDS = 60
_last_reject_log = 0.0


def _now():
    return datetime.now(timezone.utc)


def _should_log_rejection() -> bool:
    global _last_reject_log
    now = time.monotonic()
    if now - _last_reject_log < _REJECT_LOG_INTERVAL_SECONDS:
        return False
    _last_reject_log = now
    return True


# Mounted under api.router's "/api/publishing" prefix, so the public URL is
# /api/publishing/webhook/{provider}. Relative here so the prefix has one home.
@router.post("/webhook/{provider_name}")
async def receive_webhook(provider_name: str, request: Request):
    """Verify, persist, ack. No correlation work happens here.

    The provider is resolved FIRST because two things about verification are
    provider-specific and neither may be hardcoded: which header carries the
    signature (Status 200 signs ``X-Webhook-Signature``, Zernio signs
    ``X-Zernio-Signature``) and how the digest is encoded. Reading a fixed header
    name would reject every callback from the second provider as unsigned — a
    silent failure whose only symptom is that posts stop being confirmed.
    """
    raw = await request.body()

    try:
        provider = providers.get(provider_name)
    except KeyError:
        return Response(status_code=404,
                        content='{"error":"unknown provider"}',
                        media_type="application/json")

    signature, header_name = _presented_signature(provider, request)

    async with db.session() as session:
        async with session.begin():
            group_id, secret_ok, any_secret = await _verify_against_groups(
                session, provider_name, raw, signature, provider)

            if not secret_ok:
                # 401, and the body is NOT written: an unverified body is
                # attacker-controlled, so persisting it would let anyone fill the
                # table. But the rejection itself is recorded, because the
                # failure mode this catches is our own misconfiguration — with no
                # secret stored, every real callback 401s in silence and every
                # post ages into needs-check.
                if _should_log_rejection():
                    await service.log_event(
                        session, "webhook.rejected",
                        message=(
                            "a callback arrived but no webhook secret is "
                            f"configured for any {provider_name} batch, so it "
                            "could not be verified"
                            if not any_secret else
                            "callback signature did not match any configured "
                            "webhook secret"
                            if signature else
                            f"callback arrived with no {header_name} header"
                        ),
                        data={"provider": provider_name,
                              "has_signature": bool(signature),
                              "signature_header": header_name,
                              "secrets_configured": any_secret})
                return Response(status_code=401, content='{"error":"invalid signature"}',
                                media_type="application/json")

            try:
                payload = await request.json()
            except Exception:
                return Response(status_code=400,
                                content='{"error":"invalid json"}',
                                media_type="application/json")

            event = provider.parse_webhook(payload)
            if not event.event_id:
                # Without an id there is no replay guard, so it is rejected
                # rather than accepted as un-deduplicable.
                return Response(status_code=400,
                                content='{"error":"missing event id"}',
                                media_type="application/json")

            if not within_skew(event.created_at, WEBHOOK_MAX_SKEW_SECONDS):
                # Ack (2xx) so the provider stops retrying, but do not act: this
                # is either a replay or a badly-skewed clock.
                await service.log_event(
                    session, "webhook.stale",
                    message=f"event {event.event_id} outside the skew window",
                    group_id=group_id)
                return Response(status_code=202,
                                content='{"status":"stale"}',
                                media_type="application/json")

            existing = (await session.execute(
                select(ProviderWebhookEvent).where(
                    ProviderWebhookEvent.provider == provider_name,
                    ProviderWebhookEvent.provider_event_id == event.event_id)
            )).scalar_one_or_none()
            if existing is not None:
                # Replay or a provider retry after a slow ack. Idempotent by
                # construction.
                return Response(status_code=200,
                                content='{"status":"duplicate"}',
                                media_type="application/json")

            session.add(ProviderWebhookEvent(
                provider=provider_name,
                provider_event_id=event.event_id,
                event_type=event.event_type,
                publish_group_id=group_id,
                payload=service._jsonable(payload),
                signature_valid=True,
                provider_created_at=(
                    datetime.fromtimestamp(event.created_at, tz=timezone.utc)
                    if event.created_at else None),
            ))

    return Response(status_code=200, content='{"status":"received"}',
                    media_type="application/json")


def _presented_signature(provider, request: Request):
    """The signature header this provider signs with, and its value.

    Returns ``(value, header_name)``. The declared header is authoritative; the
    generic ``X-Webhook-Signature`` is tried as a fallback so a provider that
    quietly renames its header degrades to "still verified" rather than "every
    callback rejected". Both are checked against the same secret, so a fallback
    match is no weaker than a declared one.
    """
    declared = getattr(getattr(provider, "capabilities", None),
                       "signature_header", None) or WEBHOOK_SIGNATURE_HEADER
    for name in (declared, WEBHOOK_SIGNATURE_HEADER):
        value = (request.headers.get(name) or "").strip()
        if value:
            return value, declared
    return "", declared


async def _verify_against_groups(session, provider_name: str, raw: bytes,
                                 signature: str, provider=None):
    """Find the group whose signing secret validates this body.

    Returns ``(group_id, matched, any_secret_configured)``. The third value is
    what separates "someone sent us a bad signature" from "we never stored a
    secret, so no callback can ever verify" — the second is a misconfiguration on
    our side and the operator has to be able to tell them apart.

    Every configured secret is tried even after a match, so the work done is
    independent of which group matched — a timing side channel here would leak
    which of the operator's groups a probe belongs to.

    Verification itself belongs to the adapter when it offers one: providers
    disagree on the digest encoding, and Status 200's proven hex path must not
    change to accommodate a provider that does not document its own.

    A multi-account group holds one secret PER provider account, and this already
    handles that: it iterates every active ``webhook_secret`` row for the
    provider, so both of a group's secrets are candidates and either verifies.
    """
    verify = getattr(provider, "verify_signature", None) if provider else None
    if verify is None:
        verify = verify_webhook_signature

    rows = (await session.execute(
        select(PublishCredential).where(
            PublishCredential.provider == provider_name,
            PublishCredential.kind == "webhook_secret",
            PublishCredential.active.is_(True),
            PublishCredential.revoked_at.is_(None))
    )).scalars().all()
    if not signature:
        return None, False, bool(rows)

    matched_group = None
    matched = False
    for cred in rows:
        try:
            secret = crypto.decrypt({
                "key_version": cred.key_version,
                "nonce_b64": cred.nonce_b64,
                "ciphertext_b64": cred.ciphertext_b64,
                "aad": cred.aad,
            })
        except Exception as e:
            print(f"⚠️  Publishing: unreadable webhook secret "
                  f"{cred.id}: {crypto.scrub(str(e))}")
            continue
        if verify(secret.reveal(), raw, signature) and not matched:
            matched = True
            matched_group = cred.publish_group_id
    return matched_group, matched, bool(rows)


# --- Drain worker -----------------------------------------------------------
async def drain_pending(limit: int = 50) -> int:
    """Apply persisted events to attempts. Runs on the reconciler loop."""
    processed = 0
    async with db.session() as session:
        async with session.begin():
            rows = (await session.execute(
                select(ProviderWebhookEvent)
                .where(ProviderWebhookEvent.processed.is_(False))
                .order_by(ProviderWebhookEvent.received_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )).scalars().all()

            for row in rows:
                try:
                    await _apply_event(session, row)
                    row.processed = True
                    row.processed_at = _now()
                    row.process_error = None
                except Exception as e:
                    # Keep the row unprocessed but record why: a correlation bug
                    # must not silently swallow a completion signal.
                    row.process_error = crypto.scrub(str(e))[:1000]
                    print(f"⚠️  Publishing: webhook {row.provider_event_id} "
                          f"failed to apply: {row.process_error}")
                processed += 1
    return processed


async def _apply_event(session, row: ProviderWebhookEvent) -> None:
    provider = providers.get(row.provider)
    event = provider.parse_webhook(row.payload or {})

    if event.event_type == "account.disconnected":
        await _handle_disconnect(session, row, event)
        return

    if not event.provider_post_ref:
        return

    attempt = (await session.execute(
        select(PublishAttempt).where(
            PublishAttempt.provider == row.provider,
            PublishAttempt.provider_post_ref == event.provider_post_ref)
        .order_by(PublishAttempt.created_at.desc())
        .limit(1)
        .with_for_update()
    )).scalar_one_or_none()

    if attempt is None:
        # A post we have no record of. Logged, not dropped — it usually means a
        # post was created outside this system, or the submit response was lost
        # before the ref could be stored.
        await service.log_event(
            session, "webhook.unmatched",
            message=f"{event.event_type} for unknown post "
                    f"{event.provider_post_ref}",
            group_id=row.publish_group_id,
            data={"event_id": row.provider_event_id})
        return

    if attempt.status in (state.SUCCEEDED, state.DEAD, state.CANCELLED):
        # Already resolved; a late webhook must not reopen it.
        return

    if event.event_type == "post.published":
        if attempt.status in (state.SUBMITTED, state.IN_FLIGHT, state.UNKNOWN):
            # UNKNOWN -> SUCCEEDED is intentionally allowed here even though the
            # state machine forbids it for code paths: a webhook is positive
            # evidence the post IS live, which is exactly the ambiguity that
            # made it unknown. Resolving it is the whole point.
            attempt.status = state.SUCCEEDED
            attempt.completed_at = _now()
            attempt.provider_native_post_ref = (
                event.provider_native_post_ref
                or attempt.provider_native_post_ref)
            attempt.permalink = event.permalink or attempt.permalink
            await service.log_event(
                session, "attempt.succeeded", message="confirmed by webhook",
                request_id=attempt.publish_request_id, attempt_id=attempt.id,
                destination_id=attempt.publish_destination_id,
                group_id=attempt.publish_group_id)
            await service.refresh_request_status(
                session, attempt.publish_request_id)

    elif event.event_type == "post.failed":
        if attempt.status in (state.SUBMITTED, state.IN_FLIGHT, state.UNKNOWN):
            # Not every "failed" is a refusal. A provider that reports "we
            # stopped waiting for a confirmation" has told us nothing about
            # whether the post is live — observed 2026-08-11, where a clip that
            # DID reach TikTok came back as "Timeout". Treating that as a
            # definite failure would auto-retry a live post and double-publish.
            code = event.error_code or E_PROVIDER_5XX
            if attempt.status == state.UNKNOWN and code == E_UNKNOWN:
                # Ambiguous twice over: leave it unknown and let a human decide.
                await service.log_event(
                    session, "attempt.unknown",
                    message=(event.error_message
                             or "provider reported an inconclusive failure")[:500],
                    request_id=attempt.publish_request_id,
                    attempt_id=attempt.id,
                    destination_id=attempt.publish_destination_id,
                    group_id=attempt.publish_group_id)
                return
            if attempt.status == state.UNKNOWN:
                # The webhook resolved the ambiguity in the safe direction: the
                # post is definitively NOT live, so a retry is now legitimate.
                attempt.status = state.SUBMITTED
            err = ProviderError(
                code,
                (event.error_message or "provider reported failure")[:2000],
                provider_post_ref=event.provider_post_ref)
            await service.record_failure(session, attempt, err)

    elif event.event_type == "post.scheduled":
        await service.log_event(
            session, "attempt.provider_scheduled",
            message=str(event.provider_post_ref),
            request_id=attempt.publish_request_id, attempt_id=attempt.id,
            destination_id=attempt.publish_destination_id,
            group_id=attempt.publish_group_id)


async def _handle_disconnect(session, row: ProviderWebhookEvent, event) -> None:
    """A destination lost its platform connection.

    Marking it here means the next scheduled publish skips it with a clear
    reason instead of spending a quota slot to discover the same 403.
    """
    if not event.provider_account_ref:
        return
    q = select(PublishDestination).where(
        PublishDestination.provider == row.provider,
        PublishDestination.provider_account_ref == event.provider_account_ref)
    if row.publish_group_id:
        q = q.where(PublishDestination.publish_group_id == row.publish_group_id)
    dests = (await session.execute(q)).scalars().all()
    for dest in dests:
        dest.health = "disconnected"
        dest.health_detail = (event.error_message
                              or "provider reported the account disconnected")
        await service.log_event(
            session, "destination.disconnected",
            message=dest.health_detail[:500], destination_id=dest.id,
            group_id=dest.publish_group_id)
