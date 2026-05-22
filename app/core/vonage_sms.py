"""SMS через Vonage (Nexmo) SMS API. Документация: https://developer.vonage.com/en/messaging/sms/overview"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.core.smsc import normalize_russian_phone_digits

logger = logging.getLogger(__name__)

VONAGE_SMS_JSON = "https://rest.nexmo.com/sms/json"


class VonageSendError(RuntimeError):
    pass


def vonage_configured() -> bool:
    k = (settings.vonage_api_key or "").strip()
    s = (settings.vonage_api_secret or "").strip()
    f = (settings.vonage_from or "").strip()
    return bool(k and s and f)


def _to_international_digits(phone: str) -> str | None:
    s = (phone or "").strip()
    if s.startswith("+"):
        d = "".join(c for c in s[1:] if c.isdigit())
        return d if len(d) >= 10 else None
    d = normalize_russian_phone_digits(phone)
    if d:
        return d
    digits = "".join(c for c in phone if c.isdigit())
    return digits if len(digits) >= 10 else None


async def send_sms_vonage(*, to_phone: str, text: str) -> None:
    if not vonage_configured():
        raise VonageSendError("Vonage не настроен")

    to_d = _to_international_digits(to_phone)
    if not to_d:
        raise VonageSendError("Неверный формат номера")

    data: dict[str, str] = {
        "api_key": settings.vonage_api_key.strip(),
        "api_secret": settings.vonage_api_secret.strip(),
        "to": to_d,
        "from": settings.vonage_from.strip(),
        "text": text,
    }
    if any(ord(c) > 127 for c in text):
        data["type"] = "unicode"

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
        try:
            r = await client.post(VONAGE_SMS_JSON, data=data)
        except httpx.HTTPError as e:
            logger.exception("Vonage SMS request failed")
            raise VonageSendError("Vonage недоступен") from e

    if r.status_code >= 400:
        logger.warning("Vonage HTTP %s: %s", r.status_code, r.text[:500])
        raise VonageSendError(f"Vonage HTTP {r.status_code}")

    try:
        body = r.json()
    except ValueError as e:
        logger.warning("Vonage non-JSON: %s", r.text[:500])
        raise VonageSendError("Некорректный ответ Vonage") from e

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise VonageSendError(str(body))

    m0 = messages[0]
    status = str(m0.get("status", ""))
    if status != "0":
        err = m0.get("error-text") or m0.get("network") or str(m0)
        raise VonageSendError(f"Vonage: {err} (status={status})")
