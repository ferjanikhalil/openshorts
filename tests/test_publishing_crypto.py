"""Tests for credential sealing: AES-256-GCM, AAD binding, rotation, masking.

The keys below are constant byte patterns generated in-process. They are test
fixtures, not secrets, and nothing here ever touches a real provider key — the
plaintexts are `rl_`-shaped strings so the scrubbing patterns are exercised on
something that looks like the real thing without being one.

`publishing.config.Settings` reads the environment on every attribute access, so
pointing the module at a different master key is just monkeypatch.setenv — no
reload, no cache to invalidate.
"""
import base64

import pytest

# The only third-party dependency in the publishing package. Skipping keeps a
# machine without it able to run the rest of the suite.
pytest.importorskip("cryptography")

from cryptography.exceptions import InvalidTag  # noqa: E402

from publishing import crypto  # noqa: E402
from publishing.config import decode_master_key  # noqa: E402

KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(reversed(range(32)))).decode()

SECRET = "rl_live_9f2c4b7a1d8e6350"
AAD = "publish_credentials:api_key:7c9f1e2a-0000-4000-8000-000000000001"


@pytest.fixture
def key_a(monkeypatch):
    monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_A)
    monkeypatch.delenv("PUBLISHING_MASTER_KEY_OLD", raising=False)


class TestKeyVersion:
    def test_derived_and_stable(self):
        raw = decode_master_key(KEY_A)
        assert crypto.key_version(raw) == crypto.key_version(raw)
        assert len(crypto.key_version(raw)) == 12

    def test_different_keys_get_different_versions(self):
        # Two deploys sharing a key must agree on the label; two different keys
        # must not, or rotation could not tell the blobs apart.
        assert crypto.key_version(decode_master_key(KEY_A)) != \
            crypto.key_version(decode_master_key(KEY_B))

    def test_version_is_not_the_key(self):
        raw = decode_master_key(KEY_A)
        assert base64.b64encode(raw).decode() not in crypto.key_version(raw)


class TestRoundTrip:
    def test_encrypt_then_decrypt(self, key_a):
        blob = crypto.encrypt(SECRET, aad=AAD)
        assert crypto.decrypt(blob, aad=AAD).reveal() == SECRET

    def test_aad_defaults_to_the_stored_one(self, key_a):
        # The dispatcher decrypts without restating the AAD; the blob carries it.
        blob = crypto.encrypt(SECRET, aad=AAD)
        assert crypto.decrypt(blob).reveal() == SECRET

    def test_no_column_carries_the_plaintext(self, key_a):
        blob = crypto.encrypt(SECRET, aad=AAD)
        for field, value in blob.items():
            assert SECRET not in str(value), f"{field} leaked the plaintext"

    def test_stored_columns_are_exactly_what_the_model_expects(self, key_a):
        blob = crypto.encrypt(SECRET, aad=AAD)
        assert set(blob) == {"key_version", "nonce_b64", "ciphertext_b64",
                             "aad", "fingerprint", "last4"}
        assert blob["last4"] == SECRET[-4:]

    def test_nonce_is_never_reused(self, key_a):
        blobs = [crypto.encrypt(SECRET, aad=AAD) for _ in range(20)]
        nonces = {b["nonce_b64"] for b in blobs}
        assert len(nonces) == 20
        # Same plaintext, same key, different ciphertext — that is the point of
        # the random nonce.
        assert len({b["ciphertext_b64"] for b in blobs}) == 20

    def test_nonce_is_96_bit(self, key_a):
        blob = crypto.encrypt(SECRET, aad=AAD)
        assert len(base64.b64decode(blob["nonce_b64"])) == 12

    def test_empty_secret_is_refused(self, key_a):
        # Storing an empty credential would produce a row that looks configured
        # and fails at publish time with a 401.
        with pytest.raises(ValueError):
            crypto.encrypt("", aad=AAD)


class TestTampering:
    def test_wrong_aad_does_not_decrypt(self, key_a):
        # A webhook-secret blob must not open as an api_key.
        blob = crypto.encrypt(SECRET, aad="publish_credentials:webhook_secret:x")
        with pytest.raises(InvalidTag):
            crypto.decrypt(blob, aad=AAD)

    def test_flipped_ciphertext_does_not_decrypt(self, key_a):
        blob = crypto.encrypt(SECRET, aad=AAD)
        raw = bytearray(base64.b64decode(blob["ciphertext_b64"]))
        raw[0] ^= 0x01
        blob["ciphertext_b64"] = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(InvalidTag):
            crypto.decrypt(blob, aad=AAD)

    def test_swapped_nonce_does_not_decrypt(self, key_a):
        a = crypto.encrypt(SECRET, aad=AAD)
        b = crypto.encrypt(SECRET, aad=AAD)
        a["nonce_b64"] = b["nonce_b64"]
        with pytest.raises(InvalidTag):
            crypto.decrypt(a, aad=AAD)


