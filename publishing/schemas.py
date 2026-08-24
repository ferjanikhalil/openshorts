"""Request/response schemas.

Two rules encoded here rather than left to convention:

  * A credential's plaintext is **write-only**. It appears on exactly one input
    model and on no output model anywhere in this file. There is no field, no
    flag and no debug mode that returns it — the only readable representation is
    fingerprint + last4.
  * Destination selection is a free combination of ``destination_ids`` and
    ``group_ids``. That is what makes single-account, hand-picked multi-account
    and whole-group publishing the same endpoint instead of three.
"""
from datetime import datetime
from typing import List, Optional

import re

from pydantic import BaseModel, Field, field_validator


# --- Groups -----------------------------------------------------------------
class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: Optional[str] = None
    enabled: bool = True
    settings: Optional[dict] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    enabled: Optional[bool] = None
    settings: Optional[dict] = None


class GroupOut(BaseModel):
    id: str
    name: str
    provider: str
    enabled: bool
    settings: Optional[dict] = None
    created_at: Optional[datetime] = None
    # Masked credential view — never the key itself. This is the group's DEFAULT
    # (unslotted) key.
    credential: Optional[dict] = None
    # Every key in the group: the default first, then one per named slot. A
    # single-account provider yields a one-element list.
    credentials: List[dict] = Field(default_factory=list)
    # Same masking for the webhook signing secret. Present/absent is the fact the
    # UI acts on: without it, no provider callback can be verified.
    webhook_secret: Optional[dict] = None
    # One per provider account, for the same reason as `credentials`.
    webhook_secrets: List[dict] = Field(default_factory=list)
    # Callback URL to paste into the provider dashboard; None when no public
    # origin is configured.
    webhook_url: Optional[str] = None
    destinations: List[dict] = Field(default_factory=list)
    summary: Optional[dict] = None


# A credential slot is an operator-typed label, and it lands in an index, an
# encryption AAD and a log line. Keep it to the characters that survive all
# three unambiguously — no spaces (a trailing one would silently make a second
# slot), no case games (lowercased so "Zernio-A" and "zernio-a" are one slot).
_SLOT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")


def _clean_slot(v):
    """Normalize a credential slot, or None for 'the group default'.

    Empty string and None mean the same thing on the wire — the UI sends "" when
    an operator clears the field — and both must collapse to NULL, because a
    ``""`` slot would be a third, invisible category that no lookup matches.
    """
    if v is None:
        return None
    v = str(v).strip().lower()
    if not v:
        return None
    if not _SLOT_RE.match(v):
        raise ValueError(
            "credential_slot must be 1-32 chars of a-z, 0-9, '-', '_' or '.'")
    return v


# --- Credentials ------------------------------------------------------------
class CredentialCreate(BaseModel):
    """The ONLY place a provider secret is accepted. Never echoed back."""

    api_key: str = Field(min_length=8, max_length=500)
    kind: str = "api_key"
    # Names ONE provider account inside the group, e.g. "zernio-a". Omitted (or
    # empty) means the group's default key — the only shape that existed before
    # multi-account support and the only one Status 200 uses.
    credential_slot: Optional[str] = Field(default=None, max_length=32)

    @field_validator("api_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Pasted keys routinely carry whitespace; a trailing newline would
        # produce a valid-looking row that 401s on every publish.
        return v.strip()

    @field_validator("credential_slot")
    @classmethod
    def _slot(cls, v):
        return _clean_slot(v)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("api_key", "webhook_secret"):
            raise ValueError("kind must be 'api_key' or 'webhook_secret'")
        return v


class CredentialOut(BaseModel):
    """Masked view. There is no field here that could carry the secret."""

    id: str
    kind: str
    provider: str
    credential_slot: Optional[str] = None
    fingerprint: str
    last4: str
    masked: str
    active: bool
    invalid: bool = False
    invalid_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    # True when the blob is sealed under a superseded master key, so the admin UI
    # can prompt for a re-entry before the old key is removed from the env.
    needs_rotation: bool = False


# --- Destinations -----------------------------------------------------------
class DestinationCreate(BaseModel):
    platform: str
    provider_account_ref: str = Field(min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)
    # Which of the group's provider accounts holds this social account. None =
    # the group's default credential.
    credential_slot: Optional[str] = Field(default=None, max_length=32)
    enabled: bool = True
    settings: Optional[dict] = None

    @field_validator("credential_slot")
    @classmethod
    def _slot(cls, v):
        return _clean_slot(v)


class DestinationUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None
    settings: Optional[dict] = None
    # Re-pointing a destination at a different provider account. Sent as "" to
    # move it back to the group default. The route distinguishes "absent" from
    # "explicitly empty" via ``model_fields_set``, because both arrive as None
    # here and only one of them means "change it".
    credential_slot: Optional[str] = Field(default=None, max_length=32)
    # Correcting a wrong account reference has to be possible without deleting
    # the destination, because deleting cascades to its attempts and would erase
    # the record of anything already posted. Providers identify accounts by
    # opaque strings whose format is not knowable in advance — Status 200 turned
    # out to want a profile UUID while both its docs and its own "copy API ID"
    # button offered the @handle — and a wrong one is rejected with an error too
    # generic to diagnose. Guarded in the route: refused while work is in flight.
    provider_account_ref: Optional[str] = Field(default=None, min_length=1,
                                                max_length=255)
    # Lets an operator clear a `blocked` health after fixing the connection at
    # the platform, without deleting and recreating the destination (which would
    # orphan its publication history).
    reset_health: bool = False

    @field_validator("credential_slot")
    @classmethod
    def _slot(cls, v):
        return _clean_slot(v)


class DestinationOut(BaseModel):
    id: str
    publish_group_id: str
    provider: str
    platform: str
    provider_account_ref: str
    display_name: Optional[str] = None
    credential_slot: Optional[str] = None
    enabled: bool
    health: str
    health_detail: Optional[str] = None
    quota_limit: Optional[int] = None
    quota_remaining: Optional[int] = None
    quota_reset_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None


# --- Publishing -------------------------------------------------------------
class PublishCreate(BaseModel):
    """One clip to any combination of destinations and/or groups."""

    job_id: str
    clip_index: int = Field(ge=0)
    destination_ids: List[str] = Field(default_factory=list)
    group_ids: List[str] = Field(default_factory=list)
    # Restrict a group expansion to certain platforms, e.g. "this group, but
    # YouTube only".
    platforms: Optional[List[str]] = None

    title: Optional[str] = None
    caption: Optional[str] = None
    # {"youtube": {"title": ..., "caption": ..., "options": {...}}, ...}
    per_platform: Optional[dict] = None

    scheduled_for: Optional[datetime] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=120)

    @field_validator("scheduled_for")
    @classmethod
    def _aware(cls, v):
        if v is not None and v.tzinfo is None:
            from datetime import timezone
            # A naive datetime would be read as server-local and silently post
            # at the wrong hour.
            return v.replace(tzinfo=timezone.utc)
        return v


