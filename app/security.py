from __future__ import annotations

import base64
import hashlib
import secrets
import time
import re
from collections import defaultdict, deque
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_login_attempts: dict[str, deque[float]] = defaultdict(deque)


class SecretBox:
    def __init__(self, key: str | bytes):
        if not key:
            raise ValueError("ENCRYPTION_KEY must be configured")
        raw = key.encode() if isinstance(key, str) else key
        # Accept Fernet key directly; derive a deterministic key for a sufficiently long passphrase.
        try:
            self._fernet = Fernet(raw)
        except (ValueError, TypeError):
            derived = hashlib.sha256(raw).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Stored secret cannot be decrypted; verify ENCRYPTION_KEY") from exc


box = SecretBox(settings.encryption_key) if settings.encryption_key else None


def encrypt_secret(value: str) -> str:
    if box is None:
        raise RuntimeError("Credential encryption is unavailable; set ENCRYPTION_KEY")
    return box.encrypt(value)


def decrypt_secret(value: str) -> str:
    if box is None:
        raise RuntimeError("Credential encryption is unavailable; set ENCRYPTION_KEY")
    return box.decrypt(value)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _password_hasher.verify(hashed, password)
    except (VerifyMismatchError, ValueError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def valid_csrf(session: dict[str, Any], token: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and token and secrets.compare_digest(str(expected), str(token)))


def login_allowed(client_ip: str, now: float | None = None, limit: int = 8, window: int = 300) -> bool:
    current = now or time.time()
    attempts = _login_attempts[client_ip]
    while attempts and current - attempts[0] > window:
        attempts.popleft()
    return len(attempts) < limit


def record_login_failure(client_ip: str, now: float | None = None) -> None:
    _login_attempts[client_ip].append(now or time.time())


def clear_login_failures(client_ip: str) -> None:
    _login_attempts.pop(client_ip, None)


def redact_sensitive(value: object, limit: int = 1500) -> str:
    """Remove signed URLs, bearer values, and common credential assignments from errors."""
    text = str(value)
    text = re.sub(r"https?://[^\s\"'<>]+", "[URL REDACTED]", text)
    text = re.sub(r"(?i)(api[-_ ]?key|client_secret|refresh_token|access_token|authorization)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", text)
    return text[:limit]
