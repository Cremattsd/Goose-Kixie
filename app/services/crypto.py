# app/services/crypto.py
import os
from typing import List, Iterable, Optional
from cryptography.fernet import Fernet, MultiFernet

ENV_KEYS = ("ENCRYPTION_KEYS", "ENCRYPTION_KEY")  # prefer ENCRYPTION_KEYS (comma-separated)

def _normalize_key(k: str) -> bytes:
    """
    Accepts a base64url Fernet key as a string. Returns bytes.
    Strips quotes/whitespace; always encodes to bytes.
    """
    if not isinstance(k, str):
        raise TypeError("Fernet key must be a string")
    k = k.strip().strip('"').strip("'")
    if not k:
        raise ValueError("Empty Fernet key")
    return k.encode("utf-8")

def _load_keys() -> List[bytes]:
    """
    Load keys from ENCRYPTION_KEYS (comma-separated) or ENCRYPTION_KEY (single).
    First key = primary (used for encrypt). All keys used for decrypt/rotate.
    """
    raw = None
    for name in ENV_KEYS:
        v = os.getenv(name)
        if v:
            raw = v
            break
    if not raw:
        raise RuntimeError(
            "ENCRYPTION_KEYS/ENCRYPTION_KEY not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    parts = [p for p in (x.strip() for x in raw.split(",")) if p]
    keys = [_normalize_key(p) for p in parts]
    # Validate by instantiating (raises if invalid)
    for kb in keys:
        Fernet(kb)
    return keys

def _multi() -> MultiFernet:
    keys = _load_keys()
    fernets = [Fernet(k) for k in keys]
    return MultiFernet(fernets)

def _primary() -> Fernet:
    # First key in ENCRYPTION_KEYS is the primary for encryption
    k = _load_keys()[0]
    return Fernet(k)

# ---------- Public API ----------

def generate_key() -> str:
    """Convenience: create a new Fernet key (base64url string)."""
    return Fernet.generate_key().decode()

def encrypt_str(s: str) -> str:
    """Encrypt a UTF-8 string to a Fernet token (string)."""
    return _primary().encrypt(s.encode("utf-8")).decode("utf-8")

def decrypt_str(token: str) -> str:
    """Decrypt a Fernet token (string) to a UTF-8 string."""
    return _multi().decrypt(token.encode("utf-8")).decode("utf-8")

def encrypt_bytes(b: bytes) -> bytes:
    return _primary().encrypt(b)

def decrypt_bytes(token: bytes) -> bytes:
    return _multi().decrypt(token)

def rotate_token(token: str) -> str:
    """
    Re-encrypt a token with the current primary key while still accepting old keys.
    Useful when you add a new key to ENCRYPTION_KEYS (as the first one).
    """
    rotated = _multi().rotate(token.encode("utf-8"))
    return rotated.decode("utf-8")

# Optional helpers for dict fields
def encrypt_fields(data: dict, fields: Iterable[str]) -> dict:
    out = dict(data)
    for f in fields:
        if f in out and out[f] is not None:
            out[f] = encrypt_str(str(out[f]))
    return out

def decrypt_fields(data: dict, fields: Iterable[str]) -> dict:
    out = dict(data)
    for f in fields:
        if f in out and isinstance(out[f], str):
            try:
                out[f] = decrypt_str(out[f])
            except Exception:
                # Leave as-is if it's not a valid token
                pass
    return out