class TestRotation:
    def test_old_key_stays_readable_during_the_window(self, monkeypatch):
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_A)
        monkeypatch.delenv("PUBLISHING_MASTER_KEY_OLD", raising=False)
        blob = crypto.encrypt(SECRET, aad=AAD)

        # Rotate: B becomes current, A is demoted but still loaded.
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_B)
        monkeypatch.setenv("PUBLISHING_MASTER_KEY_OLD", KEY_A)
        assert crypto.decrypt(blob, aad=AAD).reveal() == SECRET
        assert crypto.needs_rotation(blob) is True

    def test_new_writes_use_the_current_key(self, monkeypatch):
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_B)
        monkeypatch.setenv("PUBLISHING_MASTER_KEY_OLD", KEY_A)
        fresh = crypto.encrypt(SECRET, aad=AAD)
        assert fresh["key_version"] == crypto.key_version(decode_master_key(KEY_B))
        assert crypto.needs_rotation(fresh) is False

    def test_dropping_the_old_key_raises_something_actionable(self, monkeypatch):
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_A)
        monkeypatch.delenv("PUBLISHING_MASTER_KEY_OLD", raising=False)
        blob = crypto.encrypt(SECRET, aad=AAD)

        # Window closed too early: the blob is unreadable. It must fail loudly
        # rather than yield an empty Authorization header.
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_B)
        with pytest.raises(RuntimeError) as exc:
            crypto.decrypt(blob, aad=AAD)
        assert "PUBLISHING_MASTER_KEY_OLD" in str(exc.value)

    def test_unversioned_blob_is_rejected(self, key_a):
        with pytest.raises(RuntimeError):
            crypto.decrypt({"key_version": "", "nonce_b64": "", "ciphertext_b64": ""})


class TestFingerprintAndMasking:
    def test_fingerprint_is_stable_and_not_reversible(self):
        fp = crypto.fingerprint(SECRET)
        assert fp == crypto.fingerprint(SECRET)
        assert len(fp) == 32
        assert SECRET not in fp

    def test_fingerprint_survives_rotation(self, monkeypatch):
        # It is derived from the plaintext alone, so "is this the same key I
        # already configured?" keeps working across a master-key rotation.
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_A)
        a = crypto.encrypt(SECRET, aad=AAD)["fingerprint"]
        monkeypatch.setenv("PUBLISHING_MASTER_KEY", KEY_B)
        assert crypto.encrypt(SECRET, aad=AAD)["fingerprint"] == a

    def test_different_secrets_fingerprint_differently(self):
        assert crypto.fingerprint(SECRET) != crypto.fingerprint(SECRET + "x")

    def test_masked_shows_prefix_and_last4_only(self):
        m = crypto.masked(SECRET)
        assert m.startswith(SECRET[:6]) and m.endswith(SECRET[-4:])
        # The middle — the part that makes the key usable — is gone.
        assert SECRET[6:-4] not in m

    def test_masked_short_value_reveals_no_prefix(self):
        assert crypto.masked("abcd1234") == "…1234"

    def test_masked_empty(self):
        assert crypto.masked("") == ""
        assert crypto.last4("abc") == ""


class TestScrub:
    def test_masks_a_provider_key_in_free_text(self):
        line = f"POST /api/v2/posts failed: Authorization: Bearer {SECRET}"
        out = crypto.scrub(line)
        assert SECRET not in out
        assert "redacted" in out

    def test_masks_every_occurrence(self):
        out = crypto.scrub(f"{SECRET} and rl_test_abcdefgh12345678")
        assert "rl_live_9f2c4b7a1d8e6350" not in out
        assert "rl_test_abcdefgh12345678" not in out

    def test_leaves_ordinary_text_alone(self):
        text = "clip 3 published to instagram in 4.2s"
        assert crypto.scrub(text) == text

    def test_handles_empty_and_none(self):
        assert crypto.scrub("") == ""
        assert crypto.scrub(None) is None


class TestSecretStr:
    def test_never_reveals_itself_by_accident(self):
        s = crypto.SecretStr(SECRET)
        assert SECRET not in repr(s)
        assert SECRET not in str(s)
        assert SECRET not in f"{s}"
        assert SECRET not in "{}".format(s)
        assert SECRET not in "%s" % (s,)

    def test_reveal_is_the_only_way_out(self):
        assert crypto.SecretStr(SECRET).reveal() == SECRET

    def test_truthiness_and_length_without_revealing(self):
        assert bool(crypto.SecretStr(SECRET)) is True
        assert bool(crypto.SecretStr("")) is False
        assert len(crypto.SecretStr(SECRET)) == len(SECRET)

    def test_equality_is_constant_time_and_typed(self):
        assert crypto.SecretStr(SECRET) == crypto.SecretStr(SECRET)
        assert crypto.SecretStr(SECRET) != crypto.SecretStr("rl_other_00000000")
        # Comparing against a bare str returns NotImplemented -> falls back to
        # identity, so a stray `secret == "..."` can never accidentally pass.
        assert not (crypto.SecretStr(SECRET) == SECRET)

    def test_has_no_dict_to_leak(self):
        # __slots__: no instance __dict__ for a debugger/serializer to dump.
        with pytest.raises(AttributeError):
            crypto.SecretStr(SECRET).__dict__


