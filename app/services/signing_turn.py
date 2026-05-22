"""Очередь подписания по StepOrder: активна минимальная среди PENDING; подписать можно только на этом шаге."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocumentStep, DocumentTask, User


def user_task_match(user: User):
    return or_(
        DocumentTask.AssignedUserId == user.id,
        and_(
            DocumentTask.AssignedUserId.is_(None),
            DocumentTask.AssignedDepartmentId.is_not(None),
            user.DepartmentId is not None,
            DocumentTask.AssignedDepartmentId == user.DepartmentId,
        ),
    )


async def active_pending_step_by_document(db: AsyncSession, document_ids: list[int]) -> dict[int, int]:
    """Для каждого документа — минимальный StepOrder среди задач со статусом PENDING."""
    if not document_ids:
        return {}
    rows = (
        await db.execute(
            select(DocumentTask.DocumentId, DocumentStep.StepOrder)
            .join(DocumentStep, DocumentStep.id == DocumentTask.StepId)
            .where(
                DocumentTask.DocumentId.in_(document_ids),
                DocumentTask.Status == "PENDING",
            )
        )
    ).all()
    out: dict[int, int] = {}
    for did, so in rows:
        d, s = int(did), int(so)
        if d not in out or s < out[d]:
            out[d] = s
    return out


async def user_pending_steps_by_document(db: AsyncSession, user: User, document_ids: list[int]) -> dict[int, list[int]]:
    if not document_ids:
        return {}
    rows = (
        await db.execute(
            select(DocumentTask.DocumentId, DocumentStep.StepOrder)
            .join(DocumentStep, DocumentStep.id == DocumentTask.StepId)
            .where(
                DocumentTask.DocumentId.in_(document_ids),
                DocumentTask.Status == "PENDING",
                user_task_match(user),
            )
        )
    ).all()
    out: dict[int, list[int]] = defaultdict(list)
    for did, so in rows:
        out[int(did)].append(int(so))
    return {k: v for k, v in out.items()}


async def select_actionable_pending_task(db: AsyncSession, document_id: int, user: User) -> DocumentTask | None:
    active_by = await active_pending_step_by_document(db, [document_id])
    active = active_by.get(document_id)
    if active is None:
        return None
    return (
        await db.execute(
            select(DocumentTask)
            .join(DocumentStep, DocumentStep.id == DocumentTask.StepId)
            .where(
                DocumentTask.DocumentId == document_id,
                DocumentTask.Status == "PENDING",
                DocumentStep.StepOrder == active,
                user_task_match(user),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def waiting_for_others_signers(
    doc_id: int,
    initiator_id: int,
    user_id: int,
    active_by: dict[int, int],
    user_pending: dict[int, list[int]],
) -> bool:
    if int(initiator_id) == int(user_id):
        return False
    steps = user_pending.get(doc_id, [])
    if not steps:
        return False
    ap = active_by.get(doc_id)
    if ap is None:
        return False
    return ap not in steps
