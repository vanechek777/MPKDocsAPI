"""Short-lived signing OTP codes (in-process; ok for single worker / dev)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time

from app.core.config import settings

OTP_TTL_SECONDS = 300
MIN_SEND_INTERVAL_SECONDS = 55


class OtpCooldownError(Exception):
    pass


_lock = asyncio.Lock()
_entries: dict[int, tuple[bytes, float, float]] = {}
"""user_id -> (code_hmac_digest, expiry_unix, last_send_unix)."""


def _digest(code_plain: str) -> bytes:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        code_plain.encode("utf-8"),
        hashlib.sha256,
    ).digest()


async def issue_code_for_user(user_id: int) -> str:
    now = time.time()
    async with _lock:
        prev = _entries.get(user_id)
        if prev is not None:
            _, _, last_sent = prev
            if now - last_sent < MIN_SEND_INTERVAL_SECONDS:
                raise OtpCooldownError()

        code = f"{secrets.randbelow(1_000_000):06d}"
        _entries[user_id] = (_digest(code), now + OTP_TTL_SECONDS, now)
    return code


async def verify_and_consume(user_id: int, code_plain: str) -> bool:
    if not code_plain or not code_plain.strip():
        return False
    code_plain = code_plain.strip()
    now = time.time()
    async with _lock:
        entry = _entries.get(user_id)
        if entry is None:
            return False
        digest, exp, _ = entry
        if now > exp:
            del _entries[user_id]
            return False
        if not hmac.compare_digest(digest, _digest(code_plain)):
            return False
        del _entries[user_id]
        return True
