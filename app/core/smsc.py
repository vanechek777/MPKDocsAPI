"""SMS через SMSC.ru HTTP API (REST JSON). Документация: https://smsc.ru/api/"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SMSC_REST_SEND = "https://smsc.ru/rest/send/"

# Коды из https://smsc.ru/api/http/send/sms/sms_answer/ (fmt=3, JSON).
_SMSC_KNOWN_ERRORS: dict[int, str] = {
    1: "ошибка в параметрах запроса к SMSC",
    2: "неверный логин, пароль или API-ключ SMSC (или запрос с IP не из белого списка в кабинете)",
    3: "недостаточно средств на счёте SMSC",
    4: "IP временно заблокирован (много неверных запросов)",
    5: "неверный формат даты/времени в запросе",
    6: "сообщение запрещено (текст, имя отправителя или нужен договор для рассылки)",
    7: "неверный формат номера телефона",
    8: "сообщение на этот номер не может быть доставлено",
    9: "слишком частые одинаковые запросы (или много параллельных подключений)",
}


class SmscSendError(RuntimeError):
    pass


def _smsc_error_message(body: dict) -> str:
    raw_err = (body.get("error") or "").strip()
    code = body.get("error_code")
    try:
        icode = int(code) if code is not None else None
    except (TypeError, ValueError):
        icode = None

    hint = _SMSC_KNOWN_ERRORS.get(icode) if icode is not None else None
    if icode == 2 or "authoris" in raw_err.lower():
        return (
            "SMSC: ошибка авторизации (логин/пароль или API-ключ). "
            "Проверьте в .env: SMSC_API_KEY либо SMSC_LOGIN и SMSC_PASSWORD "
            "(значения из кабинета smsc.ru → «Пароли и авторизация»). "
            "Если в кабинете включён список разрешённых IP, добавьте IP сервера."
        )
    if icode == 6:
        return (
            "SMSC (6): «message is denied» — сообщение не принято шлюзом. Что попробовать: "
            "(1) Удалите SMSC_SENDER из .env, если отправитель не подтверждён в кабинете. "
            "(2) Поставьте латинский шаблон: SMSC_OTP_MESSAGE_TEMPLATE=Code: {code} "
            "и SMSC_OTP_TRANSLIT=1 при кириллице в шаблоне. "
            "(3) В кабинете smsc.ru → «Настройки» включите режим виртуальной отправки для теста без реальных SMS "
            "(см. раздел виртуальной отправки в документации API). "
            "(4) Напишите в поддержку SMSC (support@smsc.ru), что нужен OTP/сервисный текст, ошибка 6 — часто нужен допуск по типу отправки или договор. "
            f"Ответ шлюза: {raw_err or 'message is denied'}."
        )
    if hint:
        return f"SMSC ({icode}): {hint}. Ответ: {raw_err or '—'}"
    return f"SMSC: {raw_err or icode or body}"


def format_otp_sms_text(template: str, code: str) -> str:
    """Подставляет код в шаблон; использует только замену {code}, без str.format (безопаснее для .env)."""
    if "{code}" not in template:
        return f"Code: {code}"
    return template.replace("{code}", code)


def normalize_russian_phone_digits(phone: str) -> str | None:
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    if len(digits) == 11 and digits[0] == "7":
        return digits
    return None


def smsc_configured() -> bool:
    if (settings.smsc_api_key or "").strip():
        return True
    login = (settings.smsc_login or "").strip()
    psw = (settings.smsc_password or "").strip()
    return bool(login and psw)


async def send_sms_text(*, to_phone: str, text: str, translit: int | None = None) -> None:
    if not smsc_configured():
        raise SmscSendError("SMSC не настроен")

    phones = normalize_russian_phone_digits(to_phone)
    if not phones:
        raise SmscSendError("Неверный формат номера")

    payload: dict = {
        "phones": phones,
        "mes": text,
        "fmt": 3,
        "charset": "utf-8",
    }

    api_key = (settings.smsc_api_key or "").strip()
    if api_key:
        payload["apikey"] = api_key
    else:
        payload["login"] = (settings.smsc_login or "").strip()
        payload["psw"] = (settings.smsc_password or "").strip()

    sender = (settings.smsc_sender or "").strip()
    if sender:
        payload["sender"] = sender

    tlit = translit if translit is not None else 0
    if tlit in (1, 2):
        payload["translit"] = tlit

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
        try:
            r = await client.post(SMSC_REST_SEND, json=payload)
        except httpx.HTTPError as e:
            logger.exception("SMSC request failed")
            raise SmscSendError("Шлюз SMS недоступен") from e

    if r.status_code >= 400:
        logger.warning("SMSC HTTP %s: %s", r.status_code, r.text[:500])
        raise SmscSendError("Шлюз SMS отклонил запрос")

    try:
        body = r.json()
    except ValueError as e:
        logger.warning("SMSC non-JSON response: %s", r.text[:500])
        raise SmscSendError("Некорректный ответ шлюза") from e

    if not isinstance(body, dict):
        raise SmscSendError("Некорректный ответ шлюза")

    if "error" in body:
        ec = body.get("error_code")
        try:
            ec_int = int(ec) if ec is not None else None
        except (TypeError, ValueError):
            ec_int = None
        if ec_int == 6:
            logger.warning(
                "SMSC error 6: sender_in_request=%s text_preview=%s",
                bool(sender),
                text[:80] + ("..." if len(text) > 80 else ""),
            )
        raise SmscSendError(_smsc_error_message(body))

    if "id" not in body:
        raise SmscSendError(str(body or "SMS не принята"))
