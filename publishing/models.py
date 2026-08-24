"""Publishing schema.

Nine tables on the SAME declarative Base as ``cloud/models.py``, so one engine
and one ``create_all`` serve both and a publish row can reference a cloud user.

The shape follows one rule: **the destination is the unit of publication.** A
``publish_attempt`` is bound to exactly one connected social account, never to a
group. A group is a reusable grouping that *expands* into destinations at
request time, which is what lets single-account, hand-picked multi-account, and
whole-group publishing share one code path instead of three.

Column-type note: Postgres-only (UUID/JSONB). Publishing requires Postgres by
design — see config.validate_required.
"""
import uuid

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from cloud.database import Base


def _uuid():
    return uuid.uuid4()


# --- Enumerated values (kept as plain strings, validated in state.py) --------
# Deliberately NOT Postgres ENUMs: adding a state to a native enum needs a
# migration and a lock, and this state machine will grow.

class PublishGroup(Base):
    """A reusable grouping of destinations sharing ONE provider credential.

    The UI calls this a "Batch". It carries no per-platform detail and no
    provider account identity of its own — those live on the destinations.
    """
    __tablename__ = "publish_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # NULL in self-host (no auth at all). Set in cloud mode so a group is owned.
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    name = Column(String(120), nullable=False)
    provider = Column(String(40), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    # Free-form operator notes + future per-group publishing defaults (caption
    # template, default schedule window). JSONB so adding one needs no migration.
    settings = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    __table_args__ = (
        Index("ix_publish_groups_user_enabled", "user_id", "enabled"),
    )


class PublishCredential(Base):
    """An encrypted provider secret. One row per (group, kind, slot) generation.

    Rotation is by-insert: a new row supersedes the old one, and the old row is
    retained (revoked, not deleted) so the audit trail explains which credential
    a historical attempt used.

    ``kind`` separates the outbound API key from the inbound webhook signing
    secret. They are different secrets with different blast radii and the AAD
    binds each ciphertext to its kind.

    ``credential_slot`` is what allows MORE THAN ONE provider account behind one
    group. It exists because of a hard external limit: Zernio's free tier
    connects 2 social accounts per Zernio account, and a full fan-out needs 3
    (TikTok + Instagram + YouTube). So a group holds several API keys, each
    labelled with a slot, and a destination names the slot it publishes through.
    NULL means "the group default" — which is exactly what every pre-existing
    Status 200 group is, so nothing about a one-key group changes.
    """
    __tablename__ = "publish_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    publish_group_id = Column(UUID(as_uuid=True),
                              ForeignKey("publish_groups.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    provider = Column(String(40), nullable=False)
    # 'api_key' | 'webhook_secret'
    kind = Column(String(30), nullable=False, default="api_key")
    # Operator-chosen label naming ONE provider account inside this group, e.g.
    # "zernio-a". NULL = the group's default credential (the only shape that
    # existed before multi-account support, and still the only one Status 200
    # uses).
    credential_slot = Column(String(32), nullable=True)

    # --- sealed material (see crypto.encrypt) ---
    key_version = Column(String(20), nullable=False)
    nonce_b64 = Column(Text, nullable=False)
    ciphertext_b64 = Column(Text, nullable=False)
    aad = Column(Text, nullable=False, default="")

    # --- the ONLY fields ever shown to a human ---
    fingerprint = Column(String(64), nullable=False)
    last4 = Column(String(8), nullable=False, default="")

    active = Column(Boolean, nullable=False, default=True)
    # Set when a provider 401s on this credential, so the dispatcher can stop
    # retrying a key that is definitively dead instead of burning attempts.
    invalid_at = Column(DateTime(timezone=True), nullable=True)
    invalid_reason = Column(Text, nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The credential lookup carries the slot, because resolution is
        # slot-then-default: a destination's own slot first, falling back to the
        # NULL-slot group default.
        Index("ix_publish_credentials_lookup",
              "publish_group_id", "kind", "credential_slot", "active"),
        # At most one ACTIVE credential per named slot. "Rotation is by-insert"
        # is enforced in application code by the revoke sweep in admin_api; this
        # pins it in the schema so a concurrent write cannot leave two live keys
        # in one slot and make dispatch's choice arbitrary.
        #
        # Restricted to non-NULL slots on purpose. Postgres treats NULLs as
        # distinct in a unique index, so the group default would not be covered
        # anyway, and that is exactly the pre-existing behaviour for the
        # pre-existing shape.
        Index("uq_credential_active_per_slot",
              "publish_group_id", "kind", "credential_slot",
              unique=True,
              postgresql_where=text(
                  "active AND revoked_at IS NULL "
                  "AND credential_slot IS NOT NULL")),
    )


class PublishDestination(Base):
    """ONE specific connected social account. The atomic publish target.

    ``provider_account_ref`` is whatever the provider uses to address the
    account (Status 200: the connected ``accountId`` / handle). It is opaque to
    everything outside the adapter — the application never parses it.

    Quota columns cache what the provider told us on the last response
    (``X-RateLimit-*`` headers, or a 202 "queued for next day" which
    authoritatively means zero left). They are an optimization for dispatch
    ordering and a cheap pre-check; the provider remains the authority.
    """
    __tablename__ = "publish_destinations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    publish_group_id = Column(UUID(as_uuid=True),
                              ForeignKey("publish_groups.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    provider = Column(String(40), nullable=False)
    # 'youtube' | 'instagram' | 'tiktok' | ... (provider-neutral platform key)
    platform = Column(String(40), nullable=False)
    provider_account_ref = Column(String(255), nullable=False)
    # Human label for the UI, e.g. "@openshorts_es". Cosmetic only.
    display_name = Column(String(255), nullable=True)
    # Which provider account (credential slot) this destination publishes
    # through. NULL = the group's default credential, which is every Status 200
    # destination and every single-key group. Only matters when one group holds
    # several provider accounts — see PublishCredential.credential_slot.
    credential_slot = Column(String(32), nullable=True)

    enabled = Column(Boolean, nullable=False, default=True)
    # 'unverified' | 'ok' | 'blocked' | 'disconnected'
    # blocked/disconnected come from a 403 or a profile.disconnected webhook and
    # stop dispatch without failing the whole request.
    health = Column(String(20), nullable=False, default="unverified")
    health_detail = Column(Text, nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)

    # --- cached quota view ---
    quota_limit = Column(Integer, nullable=True)
    quota_remaining = Column(Integer, nullable=True)
    quota_reset_at = Column(DateTime(timezone=True), nullable=True)
    # Provider-enforced spacing between posts (429 cooldown). Dispatch defers
    # past this instead of hammering and burning attempts on a known-throttled
    # destination.
    cooldown_until = Column(DateTime(timezone=True), nullable=True)

    # Per-destination publishing overrides (caption template, privacy, tags).
    settings = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    __table_args__ = (
        # The same social account must not be registered twice inside a group:
        # that would silently double-post every batch publish.
        UniqueConstraint("publish_group_id", "platform", "provider_account_ref",
                         name="uq_destination_identity"),
        Index("ix_publish_destinations_dispatch",
              "publish_group_id", "enabled", "health"),
    )


class PublishAssignment(Base):
    """A clip earmarked for a group, before anyone asked to publish it.

    This is the planning layer that lets "each group gets N different clips per
    day" work without N being a constant anywhere: the assignment records WHICH
    clip goes to WHICH group, and a scheduler (or a human) turns assignments
    into requests. Deleting the row un-assigns; it never cancels a live post.
    """
    __tablename__ = "publish_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    publish_group_id = Column(UUID(as_uuid=True),
                              ForeignKey("publish_groups.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Stable clip identity. Filenames mutate on every post-processing op
    # (subtitled_/hook_/translated_/edited_ prefixes) and video_url is rewritten
    # in place, so NOTHING here may key on a filename or a URL.
    job_id = Column(String(64), nullable=False)
    clip_index = Column(Integer, nullable=False)
    content_fingerprint = Column(String(64), nullable=True)

    # 'pending' | 'requested' | 'cancelled'
    status = Column(String(20), nullable=False, default="pending")
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    publish_request_id = Column(UUID(as_uuid=True), nullable=True)
    meta = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # One clip is assigned to a given group at most once. Re-running the
        # daily planner is therefore idempotent.
        UniqueConstraint("publish_group_id", "job_id", "clip_index",
                         name="uq_assignment_clip_per_group"),
        Index("ix_publish_assignments_due", "status", "scheduled_for"),
    )


class PublishRequest(Base):
    """One user-visible publish operation for one video.

    Note what is NOT here: ``publish_group_id``. A request fans out to an
    arbitrary destination set that may span groups (``Clip 4 -> YouTube 1 +
    TikTok 2 + Instagram 3``), so binding a request to one group would be a lie.
    The group only appears on the attempts, via each destination.

    ``status`` is DERIVED from the attempt rows and cached here for cheap
    listing — state.derive_request_status is the single source of truth.
    """
    __tablename__ = "publish_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    job_id = Column(String(64), nullable=False)
    clip_index = Column(Integer, nullable=False)
    content_fingerprint = Column(String(64), nullable=True)

    # 'single' | 'multi' | 'group' | 'scheduled' — provenance for the UI only.
    # It must never gate behaviour: all four expand to the same destination list.
    mode = Column(String(20), nullable=False, default="multi")

    # Caption/title/tags resolved at request time, so editing a template later
    # never rewrites what a live post says.
    payload = Column(JSONB, nullable=True)

    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    # 'pending' | 'in_progress' | 'succeeded' | 'partial' | 'failed'
    # | 'deferred' | 'cancelled'
    status = Column(String(20), nullable=False, default="pending")

    # Caller-supplied or server-derived. UNIQUE, so a double-clicked publish
    # button or a retried HTTP call cannot create a second fan-out.
    idempotency_key = Column(String(120), nullable=True, unique=True)

    created_by = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_publish_requests_clip", "job_id", "clip_index"),
        Index("ix_publish_requests_status", "status", "scheduled_for"),
    )


class PublishAttempt(Base):
    """THE publication record: one row per destination per try.

    Everything the operator cares about — did THIS account get THIS clip, when,
    with what provider id, and why did it fail — is answerable from this table
    alone, without joining through a group.
    """
    __tablename__ = "publish_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    publish_request_id = Column(UUID(as_uuid=True),
                                ForeignKey("publish_requests.id",
                                           ondelete="CASCADE"),
                                nullable=False, index=True)
    publish_destination_id = Column(UUID(as_uuid=True),
                                    ForeignKey("publish_destinations.id",
                                               ondelete="CASCADE"),
                                    nullable=False, index=True)
    # Denormalized for query convenience and to survive a destination move.
    publish_group_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    provider = Column(String(40), nullable=False)
    platform = Column(String(40), nullable=False)

    attempt_number = Column(Integer, nullable=False, default=1)
    # pending | in_flight | submitted | succeeded | failed | deferred | dead
    # | blocked | unknown | skipped | cancelled
    status = Column(String(20), nullable=False, default="pending")

    # Provider's id for the submission, and (when the provider reports it) the
    # native id on the social platform itself — the one that maps to a real URL.
    provider_post_ref = Column(String(255), nullable=True)
    provider_native_post_ref = Column(String(255), nullable=True)
    permalink = Column(Text, nullable=True)

    # Which credential row signed this submission. Explains a historical post
    # after a key rotation.
    publish_credential_id = Column(UUID(as_uuid=True), nullable=True)
    publish_media_id = Column(UUID(as_uuid=True), nullable=True)

    # Scheduling/backoff clock. One column serves both: a scheduled post and a
    # backed-off retry are both "don't touch me before T".
    deferred_until = Column(DateTime(timezone=True), nullable=True)

    error_code = Column(String(60), nullable=True)
    error_message = Column(Text, nullable=True)
    # Full provider response (scrubbed) for forensics.
    provider_response = Column(JSONB, nullable=True)
    # Quota as reported at submit time — explains after the fact why a post was
    # deferred or queued to the next day.
    quota_snapshot = Column(JSONB, nullable=True)

    # Guards against a worker that died mid-submit being re-claimed instantly.
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    claimed_by = Column(String(80), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    # When the provider was last asked "is this post live?". Only providers that
    # declare `supports_status_lookup` are ever polled, so this stays NULL for
    # the rest — and that is the point of storing it rather than deriving it: it
    # is what rate-limits the poller to one question per post per interval
    # instead of one per reconciliation tick, and it survives a restart, so a
    # process that flaps does not turn into a request flood.
    last_polled_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    __table_args__ = (
        # Claim index: the dispatcher's hot path is "work that is due".
        Index("ix_publish_attempts_claim", "status", "deferred_until"),
        Index("ix_publish_attempts_dest_status", "publish_destination_id",
              "status"),
        # Webhook correlation.
        Index("ix_publish_attempts_provider_ref", "provider", "provider_post_ref"),
        # THE duplicate-post guard. At most one live-or-won attempt per
        # (request, destination); a retry is only creatable once the previous
        # attempt is terminal-and-failed. Partial index -> failed/dead rows do
        # not block the retry that replaces them.
        Index("uq_attempt_live_per_destination",
              "publish_request_id", "publish_destination_id",
              unique=True,
              postgresql_where=(
                  status.in_(("pending", "in_flight", "submitted", "succeeded"))
              )),
    )


class PublishMedia(Base):
    """A reusable provider-side media reference for one clip.

    Status 200 ingests media by URL and returns a ``file_id`` reusable across
    posts AND platforms, so one upload serves a whole 3-platform fan-out. Cache
    keyed by (group, provider, fingerprint) because cross-credential reuse is
    NOT documented — the conservative scope. If reuse across keys is later
    confirmed this becomes a cache-key widening, not a schema change.

    ``expires_at`` matters: refs roll off (Status 200: 7 days), so a scheduled
    post must re-upload rather than submit a dead ref.
    """
    __tablename__ = "publish_media"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    publish_group_id = Column(UUID(as_uuid=True),
                              ForeignKey("publish_groups.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    provider = Column(String(40), nullable=False)
    content_fingerprint = Column(String(64), nullable=False)

    job_id = Column(String(64), nullable=True)
    clip_index = Column(Integer, nullable=True)

    provider_media_ref = Column(String(255), nullable=False)
    source_url = Column(Text, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    mime_type = Column(String(80), nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("publish_group_id", "provider", "content_fingerprint",
                         name="uq_media_per_group_content"),
    )


class ProviderWebhookEvent(Base):
    """Raw inbound provider callback, persisted BEFORE it is interpreted.

    Two reasons for the table. First, the provider requires a 2xx within ~5s, so
    the handler must persist-and-ack and let a drain worker do the real work.
    Second, the signature carries no timestamp or nonce, so it never expires on
    its own; ``provider_event_id`` UNIQUE is what makes a replayed body a no-op.
    """
    __tablename__ = "provider_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider = Column(String(40), nullable=False)
    provider_event_id = Column(String(255), nullable=False)
    event_type = Column(String(60), nullable=True)
    # Which group's signing secret verified it — a webhook is only trusted
    # relative to a specific credential.
    publish_group_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    payload = Column(JSONB, nullable=False)
    signature_valid = Column(Boolean, nullable=False, default=False)
    provider_created_at = Column(DateTime(timezone=True), nullable=True)

    processed = Column(Boolean, nullable=False, default=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    process_error = Column(Text, nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id",
                         name="uq_webhook_event_identity"),
        Index("ix_webhook_events_pending", "processed", "received_at"),
    )


class PublishEvent(Base):
    """Append-only audit log. Never updated, never used to derive state.

    Exists so "why did this post go out twice / not at all" is answerable from
    the database months later, independent of application logs.
    """
    __tablename__ = "publish_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    publish_request_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    publish_attempt_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    publish_destination_id = Column(UUID(as_uuid=True), nullable=True)
    publish_group_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    kind = Column(String(60), nullable=False)
    message = Column(Text, nullable=True)
    data = Column(JSONB, nullable=True)
    actor = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_publish_events_time", "created_at"),
    )
