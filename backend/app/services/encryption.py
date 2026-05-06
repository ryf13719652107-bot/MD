import base64
import os
from cryptography.fernet import Fernet
from ..config import settings


def generate_key() -> bytes:
    return Fernet.generate_key()


def _get_key() -> bytes:
    key = settings.encryption_key
    if not key:
        key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        raise ValueError("ENCRYPTION_KEY is not set. Run generate_key() to create one.")
    return key.encode() if isinstance(key, str) else key


def encrypt(plaintext: str) -> str:
    f = Fernet(_get_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    f = Fernet(_get_key())
    return f.decrypt(token.encode()).decode()


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]
