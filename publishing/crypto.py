"""Envelope encryption for provider credentials.

Provider API keys are the one truly dangerous secret in this subsystem: a leaked
Status 200 key can post to a real audience under the user's identity. So:

  - Plaintext exists only inside a request handler or a submit call. It is never
    logged, never returned to the frontend, never written outside the encrypted
    column.
  - AES-256-GCM with a random 96-bit nonce per encryption. GCM is authenticated,
    so a tampered ciphertext fails to decrypt rather than yielding garbage.
  - The AAD binds the ciphertext to its purpose, so a webhook secret blob cannot
    be swapped into an api_key row and decrypt cleanly.
  - Rotation is by-insert: every blob records the ``key_version`` that sealed it,
    and ``PUBLISHING_MASTER_KEY_OLD`` stays readable during the window, so
    rotating never requires a stop-the-world re-encrypt.

The only things ever shown to a human are ``fingerprint`` and ``last4``, which is
enough to answer "is this the key I pasted?" and nothing more.
"""
import base64
import hashlib
import hmac
import os
import re
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import decode_master_key, settings

# Status 200 keys are `rl_`-prefixed. Any log line that would carry one gets it
# masked — app.py's _scrub_secrets gains this pattern too, so a key pasted into a
# job log or an httpx error string cannot survive to disk.
SECRET_PATTERNS = [
    re.compile(r'\brl_[A-Za-z0-9_\-]{8,}'),
]


def scrub(text: str) -> str:
    """Mask any provider-key-shaped token in arbitrary text."""
    if not text:
        return text
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0)[:6] + "…redacted", text)
    return text


class SecretStr:
    """A string that refuses to reveal itself in a repr, log, or traceback.

    Wrapping plaintext in this makes accidental exposure loud: you have to call
    ``.reveal()`` on purpose, and that call site is greppable.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value or ""

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other) -> bool:
        if isinstance(other, SecretStr):
            return hmac.compare_digest(self._value, other._value)
        return NotImplemented

    def __hash__(self):
        return hash(("SecretStr", self._value))

    def __repr__(self) -> str:
        return "SecretStr(***)"

    __str__ = __repr__

    def __format__(self, spec) -> str:
        return "***"


def key_version(key: bytes) -> str:
    """Short, stable id for a master key. Not secret — it is a label, and it is
    derived (not configured) so two deploys with the same key agree on it."""
    return hashlib.sha256(b"openshorts-publishing-kv\x00" + key).hexdigest()[:12]


def _keys() -> dict:
    """{key_version: key_bytes} for every master key currently readable."""
    out = {}
    cur = decode_master_key(settings.master_key_b64)
    out[key_version(cur)] = cur
    if settings.master_key_old_b64:
        old = decode_master_key(settings.master_key_old_b64)
        out.setdefault(key_version(old), old)
    return out


def _current_key():
    cur = decode_master_key(settings.master_key_b64)
    return key_version(cur), cur


def fingerprint(plaintext: str) -> str:
    """Stable non-reversible id for a secret value.

    Lets an admin confirm two rows hold the same key, and lets the API answer
    "already configured" without ever comparing plaintext across requests.
    """
    return hashlib.sha256(b"openshorts-publishing-fp\x00"
                          + plaintext.encode()).hexdigest()[:32]


def last4(plaintext: str) -> str:
    return plaintext[-4:] if len(plaintext) >= 4 else ""


def masked(plaintext: str) -> str:
    """Human-readable stand-in, e.g. ``rl_live_…a91f``. Safe for the UI."""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "…" + last4(plaintext)
    return plaintext[:6] + "…" + last4(plaintext)


def encrypt(plaintext: str, *, aad: str = "") -> dict:
    """Seal a secret. Returns the columns to store — no plaintext among them."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty secret")
    kv, key = _current_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), aad.encode() if aad else None)
    return {
        "key_version": kv,
        "nonce_b64": base64.b64encode(nonce).decode(),
        "ciphertext_b64": base64.b64encode(ct).decode(),
        "aad": aad or "",
        "fingerprint": fingerprint(plaintext),
        "last4": last4(plaintext),
    }


def decrypt(blob: dict, *, aad: Optional[str] = None) -> SecretStr:
    """Open a sealed secret, accepting either the current or the old key.

    Raises on an unknown key version rather than returning None: a credential
    that cannot be decrypted must surface as a config error at the call site, not
    as a silently empty Authorization header.
    """
    kv = blob.get("key_version") or ""
    keys = _keys()
    key = keys.get(kv)
    if key is None:
        raise RuntimeError(
            f"credential was encrypted with master key version '{kv}', which is not "
            "loaded. Set PUBLISHING_MASTER_KEY_OLD to the previous key to read it, "
            "or re-enter the credential in the admin UI."
        )
    nonce = base64.b64decode(blob["nonce_b64"])
    ct = base64.b64decode(blob["ciphertext_b64"])
    use_aad = blob.get("aad", "") if aad is None else aad
    pt = AESGCM(key).decrypt(nonce, ct, use_aad.encode() if use_aad else None)
    return SecretStr(pt.decode())


def needs_rotation(blob: dict) -> bool:
    """True when a stored blob is sealed under a superseded key."""
    kv, _ = _current_key()
    return (blob.get("key_version") or "") != kv
