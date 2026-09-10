"""Credential encryption at rest (spec 53, 54).

Provider API keys and SIP passwords are stored as ciphertext. Both planes need
this: the Control Plane encrypts on write, the worker decrypts at call start.

Fernet is used rather than raw AES because it is authenticated — a tampered
ciphertext fails to decrypt instead of yielding garbage that gets sent to a
provider as a credential. Key versions are recorded alongside each value so a
key can be rotated without re-encrypting everything in one transaction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionError(RuntimeError):
    """Encryption or decryption failed."""


class MissingEncryptionKeyError(CredentialEncryptionError):
    """No key is configured.

    Raised rather than silently falling back to plaintext. A platform that
    stores credentials unencrypted because a variable was unset is worse than
    one that refuses to start.
    """


class KeyVersionUnknownError(CredentialEncryptionError):
    """A stored value names a key version this process does not hold.

    Happens when a key is rotated and old keys are dropped too early. The
    ciphertext is intact; the key to read it is gone.
    """


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    """Ciphertext plus the key version that produced it."""

    ciphertext: bytes
    key_version: int


class CredentialCipher:
    """Encrypts and decrypts credentials.

    Holds the current key for writing, plus any retired keys still needed to
    read existing rows.
    """

    def __init__(self, keys: dict[int, str], current_version: int) -> None:
        if not keys:
            raise MissingEncryptionKeyError("no encryption keys were provided")
        if current_version not in keys:
            raise MissingEncryptionKeyError(
                f"current key version {current_version} is not among the provided keys"
            )

        try:
            self._ciphers = {version: Fernet(key.encode()) for version, key in keys.items()}
        except (ValueError, TypeError) as exc:
            raise CredentialEncryptionError(f"invalid encryption key: {exc}") from exc

        self._current_version = current_version

    @classmethod
    def from_env(cls) -> CredentialCipher:
        """Build from the environment.

        ``CREDENTIAL_ENCRYPTION_KEY`` is the current key.
        ``CREDENTIAL_ENCRYPTION_KEYS_RETIRED`` optionally holds
        ``version:key`` pairs, comma separated, for values written before the
        last rotation.
        """
        current = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
        if not current:
            raise MissingEncryptionKeyError(
                "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )

        current_version = int(os.getenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "1"))
        keys = {current_version: current}

        retired = os.getenv("CREDENTIAL_ENCRYPTION_KEYS_RETIRED", "").strip()
        for entry in (e for e in retired.split(",") if e.strip()):
            version_text, _, key = entry.partition(":")
            if not key:
                raise CredentialEncryptionError(
                    f"retired key entry {entry!r} is not in version:key form"
                )
            keys[int(version_text)] = key.strip()

        return cls(keys, current_version)

    @property
    def current_version(self) -> int:
        return self._current_version

    def encrypt(self, plaintext: str) -> EncryptedValue:
        """Encrypt with the current key."""
        if not plaintext:
            raise CredentialEncryptionError("refusing to encrypt an empty credential")
        token = self._ciphers[self._current_version].encrypt(plaintext.encode())
        return EncryptedValue(ciphertext=token, key_version=self._current_version)

    def decrypt(self, ciphertext: bytes, key_version: int) -> str:
        """Decrypt a stored value."""
        cipher = self._ciphers.get(key_version)
        if cipher is None:
            raise KeyVersionUnknownError(
                f"no key for version {key_version}; available: {sorted(self._ciphers)}"
            )
        try:
            return cipher.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            # Authenticated encryption, so this means the stored bytes were
            # altered or encrypted under a different key — not that the
            # credential is merely wrong.
            raise CredentialEncryptionError(
                f"credential failed authentication under key version {key_version}; "
                "the stored value may be corrupt or encrypted with another key"
            ) from exc

    def needs_rotation(self, key_version: int) -> bool:
        """Whether a value should be re-encrypted under the current key."""
        return key_version != self._current_version

    @staticmethod
    def hint(plaintext: str, *, length: int = 4) -> str:
        """Last few characters, for showing which key is configured.

        Lets the UI identify a credential without decrypting it (spec 54).
        """
        return plaintext[-length:] if len(plaintext) > length else "*" * len(plaintext)


def generate_key() -> str:
    """Generate a new Fernet key."""
    return Fernet.generate_key().decode()
