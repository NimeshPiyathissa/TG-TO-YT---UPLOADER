"""Fernet encryption for tokens/secrets at rest.

The key lives at ``FERNET_KEY_PATH`` (mode 0400, owned by the ``tgyt`` service
user) rather than in the env file, so it survives an env-file leak and can be
backed up independently of the database (see README).
"""
from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

from bot.config import settings

_fernet: Fernet | None = None


def _load_key() -> bytes:
    path = Path(settings.fernet_key_path)
    if not path.exists():
        raise SystemExit(
            f"FATAL: Fernet key not found at {path}. The installer generates this on "
            "first run; if this is a manual setup, create it with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            f"and write it to {path} (mode 0400)."
        )
    return path.read_bytes().strip()


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(plaintext: str) -> bytes:
    return _get().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    return _get().decrypt(ciphertext).decode("utf-8")
