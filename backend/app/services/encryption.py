import base64
import os
from cryptography.fernet import Fernet
from ..config import settings

_KEY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", ".encryption_key")


def generate_key() -> bytes:
    return Fernet.generate_key()


def _get_key() -> bytes:
    key = settings.encryption_key
    if not key:
        key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        # Auto-generate a key on first run and persist to file
        if os.path.exists(_KEY_FILE):
            with open(_KEY_FILE, "rb") as f:
                key = f.read().decode()
        else:
            key = Fernet.generate_key().decode()
            os.makedirs(os.path.dirname(_KEY_FILE), exist_ok=True)
            with open(_KEY_FILE, "w") as f:
                f.write(key)
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
