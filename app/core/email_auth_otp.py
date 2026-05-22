"""Короткоживущие коды входа / подтверждения регистрации по email (in-process)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time

from app.core.config import settings
from app.core.otp import OtpCooldownError

EMAIL_GATE_TTL_SECONDS = 600
MIN_SEND_INTERVAL_SECONDS = 55

_lock = asyncio.Lock()
# normalized email -> (digest, expiry_unix, last_send_unix, user_id)
_login_by_email: dict[str, tuple[bytes, float, float, int]] = {}
# normalized email -> (digest, expiry_unix, last_send_unix, phone, full_name, password_hash)
_pending_register: dict[str, tuple[bytes, float, float, str, str, str]] = {}


def _digest(code_plain: str) -> bytes:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        ("email-login:" + code_plain).encode("utf-8"),
        hashlib.sha256,
    ).digest()


async def issue_email_login_code(normalized_email: str, user_id: int) -> str:
    now = time.time()
    normalized_email = normalized_email.strip().lower()
    async with _lock:
        prev = _login_by_email.get(normalized_email)
        if prev is not None:
            _, _, last_sent, _ = prev
            if now - last_sent < MIN_SEND_INTERVAL_SECONDS:
                raise OtpCooldownError()

        code = f"{secrets.randbelow(1_000_000):06d}"
        _login_by_email[normalized_email] = (
            _digest(code),
            now + EMAIL_GATE_TTL_SECONDS,
            now,
            user_id,
        )
    return code


async def verify_email_login_consume(normalized_email: str, code_plain: str) -> int | None:
    code_plain = (code_plain or "").strip()
    if not code_plain:
        return None
    normalized_email = normalized_email.strip().lower()
    now = time.time()
    async with _lock:
        entry = _login_by_email.get(normalized_email)
        if entry is None:
            return None
        digest, exp, _, user_id = entry
        if now > exp:
            del _login_by_email[normalized_email]
            return None
        if not hmac.compare_digest(digest, _digest(code_plain)):
            return None
        del _login_by_email[normalized_email]
        return user_id


def _digest_reg(code_plain: str) -> bytes:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        ("email-reg:" + code_plain).encode("utf-8"),
        hashlib.sha256,
    ).digest()


async def issue_register_email_code(
    normalized_email: str,
    *,
    phone: str,
    full_name: str,
    password_hash: str,
) -> str:
    now = time.time()
    normalized_email = normalized_email.strip().lower()
    async with _lock:
        prev = _pending_register.get(normalized_email)
        if prev is not None:
            _, _, last_sent, _, _, _ = prev
            if now - last_sent < MIN_SEND_INTERVAL_SECONDS:
                raise OtpCooldownError()

        code = f"{secrets.randbelow(1_000_000):06d}"
        _pending_register[normalized_email] = (
            _digest_reg(code),
            now + EMAIL_GATE_TTL_SECONDS,
            now,
            phone,
            full_name,
            password_hash,
        )
    return code


async def verify_register_email_consume(normalized_email: str, code_plain: str) -> tuple[str, str, str] | None:
    code_plain = (code_plain or "").strip()
    if not code_plain:
        return None
    normalized_email = normalized_email.strip().lower()
    now = time.time()
    async with _lock:
        entry = _pending_register.get(normalized_email)
        if entry is None:
            return None
        digest, exp, _, phone, full_name, password_hash = entry
        if now > exp:
            del _pending_register[normalized_email]
            return None
        if not hmac.compare_digest(digest, _digest_reg(code_plain)):
            return None
        del _pending_register[normalized_email]
        return (phone, full_name, password_hash)
