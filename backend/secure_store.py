"""Application-level encryption for workspace provider credentials.

The encryption key must be supplied through CREDENTIAL_ENCRYPTION_KEY (a
32-byte urlsafe base64 Fernet key). It is deliberately never stored in Mongo.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key") from exc


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Stored credential could not be decrypted") from exc
