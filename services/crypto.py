import os
from cryptography.fernet import Fernet

def _fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY not set. Generate via: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    return Fernet(key.encode() if not key.endswith("=") else key)

def encrypt(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()

def decrypt(s: str) -> str:
    return _fernet().decrypt(s.encode()).decode()
