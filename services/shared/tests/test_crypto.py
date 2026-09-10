"""Tests for credential encryption (spec 53, 54)."""

from __future__ import annotations

import pytest

from shared.crypto import (
    CredentialCipher,
    CredentialEncryptionError,
    KeyVersionUnknownError,
    MissingEncryptionKeyError,
    generate_key,
)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher({1: generate_key()}, current_version=1)


class TestRoundTrip:
    def test_encrypt_then_decrypt_returns_the_original(self, cipher: CredentialCipher) -> None:
        encrypted = cipher.encrypt("sk-super-secret-key")
        assert cipher.decrypt(encrypted.ciphertext, encrypted.key_version) == "sk-super-secret-key"

    def test_ciphertext_does_not_contain_the_plaintext(self, cipher: CredentialCipher) -> None:
        encrypted = cipher.encrypt("sk-super-secret-key")
        assert b"super-secret" not in encrypted.ciphertext

    def test_same_plaintext_encrypts_differently_each_time(self, cipher: CredentialCipher) -> None:
        """Deterministic ciphertext would let an observer tell that two tenants
        configured the same key."""
        first = cipher.encrypt("same-value").ciphertext
        second = cipher.encrypt("same-value").ciphertext
        assert first != second

    def test_key_version_is_recorded(self, cipher: CredentialCipher) -> None:
        assert cipher.encrypt("value").key_version == 1


class TestTamperDetection:
    def test_modified_ciphertext_is_rejected(self, cipher: CredentialCipher) -> None:
        """Authenticated encryption: a tampered value must fail, not decrypt to
        garbage that then gets sent to a provider as a credential."""
        encrypted = cipher.encrypt("sk-secret")
        tampered = bytearray(encrypted.ciphertext)
        tampered[-1] ^= 0x01

        with pytest.raises(CredentialEncryptionError, match="failed authentication"):
            cipher.decrypt(bytes(tampered), encrypted.key_version)

    def test_a_different_key_cannot_decrypt(self) -> None:
        first = CredentialCipher({1: generate_key()}, current_version=1)
        second = CredentialCipher({1: generate_key()}, current_version=1)
        encrypted = first.encrypt("sk-secret")

        with pytest.raises(CredentialEncryptionError):
            second.decrypt(encrypted.ciphertext, encrypted.key_version)


class TestKeyRotation:
    def test_a_retired_key_still_reads_old_values(self) -> None:
        """Rotation must not require re-encrypting everything in one transaction."""
        old_key, new_key = generate_key(), generate_key()

        before = CredentialCipher({1: old_key}, current_version=1)
        encrypted = before.encrypt("sk-written-before-rotation")

        after = CredentialCipher({1: old_key, 2: new_key}, current_version=2)
        assert after.decrypt(encrypted.ciphertext, 1) == "sk-written-before-rotation"
        assert after.encrypt("new").key_version == 2

    def test_dropping_a_key_too_early_is_reported_clearly(self) -> None:
        old_key, new_key = generate_key(), generate_key()
        encrypted = CredentialCipher({1: old_key}, current_version=1).encrypt("sk-old")

        only_new = CredentialCipher({2: new_key}, current_version=2)
        with pytest.raises(KeyVersionUnknownError, match="no key for version 1"):
            only_new.decrypt(encrypted.ciphertext, 1)

    def test_needs_rotation_flags_stale_values(self) -> None:
        cipher = CredentialCipher({1: generate_key(), 2: generate_key()}, current_version=2)
        assert cipher.needs_rotation(1) is True
        assert cipher.needs_rotation(2) is False


class TestConfigurationErrors:
    def test_no_keys_is_refused(self) -> None:
        """Falling back to plaintext because a variable is unset would be worse
        than refusing to start."""
        with pytest.raises(MissingEncryptionKeyError):
            CredentialCipher({}, current_version=1)

    def test_current_version_must_be_present(self) -> None:
        with pytest.raises(MissingEncryptionKeyError, match="not among the provided keys"):
            CredentialCipher({1: generate_key()}, current_version=2)

    def test_invalid_key_material_is_reported(self) -> None:
        with pytest.raises(CredentialEncryptionError, match="invalid encryption key"):
            CredentialCipher({1: "not-a-valid-fernet-key"}, current_version=1)

    def test_missing_environment_key_names_the_fix(self, monkeypatch) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        with pytest.raises(MissingEncryptionKeyError, match="Fernet.generate_key"):
            CredentialCipher.from_env()

    def test_retired_keys_are_loaded_from_the_environment(self, monkeypatch) -> None:
        old_key, new_key = generate_key(), generate_key()
        encrypted = CredentialCipher({1: old_key}, current_version=1).encrypt("sk-old")

        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", new_key)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "2")
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS_RETIRED", f"1:{old_key}")

        cipher = CredentialCipher.from_env()
        assert cipher.current_version == 2
        assert cipher.decrypt(encrypted.ciphertext, 1) == "sk-old"

    def test_malformed_retired_entry_is_reported(self, monkeypatch) -> None:
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", generate_key())
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS_RETIRED", "missing-colon")
        with pytest.raises(CredentialEncryptionError, match="version:key"):
            CredentialCipher.from_env()

    def test_empty_credential_is_refused(self, cipher: CredentialCipher) -> None:
        with pytest.raises(CredentialEncryptionError, match="empty credential"):
            cipher.encrypt("")


class TestHint:
    def test_hint_shows_only_the_tail(self) -> None:
        assert CredentialCipher.hint("sk-abcdefgh1234") == "1234"

    def test_short_values_are_fully_masked(self) -> None:
        """A hint must never reveal a short credential in its entirety."""
        assert CredentialCipher.hint("abc") == "***"
