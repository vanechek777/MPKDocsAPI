"""Транзакционные уведомления по email о документе (SMTP через smtp_otp.send_otp_email)."""

from __future__ import annotations

import asyncio
import html
import logging

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.smtp_otp import SmtpSendError, send_otp_email, smtp_configured
from app.db.models import Document, DocumentContent, DocumentStep, DocumentTask, User
from app.db.session import SessionLocal
from app.services.signing_turn import active_pending_step_by_document

_logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return settings.document_notify_email_enabled and smtp_configured()


async def _document_title(db: AsyncSession, document_id: int) -> str:
    row = (
        await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == document_id))
    ).scalar_one_or_none()
    if isinstance(row, dict):
        for key in ("fileName", "title"):
            fn = row.get(key)
            if isinstance(fn, str) and fn.strip():
                return fn.strip()
    return f"Документ #{document_id}"


async def pending_tasks_on_active_step(db: AsyncSession, document_id: int) -> list[DocumentTask]:
    """Только задачи активного по очереди шага со статусом PENDING."""
    active_by = await active_pending_step_by_document(db, [document_id])
    active_ord = active_by.get(document_id)
    if active_ord is None:
        return []

    stmt = (
        select(DocumentTask)
        .join(DocumentStep, DocumentStep.id == DocumentTask.StepId)
        .where(
            DocumentTask.DocumentId == document_id,
            DocumentTask.Status == "PENDING",
            DocumentStep.StepOrder == active_ord,
        )
    )
    return list((await db.execute(stmt)).scalars().unique().all())


async def _collect_recipient_emails_for_task(db: AsyncSession, t: DocumentTask) -> list[tuple[str, str]]:
    """Пары (email, display_name_hint) без дублей адресов."""
    out: dict[str, str] = {}

    uid = t.AssignedUserId
    if uid is not None:
        u = (
            await db.execute(select(User).where(User.id == int(uid)))
        ).scalar_one_or_none()
        if u is not None:
            addr = (u.Email or "").strip()
            if addr:
                nm = ((u.FullName or "").strip() or f"#{u.id}")
                out[addr.lower()] = nm
        return list(out.items())

    dept_id = t.AssignedDepartmentId
    if dept_id is not None:
        rows = (
            await db.execute(
                select(User).where(
                    and_(
                        User.DepartmentId == int(dept_id),
                        User.Email.is_not(None),
                        User.Status.is_not(False),
                    )
                )
            )
        ).scalars().all()
        for u in rows:
            addr = (u.Email or "").strip()
            if not addr:
                continue
            nm = ((u.FullName or "").strip() or f"#{u.id}")
            out.setdefault(addr.lower(), nm)
    return list(out.items())


def _build_multipart(*, subject: str, lines_plain: list[str], lines_html: list[str]) -> tuple[str, str]:
    plain = "\n".join(lines_plain) + "\n"
    safe_html_blocks = "".join(f"<p>{html.escape(x, quote=True)}</p>" for x in lines_html)
    body_html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;font-size:15px;color:#111">
{safe_html_blocks}
<p style="color:#666;font-size:12px">МПК Документы</p>
</body></html>"""
    return plain, body_html


async def _send_safe(to_addr: str, subject: str, plain: str, body_html: str) -> None:
    try:
        await send_otp_email(to_addr=to_addr, subject=subject, body_plain=plain, body_html=body_html)
    except SmtpSendError as e:
        _logger.warning("document notify SMTP fail to=%s: %s", to_addr, e)


async def notify_turn_opened(document_id: int) -> None:
    """Ваш ход подписать: всем адресатам активного шага (персонально или отдел)."""
    if not _enabled():
        return
    try:
        async with SessionLocal() as db:
            doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
            if doc is None:
                return
            st = (doc.Status or "").upper()
            if st in {"DRAFT", "SIGNED", "REJECTED", "CANCELLED"}:
                return

            initiator = (
                await db.execute(select(User).where(User.id == doc.InitiatorId))
            ).scalar_one_or_none()
            initiator_name = ((initiator.FullName or "").strip() if initiator else "") or "Инициатор"
            title = await _document_title(db, document_id)

            tasks = await pending_tasks_on_active_step(db, document_id)

            recipients: dict[str, str] = {}
            for t in tasks:
                for addr, nm in await _collect_recipient_emails_for_task(db, t):
                    recipients.setdefault(addr, nm)

            if not recipients:
                return

            subj = "Документ на подписание — МПК Документы"
            for email_addr in recipients.keys():
                lines_p = [
                    "Здравствуйте,",
                    "",
                    f'Вам направили документ «{title}» (№ {document_id}) на подписание.',
                    f"Отправитель: {initiator_name}.",
                    "",
                    "Откройте приложение «МПК Документы» или веб-клиент, чтобы подписать или отклонить документ.",
                ]
                lines_h = lines_p[:-1] + ["Откройте приложение или веб-клиент, чтобы подписать или отклонить документ."]
                plain, body_html = _build_multipart(subject=subj, lines_plain=lines_p, lines_html=lines_h)
                await _send_safe(email_addr, subj, plain, body_html)
    except Exception:
        _logger.exception("notify_turn_opened failed doc_id=%s", document_id)


async def notify_initiator_progress(
    document_id: int,
    actor_user_id: int,
    *,
    event: str,
    document_status: str | None,
) -> None:
    """Инициатору: кто-то подписал или отклонил (event: signed|rejected)."""
    if not _enabled():
        return
    try:
        async with SessionLocal() as db:
            doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
            if doc is None:
                return
            initiator = (
                await db.execute(select(User).where(User.id == doc.InitiatorId))
            ).scalar_one_or_none()
            if initiator is None:
                return
            to = (initiator.Email or "").strip()
            if not to:
                return

            if int(actor_user_id) == int(doc.InitiatorId):
                return

            actor = (
                await db.execute(select(User).where(User.id == int(actor_user_id)))
            ).scalar_one_or_none()
            actor_name = ((actor.FullName or "").strip() if actor else "") or f"Пользователь #{actor_user_id}"
            title = await _document_title(db, document_id)
            st = (document_status or doc.Status or "").upper()

            if event == "rejected":
                subj = f"Документ отклонён — {title}"
                lines_p = [
                    "Здравствуйте,",
                    "",
                    f'Документ «{title}» (№ {document_id}) отклонён.',
                    f"Отклонил: {actor_name}.",
                ]
            elif st == "SIGNED":
                subj = f"Документ полностью подписан — {title}"
                lines_p = [
                    "Здравствуйте,",
                    "",
                    f'Документ «{title}» (№ {document_id}) полностью подписан.',
                    f"Последнее действие выполнил: {actor_name}.",
                ]
            else:
                subj = f"Документ подписан — {title}"
                lines_p = [
                    "Здравствуйте,",
                    "",
                    f'{actor_name} подписал документ «{title}» (№ {document_id}).',
                    "Ожидайте следующих подписантов или откройте карточку документа в системе.",
                ]

            plain, body_html = _build_multipart(subject=subj, lines_plain=lines_p, lines_html=lines_p)
            await _send_safe(to, subj, plain, body_html)
    except Exception:
        _logger.exception("notify_initiator_progress failed doc_id=%s", document_id)


def fire_notify_turn(document_id: int) -> None:
    asyncio.create_task(notify_turn_opened(document_id))


def fire_notify_initiator(document_id: int, actor_user_id: int, *, event: str, document_status: str | None) -> None:
    asyncio.create_task(notify_initiator_progress(document_id, actor_user_id, event=event, document_status=document_status))