class TestMasterKeyValidation:
    def test_wrong_length_is_rejected_with_instructions(self):
        short = base64.b64encode(b"tooshort").decode()
        with pytest.raises(RuntimeError) as exc:
            decode_master_key(short)
        assert "32 bytes" in str(exc.value)
        assert "base64" in str(exc.value)

    def test_non_base64_is_rejected_with_instructions(self):
        with pytest.raises(RuntimeError) as exc:
            decode_master_key("not base64 at all!!")
        assert "base64" in str(exc.value)

    def test_a_generated_key_is_accepted(self):
        import os
        assert len(decode_master_key(
            base64.b64encode(os.urandom(32)).decode())) == 32


class TestCredentialAad:
    """The AAD string itself, pinned.

    ``_credential_aad`` builds what GCM authenticates, so its output is a storage
    format, not an implementation detail: every credential already in a database
    was sealed against the exact bytes this function returned on the day it was
    entered. Changing the formula for an existing case makes those rows
    undecryptable and unrecoverable without re-entry, so the strings are asserted
    literally rather than round-tripped.
    """

    GROUP = "7c9f1e2a-0000-4000-8000-000000000001"

    @pytest.fixture(autouse=True)
    def _aad(self):
        pytest.importorskip("fastapi")
        pytest.importorskip("sqlalchemy")
        from publishing.admin_api import _credential_aad
        self.aad = _credential_aad

    def test_the_legacy_unslotted_string_is_unchanged_byte_for_byte(self):
        """Every pre-slots credential was sealed against exactly this.

        Appending ``|slot:None`` unconditionally when slots were added would have
        changed the AAD of every existing row; GCM would refuse to open them and
        each would surface at dispatch as ``credential_unreadable`` —
        indistinguishable from a rotated master key, for keys that are perfectly
        fine.
        """
        assert self.aad(self.GROUP, "api_key", "status200") == (
            f"group:{self.GROUP}|kind:api_key|provider:status200")

    def test_an_explicit_none_slot_is_the_same_string_as_no_slot(self):
        # The group default is the unslotted credential — same question, and it
        # must not seal differently depending on which caller asked.
        assert self.aad(self.GROUP, "api_key", "zernio", None) == \
            self.aad(self.GROUP, "api_key", "zernio")

    def test_an_empty_or_whitespace_slot_is_also_the_default(self):
        # A blank form field must not create a credential nothing can resolve.
        assert self.aad(self.GROUP, "api_key", "zernio", "") == \
            self.aad(self.GROUP, "api_key", "zernio")
        assert self.aad(self.GROUP, "api_key", "zernio", "   ") == \
            self.aad(self.GROUP, "api_key", "zernio")

    def test_a_named_slot_appends_exactly_one_segment(self):
        assert self.aad(self.GROUP, "api_key", "zernio", "zernio-b") == (
            f"group:{self.GROUP}|kind:api_key|provider:zernio|slot:zernio-b")

    def test_each_dimension_binds_independently(self):
        base = self.aad(self.GROUP, "api_key", "zernio", "zernio-a")
        assert base != self.aad(self.GROUP, "webhook_secret", "zernio",
                                "zernio-a")
        assert base != self.aad(self.GROUP, "api_key", "status200", "zernio-a")
        assert base != self.aad(self.GROUP, "api_key", "zernio", "zernio-b")
        assert base != self.aad("00000000-0000-4000-8000-000000000002",
                                "api_key", "zernio", "zernio-a")

    def test_a_key_moved_between_slots_in_the_database_will_not_open(self, key_a):
        """What the slot segment actually buys.

        A group now holds several provider accounts. Without the slot in the AAD,
        moving a ciphertext from one slot's row to another's would decrypt
        cleanly and publish to the wrong account under the wrong key.
        """
        sealed = crypto.encrypt(
            SECRET, aad=self.aad(self.GROUP, "api_key", "zernio", "zernio-a"))
        moved = dict(sealed,
                     aad=self.aad(self.GROUP, "api_key", "zernio", "zernio-b"))
        with pytest.raises(InvalidTag):
            crypto.decrypt(moved)
        # ...and still opens in its own slot.
        assert crypto.decrypt(sealed).reveal() == SECRET

    def test_a_slotted_blob_cannot_pose_as_the_group_default(self, key_a):
        sealed = crypto.encrypt(
            SECRET, aad=self.aad(self.GROUP, "api_key", "zernio", "zernio-a"))
        with pytest.raises(InvalidTag):
            crypto.decrypt(dict(sealed,
                                aad=self.aad(self.GROUP, "api_key", "zernio")))
