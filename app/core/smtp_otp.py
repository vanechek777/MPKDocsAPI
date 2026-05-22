"""Отправка кода подписания по SMTP (в т.ч. Яндекс: smtp.yandex.ru)."""

from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


class SmtpSendError(RuntimeError):
    pass


def smtp_configured() -> bool:
    u = (settings.smtp_user or "").strip()
    p = (settings.smtp_password or "").strip()
    h = (settings.smtp_host or "").strip()
    return bool(h and u and p)


def _from_address() -> str:
    return (settings.smtp_from or settings.smtp_user or "").strip()


def build_otp_email_plain_and_html(code: str) -> tuple[str, str]:
    """Текстовая и HTML-версия письма с кодом (для multipart/alternative)."""
    raw = (code or "").strip()
    safe_html = html.escape(raw, quote=True)
    plain = (
        "Здравствуйте,\n\n"
        f"Ваш код для входа в МПК.Документы: {raw}\n\n"
        "Код действует 10 минут. Если вы не запрашивали вход, просто проигнорируйте письмо.\n\n"
        "Поддержка: mpkchita.ru"
    )
    body_html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"></head>
<body>
<p>Здравствуйте,</p>
<p>Ваш код для входа в <strong>МПК.Документы</strong>:</p>
<h2 style="letter-spacing:6px;font:700 24px system-ui">{safe_html}</h2>
<p>Код действует 10 минут. Если вы не запрашивали вход, просто проигнорируйте письмо.</p>
<p style="color:#666;font-size:12px">Поддержка: mpkchita.ru</p>
</body>
</html>"""
    return plain, body_html


def build_registration_otp_email_plain_and_html(code: str) -> tuple[str, str]:
    """Текст для письма с кодом подтверждения регистрации."""
    raw = (code or "").strip()
    safe_html = html.escape(raw, quote=True)
    plain = (
        "Здравствуйте,\n\n"
        f"Ваш код для завершения регистрации в МПК.Документы: {raw}\n\n"
        "Код действует 10 минут. Если вы не создавали аккаунт, проигнорируйте письмо.\n\n"
        "Поддержка: mpkchita.ru"
    )
    body_html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"></head>
<body>
<p>Здравствуйте,</p>
<p>Ваш код для <strong>завершения регистрации</strong> в МПК.Документы:</p>
<h2 style="letter-spacing:6px;font:700 24px system-ui">{safe_html}</h2>
<p>Код действует 10 минут. Если вы не создавали аккаунт, проигнорируйте письмо.</p>
<p style="color:#666;font-size:12px">Поддержка: mpkchita.ru</p>
</body>
</html>"""
    return plain, body_html


async def send_otp_email(
    *,
    to_addr: str,
    subject: str,
    body_plain: str,
    body_html: str | None = None,
) -> None:
    if not smtp_configured():
        raise SmtpSendError("SMTP не настроен")

    to_addr = to_addr.strip()
    mail_from = _from_address()
    if not mail_from:
        raise SmtpSendError("Задайте EMAIL_HOST_USER или DEFAULT_FROM_EMAIL (или SMTP_USER / SMTP_FROM)")

    host = settings.smtp_host.strip()
    port = int(settings.smtp_port)
    user = settings.smtp_user.strip()
    password = settings.smtp_password
    use_ssl = settings.smtp_use_ssl

    def _send_sync() -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = mail_from
        msg["To"] = to_addr
        msg.set_content(body_plain, charset="utf-8")
        if body_html:
            msg.add_alternative(body_html, subtype="html")

        ctx = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=ctx)
                smtp.login(user, password)
                smtp.send_message(msg)

    try:
        await asyncio.to_thread(_send_sync)
    except smtplib.SMTPException as e:
        logger.warning("SMTP error: %s", e)
        raise SmtpSendError(str(e)) from e
    except OSError as e:
        logger.warning("SMTP connection error: %s", e)
        raise SmtpSendError(str(e)) from e
