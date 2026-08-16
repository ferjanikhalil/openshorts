"""publishing baseline: groups, credentials, destinations, requests, attempts

Revision ID: 20260809_publishing
Revises:
Create Date: 2026-08-09

Baseline for the automated-publishing schema (``publishing/models.py``).

Why this migration is defensive
-------------------------------
This repository's boot path is ``create_all`` (see ``cloud.database.init_engine``)
and Alembic exists for controlled changes on top of it. That means a real
deployment can reach ``alembic upgrade head`` with the tables ALREADY present,
created by a previous boot. A plain ``op.create_table`` would abort there with
"relation already exists" and leave the environment un-stampable.

So every step below checks the live catalogue first and creates only what is
missing. The result is identical DDL whichever path ran first, and the migration
is safe to apply to a fresh database, to a database that has already booted, and
to a database that is half-way between the two.

Cloud-mode tables are deliberately NOT included: they have no baseline of their
own and remain ``create_all``-managed. This revision is the publishing schema
only.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260809_publishing"
down_revision = None
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")

# The nine tables, in dependency order.
TABLES = (
    "publish_groups",
    "publish_credentials",
    "publish_destinations",
    "publish_assignments",
    "publish_requests",
    "publish_attempts",
    "publish_media",
    "provider_webhook_events",
    "publish_events",
)


def _existing_tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_indexes(table):
    insp = sa.inspect(op.get_bind())
    names = {ix["name"] for ix in insp.get_indexes(table)}
    # Unique CONSTRAINTS are reported separately from indexes in Postgres.
    names |= {uq["name"] for uq in insp.get_unique_constraints(table)}
    return names


def _index(name, table, cols, unique=False, **kw):
    """Create an index only if nothing by that name is already there."""
    if name in _existing_indexes(table):
        return
    op.create_index(name, table, cols, unique=unique, **kw)


def upgrade():
    have = _existing_tables()

    # --- publish_groups ------------------------------------------------------
    # A reusable bundle of destinations sharing one provider credential. Not the
    # unit of publication — see publish_attempts.
    if "publish_groups" not in have:
        op.create_table(
            "publish_groups",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("user_id", UUID, nullable=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("settings", JSONB, nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
            sa.Column("updated_at", TS, server_default=NOW),
        )
    _index("ix_publish_groups_user_id", "publish_groups", ["user_id"])
    _index("ix_publish_groups_user_enabled", "publish_groups",
           ["user_id", "enabled"])

    # --- publish_credentials -------------------------------------------------
    # Sealed provider secrets. Rotation is by-insert: the superseded row is
    # revoked, never deleted, so a historical attempt can still name the key that
    # signed it. Only `fingerprint` and `last4` are ever shown to a human.
    if "publish_credentials" not in have:
        op.create_table(
            "publish_credentials",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("publish_group_id", UUID,
                      sa.ForeignKey("publish_groups.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("key_version", sa.String(20), nullable=False),
            sa.Column("nonce_b64", sa.Text(), nullable=False),
            sa.Column("ciphertext_b64", sa.Text(), nullable=False),
            sa.Column("aad", sa.Text(), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("last4", sa.String(8), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("invalid_at", TS, nullable=True),
            sa.Column("invalid_reason", sa.Text(), nullable=True),
            sa.Column("last_used_at", TS, nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
            sa.Column("revoked_at", TS, nullable=True),
        )
    _index("ix_publish_credentials_publish_group_id", "publish_credentials",
           ["publish_group_id"])
    _index("ix_publish_credentials_lookup", "publish_credentials",
           ["publish_group_id", "kind", "active"])

    # --- publish_destinations ------------------------------------------------
    # One connected social account: the atomic publish target.
    if "publish_destinations" not in have:
        op.create_table(
            "publish_destinations",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("publish_group_id", UUID,
                      sa.ForeignKey("publish_groups.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("platform", sa.String(40), nullable=False),
            sa.Column("provider_account_ref", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("health", sa.String(20), nullable=False),
            sa.Column("health_detail", sa.Text(), nullable=True),
            sa.Column("verified_at", TS, nullable=True),
            sa.Column("quota_limit", sa.Integer(), nullable=True),
            sa.Column("quota_remaining", sa.Integer(), nullable=True),
            sa.Column("quota_reset_at", TS, nullable=True),
            sa.Column("cooldown_until", TS, nullable=True),
            sa.Column("settings", JSONB, nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
            sa.Column("updated_at", TS, server_default=NOW),
            # Registering the same account twice inside a group would silently
            # double-post every whole-group publish.
            sa.UniqueConstraint("publish_group_id", "platform",
                                "provider_account_ref",
                                name="uq_destination_identity"),
        )
    _index("ix_publish_destinations_publish_group_id", "publish_destinations",
           ["publish_group_id"])
    _index("ix_publish_destinations_dispatch", "publish_destinations",
           ["publish_group_id", "enabled", "health"])

    # --- publish_assignments -------------------------------------------------
    # Planning layer: which clip is earmarked for which group, before anyone
    # asked to publish it. Keyed on (job_id, clip_index) and never on a filename
    # or a URL — both are rewritten in place by post-processing.
    if "publish_assignments" not in have:
        op.create_table(
            "publish_assignments",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("publish_group_id", UUID,
                      sa.ForeignKey("publish_groups.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("user_id", UUID, nullable=True),
            sa.Column("job_id", sa.String(64), nullable=False),
            sa.Column("clip_index", sa.Integer(), nullable=False),
            sa.Column("content_fingerprint", sa.String(64), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("scheduled_for", TS, nullable=True),
            sa.Column("publish_request_id", UUID, nullable=True),
            sa.Column("meta", JSONB, nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
            # Makes re-running the daily planner idempotent.
            sa.UniqueConstraint("publish_group_id", "job_id", "clip_index",
                                name="uq_assignment_clip_per_group"),
        )
    _index("ix_publish_assignments_publish_group_id", "publish_assignments",
           ["publish_group_id"])
    _index("ix_publish_assignments_user_id", "publish_assignments", ["user_id"])
    _index("ix_publish_assignments_due", "publish_assignments",
           ["status", "scheduled_for"])

    # --- publish_requests ----------------------------------------------------
    # One user-visible publish operation. Note the absence of publish_group_id:
    # a request may span groups, so binding it to one would be a lie.
    if "publish_requests" not in have:
        op.create_table(
            "publish_requests",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("user_id", UUID, nullable=True),
            sa.Column("job_id", sa.String(64), nullable=False),
            sa.Column("clip_index", sa.Integer(), nullable=False),
            sa.Column("content_fingerprint", sa.String(64), nullable=True),
            sa.Column("mode", sa.String(20), nullable=False),
            sa.Column("payload", JSONB, nullable=True),
            sa.Column("scheduled_for", TS, nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            # UNIQUE: a double-clicked publish button cannot create a second
            # fan-out.
            sa.Column("idempotency_key", sa.String(120), nullable=True,
                      unique=True),
            sa.Column("created_by", sa.String(120), nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
            sa.Column("updated_at", TS, server_default=NOW),
            sa.Column("completed_at", TS, nullable=True),
        )
    _index("ix_publish_requests_user_id", "publish_requests", ["user_id"])
    _index("ix_publish_requests_clip", "publish_requests",
           ["job_id", "clip_index"])
    _index("ix_publish_requests_status", "publish_requests",
           ["status", "scheduled_for"])

    # --- publish_attempts ----------------------------------------------------
    # THE publication record: one row per destination per try.
    if "publish_attempts" not in have:
        op.create_table(
            "publish_attempts",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("publish_request_id", UUID,
                      sa.ForeignKey("publish_requests.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("publish_destination_id", UUID,
                      sa.ForeignKey("publish_destinations.id",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("publish_group_id", UUID, nullable=False),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("platform", sa.String(40), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("provider_post_ref", sa.String(255), nullable=True),
            sa.Column("provider_native_post_ref", sa.String(255), nullable=True),
            sa.Column("permalink", sa.Text(), nullable=True),
            sa.Column("publish_credential_id", UUID, nullable=True),
            sa.Column("publish_media_id", UUID, nullable=True),
            sa.Column("deferred_until", TS, nullable=True),
            sa.Column("error_code", sa.String(60), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("provider_response", JSONB, nullable=True),
            sa.Column("quota_snapshot", JSONB, nullable=True),
            sa.Column("claimed_at", TS, nullable=True),
            sa.Column("claimed_by", sa.String(80), nullable=True),
            sa.Column("submitted_at", TS, nullable=True),
            sa.Column("completed_at", TS, nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
            sa.Column("updated_at", TS, server_default=NOW),
        )
    _index("ix_publish_attempts_publish_request_id", "publish_attempts",
           ["publish_request_id"])
    _index("ix_publish_attempts_publish_destination_id", "publish_attempts",
           ["publish_destination_id"])
    _index("ix_publish_attempts_publish_group_id", "publish_attempts",
           ["publish_group_id"])
    _index("ix_publish_attempts_claim", "publish_attempts",
           ["status", "deferred_until"])
    _index("ix_publish_attempts_dest_status", "publish_attempts",
           ["publish_destination_id", "status"])
    _index("ix_publish_attempts_provider_ref", "publish_attempts",
           ["provider", "provider_post_ref"])
    # THE duplicate-post guard. At most one live-or-won attempt per
    # (request, destination). Partial, so failed/dead rows do not block the
    # retry that replaces them.
    _index(
        "uq_attempt_live_per_destination", "publish_attempts",
        ["publish_request_id", "publish_destination_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'in_flight', 'submitted', 'succeeded')"),
    )

    # --- publish_media -------------------------------------------------------
    # Provider-side media refs, reusable across posts and platforms. Scoped per
    # group because cross-credential reuse is not documented; widening that
    # later is a cache-key change, not a schema change.
    if "publish_media" not in have:
        op.create_table(
            "publish_media",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("publish_group_id", UUID,
                      sa.ForeignKey("publish_groups.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("content_fingerprint", sa.String(64), nullable=False),
            sa.Column("job_id", sa.String(64), nullable=True),
            sa.Column("clip_index", sa.Integer(), nullable=True),
            sa.Column("provider_media_ref", sa.String(255), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("size_bytes", sa.Integer(), nullable=True),
            sa.Column("mime_type", sa.String(80), nullable=True),
            sa.Column("expires_at", TS, nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
            sa.Column("last_used_at", TS, nullable=True),
            sa.UniqueConstraint("publish_group_id", "provider",
                                "content_fingerprint",
                                name="uq_media_per_group_content"),
        )
    _index("ix_publish_media_publish_group_id", "publish_media",
           ["publish_group_id"])

    # --- provider_webhook_events ---------------------------------------------
    # Raw inbound callbacks, persisted before interpretation. The signature
    # carries no timestamp or nonce, so the UNIQUE below is what makes a
    # replayed body a no-op.
    if "provider_webhook_events" not in have:
        op.create_table(
            "provider_webhook_events",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("provider_event_id", sa.String(255), nullable=False),
            sa.Column("event_type", sa.String(60), nullable=True),
            sa.Column("publish_group_id", UUID, nullable=True),
            sa.Column("payload", JSONB, nullable=False),
            sa.Column("signature_valid", sa.Boolean(), nullable=False),
            sa.Column("provider_created_at", TS, nullable=True),
            sa.Column("processed", sa.Boolean(), nullable=False),
            sa.Column("processed_at", TS, nullable=True),
            sa.Column("process_error", sa.Text(), nullable=True),
            sa.Column("received_at", TS, server_default=NOW),
            sa.UniqueConstraint("provider", "provider_event_id",
                                name="uq_webhook_event_identity"),
        )
    _index("ix_provider_webhook_events_publish_group_id",
           "provider_webhook_events", ["publish_group_id"])
    _index("ix_webhook_events_pending", "provider_webhook_events",
           ["processed", "received_at"])

    # --- publish_events ------------------------------------------------------
    # Append-only audit log. Never updated, never used to derive state.
    if "publish_events" not in have:
        op.create_table(
            "publish_events",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("publish_request_id", UUID, nullable=True),
            sa.Column("publish_attempt_id", UUID, nullable=True),
            sa.Column("publish_destination_id", UUID, nullable=True),
            sa.Column("publish_group_id", UUID, nullable=True),
            sa.Column("kind", sa.String(60), nullable=False),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("data", JSONB, nullable=True),
            sa.Column("actor", sa.String(120), nullable=True),
            sa.Column("created_at", TS, server_default=NOW),
        )
    _index("ix_publish_events_publish_request_id", "publish_events",
           ["publish_request_id"])
    _index("ix_publish_events_publish_attempt_id", "publish_events",
           ["publish_attempt_id"])
    _index("ix_publish_events_publish_group_id", "publish_events",
           ["publish_group_id"])
    _index("ix_publish_events_time", "publish_events", ["created_at"])


def downgrade():
    """Drop the publishing schema.

    Destructive by definition: the encrypted credentials and the entire record of
    what was posted where go with it. Reverse dependency order so the foreign
    keys come apart cleanly.
    """
    have = _existing_tables()
    for name in reversed(TABLES):
        if name in have:
            op.drop_table(name)
