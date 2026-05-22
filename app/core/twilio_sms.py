"""Отправка SMS через Twilio REST API (глобально). Документация: https://www.twilio.com/docs/sms/api"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.smsc import normalize_russian_phone_digits

logger = logging.getLogger(__name__)


class TwilioSendError(RuntimeError):
    pass


def twilio_configured() -> bool:
    sid = (settings.twilio_account_sid or "").strip()
    token = (settings.twilio_auth_token or "").strip()
    from_n = (settings.twilio_from_number or "").strip()
    return bool(sid and token and from_n)


def _to_e164(phone: str) -> str | None:
    s = (phone or "").strip()
    if s.startswith("+"):
        d = "".join(c for c in s[1:] if c.isdigit())
        if len(d) >= 10:
            return "+" + d
        return None
    d = normalize_russian_phone_digits(phone)
    if d:
        return "+" + d
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 10:
        return "+" + digits
    return None


async def send_sms_twilio(*, to_phone: str, text: str) -> None:
    if not twilio_configured():
        raise TwilioSendError("Twilio не настроен")

    to_e164 = _to_e164(to_phone)
    if not to_e164:
        raise TwilioSendError("Неверный формат номера")

    sid = settings.twilio_account_sid.strip()
    token = settings.twilio_auth_token.strip()
    from_num = settings.twilio_from_number.strip()

    url = f"https://api.twilio.com/2010-04-01/Accounts/{quote(sid, safe='')}/Messages.json"

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        try:
            r = await client.post(
                url,
                auth=(sid, token),
                data={"To": to_e164, "From": from_num, "Body": text},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            logger.exception("Twilio request failed")
            raise TwilioSendError("Twilio недоступен") from e

    if r.status_code not in (200, 201):
        detail = _parse_twilio_error(r)
        logger.warning("Twilio HTTP %s: %s", r.status_code, detail)
        raise TwilioSendError(detail)


def _parse_twilio_error(r: httpx.Response) -> str:
    try:
        data = r.json()
        msg = data.get("message")
        code = data.get("code")
        try:
            icode = int(code) if code is not None else None
        except (TypeError, ValueError):
            icode = None
        # https://www.twilio.com/docs/api/errors/21408
        if icode == 21408:
            return (
                "Twilio (21408): в консоли не включена отправка SMS в страну номера получателя. "
                "Откройте Twilio Console → Messaging → Settings → **Geo Permissions** "
                "(или раздел про географию SMS) и **включите нужную страну** (для +7 — Россию). "
                "У новых аккаунтов часто по умолчанию разрешён только свой регион. "
                "Документация: https://www.twilio.com/docs/api/errors/21408"
            )
        if msg:
            return f"Twilio: {msg}" + (f" (код {code})" if code is not None else "")
    except Exception:
        pass
    return r.text[:500] or f"HTTP {r.status_code}"
