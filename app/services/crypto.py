# app/services/crypto.py
from __future__ import annotations

import os
from typing import List
from cryptography.fernet import Fernet, MultiFernet


# ───────────────────────── Key loading ─────────────────────────

def _raw_keys_from_env() -> List[str]:
    """
    Reads keys from env, preferring ENCRYPTION_KEYS (comma-separated),
    else falling back to ENCRYPTION_KEY.
    """
    many = os.getenv("ENCRYPTION_KEYS")
    if many and many.strip():
        return [s.strip() for s in many.split(",") if s.strip()]

    single = os.getenv("ENCRYPTION_KEY")
    return [single.strip()] if single and single.strip() else []


def _load_keys() -> List[bytes]:
    """
    Validate & return Fernet keys as bytes. Raises if none are configured.
    """
    raw = _raw_keys_from_env()
    if not raw:
        raise RuntimeError(
            "No ENCRYPTION_KEY(S) configured. Generate one:\n"
            "  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'\n"
            "Then set ENCRYPTION_KEY (or ENCRYPTION_KEYS) in your .env"
        )

    out: List[bytes] = []
    for s in raw:
        key = s.encode("utf-8")
        # Validate by constructing a Fernet instance
        Fernet(key)
        out.append(key)
    return out


# Primary first, older keys after (for rotation)
_F = MultiFernet([Fernet(k) for k in _load_keys()])


# ───────────────────────── Public API ─────────────────────────

def encrypt(plain: str | bytes | None) -> str:
    """
    Encrypts a value to a Fernet token (str).
    None -> "" so we don't store the string "None".
    """
    if plain is None:
        return ""
    if isinstance(plain, str):
        data = plain.encode("utf-8")
    elif isinstance(plain, bytes):
        data = plain
    else:
        data = str(plain).encode("utf-8")
    return _F.encrypt(data).decode("utf-8")


def decrypt(token: str | bytes | None) -> str:
    """
    Decrypts a Fernet token back to utf-8 text.
    Empty/None -> "" (idempotent for optional fields).
    """
    if token is None or token == "":
        return ""
    if isinstance(token, str):
        t = token.encode("utf-8")
    elif isinstance(token, bytes):
        t = token
    else:
        raise TypeError("token must be str or bytes")
    return _F.decrypt(t).decode("utf-8")


def rotate(token: str | bytes) -> str:
    """
    Re-encrypt an existing token with the primary key (useful after key rotation).
    """
    if isinstance(token, str):
        t = token.encode("utf-8")
    elif isinstance(token, bytes):
        t = token
    else:
        raise TypeError("token must be str or bytes")
    return _F.rotate(t).decode("utf-8")
