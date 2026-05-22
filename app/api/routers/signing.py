from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import Document, DocumentContent, DocumentTask, DocumentUserView, User
from app.services.signing_turn import active_pending_step_by_document, user_pending_steps_by_document, waiting_for_others_signers
from app.db.session import get_db

router = APIRouter(prefix="/signing", tags=["signing"])


class SigningListItem(BaseModel):
    document_id: int
    title: str
    status: str | None
    received_at: datetime | None
    signed_at: datetime | None = None
    initiator_name: str
    signed_count: int
    total_signers: int
    has_viewed: bool = False
    waiting_for_other_signers: bool = False
    recipients_viewed: int = 0
    recipients_total: int = 0


@router.get("/inbox", response_model=list[SigningListItem])
async def inbox(
    for_date: date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    total_sq = (
        select(DocumentTask.DocumentId, func.count(DocumentTask.id).label("total"))
        .group_by(DocumentTask.DocumentId)
        .subquery()
    )
    signed_sq = (
        select(DocumentTask.DocumentId, func.count(DocumentTask.id).label("signed"))
        .where(DocumentTask.Status == "SIGNED")
        .group_by(DocumentTask.DocumentId)
        .subquery()
    )

    stmt = (
        select(
            Document.id.label("document_id"),
            Document.InitiatorId.label("initiator_id"),
            Document.Status,
            Document.CreatedAt,
            User.FullName.label("initiator_name"),
            func.coalesce(signed_sq.c.signed, 0).label("signed_count"),
            func.coalesce(total_sq.c.total, 0).label("total_signers"),
        )
        .join(DocumentTask, DocumentTask.DocumentId == Document.id)
        .join(User, User.id == Document.InitiatorId)
        .outerjoin(total_sq, total_sq.c.DocumentId == Document.id)
        .outerjoin(signed_sq, signed_sq.c.DocumentId == Document.id)
        .where(
            and_(
                DocumentTask.Status == "PENDING",
                Document.InitiatorId != user.id,
                or_(
                    DocumentTask.AssignedUserId == user.id,
                    and_(
                        DocumentTask.AssignedUserId.is_(None),
                        DocumentTask.AssignedDepartmentId.is_not(None),
                        DocumentTask.AssignedDepartmentId == user.DepartmentId,
                    ),
                ),
            )
        )
        .order_by(desc(Document.CreatedAt))
    )

    if for_date:
        stmt = stmt.where(func.date(Document.CreatedAt) == for_date)

    rows = (await db.execute(stmt)).all()

    doc_ids = [int(r.document_id) for r in rows]
    viewed_ids: set[int] = set()
    if doc_ids:
        vr = (
            await db.execute(
                select(DocumentUserView.DocumentId).where(
                    and_(DocumentUserView.UserId == user.id, DocumentUserView.DocumentId.in_(doc_ids))
                )
            )
        ).all()
        viewed_ids = {int(x[0]) for x in vr}

    contents = []
    if doc_ids:
        contents = (
            await db.execute(
                select(DocumentContent.DocumentId, DocumentContent.DataJson).where(DocumentContent.DocumentId.in_(doc_ids))
            )
        ).all()
    file_by_doc: dict[int, str] = {}
    for doc_id, data in contents:
        try:
            if isinstance(data, dict):
                fn = data.get("fileName")
                if isinstance(fn, str) and fn.strip():
                    file_by_doc[int(doc_id)] = fn.strip()
        except Exception:
            pass

    active_by = await active_pending_step_by_document(db, doc_ids) if doc_ids else {}
    user_pending = await user_pending_steps_by_document(db, user, doc_ids) if doc_ids else {}

    return [
        SigningListItem(
            document_id=r.document_id,
            title=file_by_doc.get(int(r.document_id), f"Документ #{r.document_id}"),
            status=r.Status,
            received_at=r.CreatedAt,
            initiator_name=r.initiator_name,
            signed_count=int(r.signed_count or 0),
            total_signers=int(r.total_signers or 0),
            has_viewed=(int(r.document_id) in viewed_ids),
            waiting_for_other_signers=waiting_for_others_signers(
                int(r.document_id),
                int(r.initiator_id),
                int(user.id),
                active_by,
                user_pending,
            ),
            recipients_viewed=0,
            recipients_total=0,
        )
        for r in rows
    ]


@router.get("/signed", response_model=list[SigningListItem])
async def signed_by_me(
    for_date: date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(
            Document.id.label("document_id"),
            Document.Status,
            Document.CreatedAt,
            User.FullName.label("initiator_name"),
            DocumentTask.ProcessedAt.label("signed_at"),
        )
        .join(DocumentTask, DocumentTask.DocumentId == Document.id)
        .join(User, User.id == Document.InitiatorId)
        .where(
            and_(
                DocumentTask.ProcessedByUserId == user.id,
                DocumentTask.Status == "SIGNED",
                Document.Status != "CANCELLED",
            )
        )
        .order_by(desc(DocumentTask.ProcessedAt))
    )
    if for_date:
        stmt = stmt.where(func.date(Document.CreatedAt) == for_date)

    rows = (await db.execute(stmt)).all()
    doc_ids = [int(r.document_id) for r in rows]
    viewed_ids: set[int] = set()
    if doc_ids:
        vr = (
            await db.execute(
                select(DocumentUserView.DocumentId).where(
                    and_(DocumentUserView.UserId == user.id, DocumentUserView.DocumentId.in_(doc_ids))
                )
            )
        ).all()
        viewed_ids = {int(x[0]) for x in vr}

    contents = []
    if doc_ids:
        contents = (
            await db.execute(
                select(DocumentContent.DocumentId, DocumentContent.DataJson).where(DocumentContent.DocumentId.in_(doc_ids))
            )
        ).all()
    file_by_doc: dict[int, str] = {}
    for doc_id, data in contents:
        try:
            if isinstance(data, dict):
                fn = data.get("fileName")
                if isinstance(fn, str) and fn.strip():
                    file_by_doc[int(doc_id)] = fn.strip()
        except Exception:
            pass

    return [
        SigningListItem(
            document_id=r.document_id,
            title=file_by_doc.get(int(r.document_id), f"Документ #{r.document_id}"),
            status=r.Status,
            received_at=r.CreatedAt,
            signed_at=r.signed_at,
            initiator_name=r.initiator_name,
            signed_count=0,
            total_signers=0,
            has_viewed=(int(r.document_id) in viewed_ids),
            waiting_for_other_signers=False,
            recipients_viewed=0,
            recipients_total=0,
        )
        for r in rows
    ]


@router.get("/rejected-by-me", response_model=list[SigningListItem])
async def rejected_by_me(
    for_date: date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(
            Document.id.label("document_id"),
            Document.Status,
            Document.CreatedAt,
            User.FullName.label("initiator_name"),
        )
        .join(DocumentTask, DocumentTask.DocumentId == Document.id)
        .join(User, User.id == Document.InitiatorId)
        .where(and_(DocumentTask.ProcessedByUserId == user.id, DocumentTask.Status == "REJECTED"))
        .order_by(desc(DocumentTask.ProcessedAt))
    )
    if for_date:
        stmt = stmt.where(func.date(Document.CreatedAt) == for_date)

    rows = (await db.execute(stmt)).all()
    doc_ids = [int(r.document_id) for r in rows]
    viewed_ids: set[int] = set()
    if doc_ids:
        vr = (
            await db.execute(
                select(DocumentUserView.DocumentId).where(
                    and_(DocumentUserView.UserId == user.id, DocumentUserView.DocumentId.in_(doc_ids))
                )
            )
        ).all()
        viewed_ids = {int(x[0]) for x in vr}

    contents = []
    if doc_ids:
        contents = (
            await db.execute(
                select(DocumentContent.DocumentId, DocumentContent.DataJson).where(DocumentContent.DocumentId.in_(doc_ids))
            )
        ).all()
    file_by_doc: dict[int, str] = {}
    for doc_id, data in contents:
        try:
            if isinstance(data, dict):
                fn = data.get("fileName")
                if isinstance(fn, str) and fn.strip():
                    file_by_doc[int(doc_id)] = fn.strip()
        except Exception:
            pass

    return [
        SigningListItem(
            document_id=r.document_id,
            title=file_by_doc.get(int(r.document_id), f"Документ #{r.document_id}"),
            status=r.Status,
            received_at=r.CreatedAt,
            initiator_name=r.initiator_name,
            signed_count=0,
            total_signers=0,
            has_viewed=(int(r.document_id) in viewed_ids),
            waiting_for_other_signers=False,
            recipients_viewed=0,
            recipients_total=0,
        )
        for r in rows
    ]

