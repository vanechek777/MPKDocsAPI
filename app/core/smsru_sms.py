"""SMS через SMS.RU (российский сервис, доставка на +7). https://sms.ru/docs/api/api_group_sms/send"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.core.smsc import normalize_russian_phone_digits

logger = logging.getLogger(__name__)

SMSRU_SEND_URL = "https://sms.ru/sms/send"


class SmsRuSendError(RuntimeError):
    pass


def smsru_configured() -> bool:
    return bool((settings.smsru_api_id or "").strip())


def _to_ru_digits(phone: str) -> str | None:
    s = (phone or "").strip()
    if s.startswith("+"):
        d = "".join(c for c in s[1:] if c.isdigit())
        if len(d) >= 10:
            return d
        return None
    return normalize_russian_phone_digits(phone)


async def send_sms_smsru(*, to_phone: str, text: str) -> None:
    if not smsru_configured():
        raise SmsRuSendError("SMS.RU: не задан SMSRU_API_ID")

    to_d = _to_ru_digits(to_phone)
    if not to_d or len(to_d) < 10:
        raise SmsRuSendError("Неверный формат номера")

    data: dict[str, str] = {
        "api_id": settings.smsru_api_id.strip(),
        "to": to_d,
        "msg": text,
        "json": "1",
    }
    fn = (settings.smsru_from or "").strip()
    if fn:
        data["from"] = fn

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
        try:
            r = await client.post(SMSRU_SEND_URL, data=data)
        except httpx.HTTPError as e:
            logger.exception("SMS.RU request failed")
            raise SmsRuSendError("SMS.RU недоступен") from e

    if r.status_code >= 400:
        logger.warning("SMS.RU HTTP %s: %s", r.status_code, r.text[:500])
        raise SmsRuSendError(f"SMS.RU HTTP {r.status_code}")

    try:
        body = r.json()
    except ValueError as e:
        logger.warning("SMS.RU non-JSON: %s", r.text[:500])
        raise SmsRuSendError("Некорректный ответ SMS.RU") from e

    if not isinstance(body, dict):
        raise SmsRuSendError(str(body))

    if body.get("status") != "OK":
        msg = body.get("status_text") or body.get("status_code") or body
        raise SmsRuSendError(f"SMS.RU: {msg}")

    sms_block = body.get("sms")
    if not isinstance(sms_block, dict) or not sms_block:
        raise SmsRuSendError(str(body))

    sub = sms_block.get(to_d)
    if sub is None and len(sms_block) == 1:
        sub = next(iter(sms_block.values()))
    if not isinstance(sub, dict):
        raise SmsRuSendError(str(sms_block))

    if sub.get("status") != "OK":
        msg = sub.get("status_text") or sub.get("status_code") or sub
        raise SmsRuSendError(f"SMS.RU: {msg}")