class PublishPreview(BaseModel):
    """Dry expansion: what WOULD be published, without creating anything."""

    destination_ids: List[str] = Field(default_factory=list)
    group_ids: List[str] = Field(default_factory=list)
    platforms: Optional[List[str]] = None
    job_id: Optional[str] = None
    clip_index: Optional[int] = None


class AttemptOut(BaseModel):
    id: str
    publish_destination_id: str
    publish_group_id: str
    platform: str
    provider: str
    attempt_number: int
    status: str
    provider_post_ref: Optional[str] = None
    permalink: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    deferred_until: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    destination_label: Optional[str] = None


class RequestOut(BaseModel):
    id: str
    job_id: str
    clip_index: int
    mode: str
    status: str
    scheduled_for: Optional[datetime] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    payload: Optional[dict] = None
    attempts: List[AttemptOut] = Field(default_factory=list)


class RetryIn(BaseModel):
    # Required to retry an `unknown` attempt. Named to make the risk explicit at
    # the call site: that post may already be live.
    force: bool = False


# --- Assignments ------------------------------------------------------------
class AssignmentCreate(BaseModel):
    publish_group_id: str
    job_id: str
    clip_index: int = Field(ge=0)
    scheduled_for: Optional[datetime] = None
    meta: Optional[dict] = None


class AssignmentOut(BaseModel):
    id: str
    publish_group_id: str
    job_id: str
    clip_index: int
    status: str
    scheduled_for: Optional[datetime] = None
    publish_request_id: Optional[str] = None
    created_at: Optional[datetime] = None


class AssignmentBulkCreate(BaseModel):
    """Earmark a job's clips for one group, spread over the posting window.

    ``max_clips`` has no default: how many clips a group posts per day is the
    operator's decision, and a number defaulted here would quietly become the
    system's ceiling.
    """

    job_id: str
    clip_count: int = Field(ge=0, le=1000)
    clip_indexes: Optional[List[int]] = None
    max_clips: Optional[int] = Field(default=None, ge=1)
    spacing_seconds: Optional[int] = Field(default=None, ge=60, le=86400)
    start_at: Optional[datetime] = None
    # Skip the daytime posting window and start immediately. Off by default: a
    # schedule that fires at 04:00 spends a scarce daily quota slot on the
    # platform's worst engagement hour.
    immediate: bool = False
    meta: Optional[dict] = None

    @field_validator("start_at")
    @classmethod
    def _aware_start(cls, v):
        if v is not None and v.tzinfo is None:
            from datetime import timezone
            return v.replace(tzinfo=timezone.utc)
        return v


class PublishJobIn(BaseModel):
    """Publish a whole job's clips — the autopilot plan shape, over HTTP."""

    job_id: str
    clip_count: int = Field(ge=0, le=1000)
    destination_ids: List[str] = Field(default_factory=list)
    group_ids: List[str] = Field(default_factory=list)
    platforms: Optional[List[str]] = None
    clip_indexes: Optional[List[int]] = None
    max_clips: Optional[int] = Field(default=None, ge=1)
    spacing_seconds: Optional[int] = Field(default=None, ge=60, le=86400)
    immediate: bool = False
    title: Optional[str] = None
    caption: Optional[str] = None
    per_platform: Optional[dict] = None
