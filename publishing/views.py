"""Row -> dict serializers.

Separate from the routers so that the masking rule has exactly one home: if a
provider secret is ever going to leak to the frontend, it leaks through here.
``credential_out`` is therefore built field-by-field from a fixed list — there is
no ``**row.__dict__`` anywhere in this file, because that pattern is how a
ciphertext column ends up in a JSON response after someone adds a field.
"""
from . import crypto, state


def group_out(row, credential=None, destinations=None, summary=None,
              webhook_secret=None, webhook_url=None, credentials=None,
              webhook_secrets=None) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "provider": row.provider,
        "enabled": bool(row.enabled),
        "settings": row.settings or {},
        "created_at": row.created_at,
        # The group's DEFAULT key (the NULL-slot row). Kept as a scalar field
        # because it is the only shape a single-account group has ever had, and
        # the UI reads it to answer "can this group publish at all?".
        "credential": credential,
        # Every key in the group, default first, then one per named slot. A
        # multi-account provider needs the list; a single-account one returns a
        # one-element list whose only member is `credential` above.
        "credentials": credentials if credentials is not None else (
            [credential] if credential else []),
        # Masked like any other credential (presence + fingerprint, never the
        # secret). Its absence is the fact the UI needs: with no secret stored,
        # every provider callback fails verification and every post ages into
        # needs-check instead of being confirmed.
        "webhook_secret": webhook_secret,
        # One signing secret per provider account: two Zernio accounts issue two
        # independent secrets, and a callback from either has to verify.
        "webhook_secrets": webhook_secrets if webhook_secrets is not None else (
            [webhook_secret] if webhook_secret else []),
        "webhook_url": webhook_url,
        "destinations": destinations or [],
        "summary": summary,
    }


def credential_out(row) -> dict:
    """Masked credential. Deliberately enumerates every field it returns."""
    return {
        "id": str(row.id),
        "kind": row.kind,
        "provider": row.provider,
        # Which provider account inside the group this key is. None = the
        # group's default, which is every Status 200 credential ever stored.
        "credential_slot": getattr(row, "credential_slot", None),
        "fingerprint": row.fingerprint,
        "last4": row.last4,
        # Reconstructed from last4 alone — the plaintext is never decrypted to
        # build this.
        "masked": f"{row.provider[:2]}…{row.last4}" if row.last4 else "…",
        "active": bool(row.active and row.revoked_at is None),
        "invalid": row.invalid_at is not None,
        "invalid_reason": row.invalid_reason,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "needs_rotation": crypto.needs_rotation({"key_version": row.key_version}),
    }


def destination_out(row) -> dict:
    return {
        "id": str(row.id),
        "publish_group_id": str(row.publish_group_id),
        "provider": row.provider,
        "platform": row.platform,
        "provider_account_ref": row.provider_account_ref,
        "display_name": row.display_name,
        # Which of the group's provider accounts owns this destination. None =
        # the group's default credential.
        "credential_slot": getattr(row, "credential_slot", None),
        "enabled": bool(row.enabled),
        "health": row.health,
        "health_detail": row.health_detail,
        "quota_limit": row.quota_limit,
        "quota_remaining": row.quota_remaining,
        "quota_reset_at": row.quota_reset_at,
        "cooldown_until": row.cooldown_until,
        "settings": row.settings or {},
    }


def attempt_out(row, destination=None, include_raw=False) -> dict:
    out = {
        "id": str(row.id),
        "publish_request_id": str(row.publish_request_id),
        "publish_destination_id": str(row.publish_destination_id),
        "publish_group_id": str(row.publish_group_id),
        "platform": row.platform,
        "provider": row.provider,
        "attempt_number": row.attempt_number,
        "status": row.status,
        "provider_post_ref": row.provider_post_ref,
        "provider_native_post_ref": row.provider_native_post_ref,
        "permalink": row.permalink,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "deferred_until": row.deferred_until,
        "submitted_at": row.submitted_at,
        "completed_at": row.completed_at,
        "created_at": row.created_at,
        "needs_attention": row.status in state.NEEDS_ATTENTION,
        "destination_label": (
            (destination.display_name or destination.provider_account_ref)
            if destination is not None else None),
    }
    if include_raw:
        # ADMIN ONLY. What the provider actually answered, verbatim. Off by
        # default because provider internals are not part of the public shape,
        # and on for admins because when an attempt reads `unknown` — or claims
        # to be scheduled while the post is already live — this body is the only
        # evidence of what really happened.
        out["provider_response"] = row.provider_response
        out["quota_snapshot"] = row.quota_snapshot
    return out


def request_out(row, attempts=None) -> dict:
    return {
        "id": str(row.id),
        "job_id": row.job_id,
        "clip_index": row.clip_index,
        "mode": row.mode,
        "status": row.status,
        "scheduled_for": row.scheduled_for,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "payload": row.payload or {},
        "attempts": attempts or [],
    }


def assignment_out(row) -> dict:
    return {
        "id": str(row.id),
        "publish_group_id": str(row.publish_group_id),
        "job_id": row.job_id,
        "clip_index": row.clip_index,
        "status": row.status,
        "scheduled_for": row.scheduled_for,
        "publish_request_id": (str(row.publish_request_id)
                               if row.publish_request_id else None),
        "created_at": row.created_at,
    }


def event_out(row) -> dict:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "message": row.message,
        "actor": row.actor,
        "created_at": row.created_at,
        "data": row.data or {},
        "publish_request_id": (str(row.publish_request_id)
                               if row.publish_request_id else None),
        "publish_attempt_id": (str(row.publish_attempt_id)
                               if row.publish_attempt_id else None),
    }
