from __future__ import annotations

import os
import base64
from typing import List
from cryptography.fernet import Fernet, MultiFernet


def _load_keys() -> List[bytes]:
    """
    Load Fernet keys from env. Supports rotation via ENCRYPTION_KEYS (comma-separated).
    Falls back to ENCRYPTION_KEY. If none set, generates an ephemeral key (dev only).
    """
    raw = (os.getenv("ENCRYPTION_KEYS") or os.getenv("ENCRYPTION_KEY") or "").strip()
    keys: List[bytes] = []
    if raw:
        for part in raw.split(","):
            k = part.strip()
            if k:
                keys.append(k.encode("utf-8"))

    if not keys:
        # Dev fallback: ephemeral key so the app can boot locally.
        eph = base64.urlsafe_b64encode(os.urandom(32))
        keys = [eph]
        # Also set env so subsequent imports use the same key this process.
        os.environ["ENCRYPTION_KEY"] = eph.decode("utf-8")
        os.environ["ENCRYPTION_KEYS"] = os.environ["ENCRYPTION_KEY"]
    return keys


def get_fernet():
    keys = _load_keys()
    fernets = [Fernet(k) for k in keys]
    return MultiFernet(fernets) if len(fernets) > 1 else fernets[0]


def encrypt(plaintext: str) -> str:
    """
    Encrypt plaintext to a Fernet token (URL-safe base64 string).
    """
    f = get_fernet()
    token = f.encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(token: str) -> str:
    """
    Decrypt a Fernet token back to plaintext.
    """
    f = get_fernet()
    data = f.decrypt(token.encode("utf-8"))
    return data.decode("utf-8")
