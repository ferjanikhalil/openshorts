"""HMAC verification and signed media tokens. Pure, no I/O.

Two independent uses of HMAC-SHA256 live here, both testable without a network:

1. **Inbound webhook verification.** The provider signs the RAW request body.
   Verification must therefore run on the exact bytes received — re-serializing
   the parsed JSON changes key order and whitespace and breaks the signature.

2. **Outbound media tokens.** The provider fetches media by URL, so the clip has
   to be reachable from the public internet. A signed, expiring, unguessable
   token is what makes that possible without exposing the whole media directory.
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Optional, Tuple

WEBHOOK_SIGNATURE_HEADER = "X-Webhook-Signature"
_SIG_PREFIX = "sha256="


def compute_webhook_signature(secret: str, raw_body: bytes) -> str:
    """``sha256=<hex>`` over the raw body, as Status 200 documents it."""
    mac = hmac.new(secret.encode(), raw_body, hashlib.sha256)
    return _SIG_PREFIX + mac.hexdigest()


def verify_webhook_signature(secret: str, raw_body: bytes,
                             presented: Optional[str]) -> bool:
    """Constant-time signature check. Tolerates a missing ``sha256=`` prefix.

    Note what this does NOT provide: the signed preimage is the body alone, with
    no timestamp and no nonce, so a captured request stays valid forever. Replay
    protection therefore cannot come from the signature — it comes from the
    ``provider_event_id`` UNIQUE constraint plus the ``created_at`` skew window
    in webhooks.py. Both are required; neither alone is sufficient.
    """
    if not secret or not presented:
        return False
    expected = compute_webhook_signature(secret, raw_body)
    candidates = [presented.strip()]
    if not presented.strip().startswith(_SIG_PREFIX):
        candidates.append(_SIG_PREFIX + presented.strip())
    return any(hmac.compare_digest(expected, c) for c in candidates)


def verify_webhook_signature_any_encoding(
        secret: str, raw_body: bytes, presented: Optional[str]) -> bool:
    """Same check, but accepting the digest in hex OR base64.

    For providers that document "HMAC-SHA256 of the raw body" and never say how
    it is encoded — Zernio is one. Guessing wrong is not a soft failure: every
    callback is rejected as unsigned, no post is ever confirmed, and each one ages
    into ``unknown`` for a human to resolve by hand.

    Accepting both encodings is not a weakening. Both candidates are derived from
    the same secret over the same preimage, so an attacker who can produce either
    can already produce the digest itself; only the transport spelling differs.
    Comparison is over a fixed candidate set with ``compare_digest``, so it stays
    constant-time per candidate.

    Kept out of ``verify_webhook_signature`` on purpose: Status 200's verification
    path is proven in production and does not change to accommodate a second
    provider. An adapter opts in.
    """
    if not secret or not presented:
        return False
    presented = presented.strip()
    if presented.lower().startswith(_SIG_PREFIX):
        presented = presented[len(_SIG_PREFIX):].strip()
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    candidates = [
        digest.hex(),
        base64.b64encode(digest).decode(),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    ]
    # Compare every candidate — no early exit — so the work is the same whichever
    # encoding the provider chose.
    matched = False
    for c in candidates:
        if hmac.compare_digest(c, presented):
            matched = True
    return matched


def within_skew(created_at_epoch: Optional[float], max_skew_seconds: int,
                now: Optional[float] = None) -> bool:
    """Bound how old a webhook may be.

    A missing timestamp returns True: the event is still gated by the signature
    and the uniqueness constraint, and rejecting un-timestamped events would drop
    legitimate deliveries if the provider omits the field.
    """
    if created_at_epoch is None:
        return True
    now = time.time() if now is None else now
    return abs(now - float(created_at_epoch)) <= max_skew_seconds


# --- Signed media tokens ----------------------------------------------------
def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_media_token(secret: str, job_id: str, clip_index: int, filename: str,
                     expires_at: int) -> str:
    """Mint an opaque, expiring, tamper-evident media token.

    The filename is inside the signed payload rather than a URL path segment, so
    a caller cannot walk to another file: changing any byte invalidates the MAC.
    That makes this a capability for exactly one clip, not a key to the media
    directory.
    """
    payload = {
        "j": job_id,
        "c": int(clip_index),
        "f": filename,
        "e": int(expires_at),
    }
    body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64u(mac[:16])}"


def verify_media_token(secret: str, token: str,
                       now: Optional[float] = None) -> Tuple[bool, dict, str]:
    """Return ``(ok, payload, reason)``. Never raises on malformed input."""
    if not secret or not token or "." not in token:
        return False, {}, "malformed"
    body, sig = token.rsplit(".", 1)
    expected = _b64u(hmac.new(secret.encode(), body.encode(),
                              hashlib.sha256).digest()[:16])
    if not hmac.compare_digest(expected, sig):
        return False, {}, "bad_signature"
    try:
        payload = json.loads(_b64u_decode(body).decode())
    except Exception:
        return False, {}, "malformed"
    now = time.time() if now is None else now
    if float(payload.get("e", 0)) < now:
        # Expiry is checked AFTER the MAC so an attacker learns nothing about
        # validity from an unsigned guess.
        return False, payload, "expired"
    return True, payload, ""
