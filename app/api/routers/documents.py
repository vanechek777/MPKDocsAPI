from datetime import datetime, timedelta, timezone
import base64
import hashlib
import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, cast, delete, desc, exists, func, literal, or_, select
from sqlalchemy.types import Text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.audit import log_user_activity
from app.db.models import (
    Department,
    Document,
    DocumentContent,
    DocumentStep,
    DocumentTask,
    DocumentTemplate,
    DocumentUserView,
    DigitalSignature,
    SignatureProfile,
    User,
)
from app.db.session import get_db
from app.core.esig import build_esig_payload, esig_to_bytes
from app.core.nep import document_hash_hex, generate_keypair, load_private_key, load_public_key, sign_hash_hex, verify_hash_hex
from app.core.otp import verify_and_consume as verify_otp_and_consume
from app.services.document_notify import fire_notify_initiator, fire_notify_turn
from app.services.signing_turn import (
    active_pending_step_by_document,
    select_actionable_pending_task,
    user_pending_steps_by_document,
    waiting_for_others_signers,
)

router = APIRouter(prefix="/documents", tags=["documents"])

_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


async def user_can_access_document(db: AsyncSession, user: User, document_id: int) -> bool:
    """Участник маршрута подписания или инициатор."""
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None:
        return False
    if int(doc.InitiatorId) == int(user.id):
        return True
    n_user = (
        await db.execute(
            select(func.count())
            .select_from(DocumentTask)
            .where(and_(DocumentTask.DocumentId == document_id, DocumentTask.AssignedUserId == user.id))
        )
    ).scalar_one()
    if (n_user or 0) > 0:
        return True
    if user.DepartmentId is not None:
        n_dept = (
            await db.execute(
                select(func.count())
                .select_from(DocumentTask)
                .where(
                    and_(
                        DocumentTask.DocumentId == document_id,
                        DocumentTask.AssignedUserId.is_(None),
                        DocumentTask.AssignedDepartmentId == user.DepartmentId,
                    )
                )
            )
        ).scalar_one()
        if (n_dept or 0) > 0:
            return True
    return False


class DocumentListItem(BaseModel):
    id: int
    title: str
    status: str | None
    created_at: datetime | None
    initiator_name: str
    signed_count: int
    total_signers: int
    my_signed: bool = False
    is_sent: bool = False
    has_viewed: bool = False
    waiting_for_other_signers: bool = False
    recipients_viewed: int = 0
    recipients_total: int = 0
    recipients_viewed_names: list[str] = Field(default_factory=list)
    recipients_row_caption: str | None = Field(
        default=None,
        description="Для отправленных мной: серая плашка у даты — ФИО одного получателя или «Отправлено N чел.»",
    )


class CreateDocumentRequest(BaseModel):
    template_id: int
    content: dict | None = None
    signer_user_ids: list[int] = []
    signer_department_id: int | None = None
    save_as_draft: bool = False


class CreateDocumentResponse(BaseModel):
    document_id: int
    status: str


@router.post("", response_model=CreateDocumentResponse)
async def create_document(
    payload: CreateDocumentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Create document + content first
    status = "DRAFT" if payload.save_as_draft else "IN_PROGRESS"
    doc = Document(
        TemplateId=payload.template_id,
        InitiatorId=user.id,
        Status=status,
        CreatedAt=datetime.now(tz=timezone.utc).replace(tzinfo=None),
    )
    db.add(doc)
    await db.flush()

    content = payload.content or {}
    # Keep signer list in content for drafts / later audit.
    if payload.signer_user_ids:
        content.setdefault("signerUserIds", payload.signer_user_ids)
    db.add(DocumentContent(DocumentId=doc.id, DataJson=content))

    # If draft - do not create signing tasks yet (won't appear in inbox).
    if payload.save_as_draft:
        await log_user_activity(db, user_id=int(user.id), action="DOC_CREATE_DRAFT", detail=str(doc.id))
        await db.commit()
        return CreateDocumentResponse(document_id=doc.id, status=doc.Status or "DRAFT")

    # Build sequential signer workflow: each signer is a step+task.
    step_order = 1
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    for signer_id in payload.signer_user_ids:
        st = DocumentStep(
            DocumentId=doc.id,
            StepOrder=step_order,
            ApprovalMode="ANY",
            Status="PENDING",
        )
        db.add(st)
        await db.flush()
        # Initiator should never be "waiting" on their own document:
        # if initiator is included as signer, mark it as already signed immediately.
        is_initiator = int(signer_id) == int(user.id)
        db.add(
            DocumentTask(
                StepId=st.id,
                DocumentId=doc.id,
                AssignedUserId=signer_id,
                Status="SIGNED" if is_initiator else "PENDING",
                ProcessedByUserId=user.id if is_initiator else None,
                ProcessedAt=now if is_initiator else None,
            )
        )
        step_order += 1

    # Department assignment (single task for the whole dept) can be combined with user signers.
    if payload.signer_department_id is not None:
        st = DocumentStep(
            DocumentId=doc.id,
            StepOrder=step_order,
            ApprovalMode="ANY",
            Status="PENDING",
        )
        db.add(st)
        await db.flush()
        db.add(
            DocumentTask(
                StepId=st.id,
                DocumentId=doc.id,
                AssignedDepartmentId=payload.signer_department_id,
                Status="PENDING",
            )
        )

    await log_user_activity(db, user_id=int(user.id), action="DOC_CREATE", detail=str(doc.id))
    await db.commit()
    fire_notify_turn(doc.id)
    return CreateDocumentResponse(document_id=doc.id, status=doc.Status or "IN_PROGRESS")


class UpdateDraftContentRequest(BaseModel):
    content: dict


@router.patch("/{document_id}/draft-content", status_code=status.HTTP_204_NO_CONTENT)
async def update_draft_content(
    document_id: int,
    payload: UpdateDraftContentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Черновик: инициатор обновляет JSON (мастер, поля формы). Вложение не затирается, если в запросе нет нового fileBase64."""
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")
    if int(doc.InitiatorId) != int(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет права изменять этот документ")
    if (doc.Status or "").upper() != "DRAFT":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Обновление только для черновика (DRAFT)")
    dc = (
        await db.execute(select(DocumentContent).where(DocumentContent.DocumentId == document_id))
    ).scalar_one_or_none()
    if dc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Нет содержимого документа")
    old = dict(dc.DataJson or {}) if isinstance(dc.DataJson, dict) else {}
    incoming = dict(payload.content or {})
    merged = {**old}
    for k, v in incoming.items():
        if k in ("fileBase64", "fileMime", "fileUploadedAt") and v in (None, ""):
            continue
        merged[k] = v
    dc.DataJson = merged
    from sqlalchemy.orm import attributes

    attributes.flag_modified(dc, "DataJson")
    await db.commit()
    return None


@router.get("/drafts", response_model=list[DocumentListItem])
async def list_my_drafts(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Черновики текущего пользователя (инициатор, статус DRAFT)."""
    limit = min(max(limit, 1), 200)
    stmt = (
        select(
            Document.id,
            Document.Status,
            Document.CreatedAt,
            Document.InitiatorId,
            User.FullName.label("initiator_name"),
            literal(0).label("signed_count"),
            literal(0).label("total_signers"),
        )
        .join(User, User.id == Document.InitiatorId)
        .where(
            and_(
                Document.InitiatorId == user.id,
                func.upper(func.coalesce(Document.Status, "")) == "DRAFT",
            )
        )
        .order_by(desc(Document.CreatedAt))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    doc_ids = [int(r.id) for r in rows]
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
        DocumentListItem(
            id=r.id,
            title=file_by_doc.get(int(r.id), f"Черновик #{r.id}"),
            status=r.Status,
            created_at=r.CreatedAt,
            initiator_name=r.initiator_name,
            signed_count=0,
            total_signers=0,
            my_signed=False,
            is_sent=True,
            has_viewed=(int(r.id) in viewed_ids),
            waiting_for_other_signers=False,
            recipients_viewed=0,
            recipients_total=0,
            recipients_viewed_names=[],
            recipients_row_caption=None,
        )
        for r in rows
    ]


@router.delete("/{document_id}/draft", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_draft(
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Полное удаление черновика инициатором (только статус DRAFT)."""
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")
    if int(doc.InitiatorId) != int(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет права удалить этот черновик")
    if (doc.Status or "").upper() != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Удалить можно только черновик (DRAFT)",
        )

    await db.execute(delete(DocumentTask).where(DocumentTask.DocumentId == document_id))
    await db.execute(delete(DocumentStep).where(DocumentStep.DocumentId == document_id))
    await db.execute(delete(DocumentUserView).where(DocumentUserView.DocumentId == document_id))
    await db.execute(delete(DigitalSignature).where(DigitalSignature.DocumentId == document_id))
    await db.execute(delete(DocumentContent).where(DocumentContent.DocumentId == document_id))
    await db.execute(delete(Document).where(Document.id == document_id))
    await db.commit()
    return None


@router.get("/recent", response_model=list[DocumentListItem])
async def recent_documents(
    limit: int = 50,
    tab: str = "all",  # all|received|sent
    q: str | None = None,
    archive: bool = False,
    search_in_template_names: bool = True,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    limit = min(max(limit, 1), 200)
    # «Недавние» — документы за последние 3×24 часа от текущего момента (UTC, как CreatedAt в БД).
    now_utc = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(days=3)

    # signer stats for each document
    signer_total_sq = (
        select(DocumentTask.DocumentId, func.count(DocumentTask.id).label("total"))
        .group_by(DocumentTask.DocumentId)
        .subquery()
    )
    signer_signed_sq = (
        select(
            DocumentTask.DocumentId,
            func.count(DocumentTask.id).label("signed"),
        )
        .where(DocumentTask.Status == "SIGNED")
        .group_by(DocumentTask.DocumentId)
        .subquery()
    )

    stmt = (
        select(
            Document.id,
            Document.Status,
            Document.CreatedAt,
            Document.InitiatorId,
            User.FullName.label("initiator_name"),
            func.coalesce(signer_signed_sq.c.signed, 0).label("signed_count"),
            func.coalesce(signer_total_sq.c.total, 0).label("total_signers"),
        )
        .join(User, User.id == Document.InitiatorId)
        .outerjoin(signer_total_sq, signer_total_sq.c.DocumentId == Document.id)
        .outerjoin(signer_signed_sq, signer_signed_sq.c.DocumentId == Document.id)
        .order_by(desc(Document.CreatedAt))
        .limit(limit)
    )

    if tab == "sent":
        stmt = stmt.where(Document.InitiatorId == user.id)
    elif tab == "received":
        # documents where current user has at least one task
        exists_task = (
            select(func.count())
            .select_from(DocumentTask)
            .where(and_(DocumentTask.DocumentId == Document.id, DocumentTask.AssignedUserId == user.id))
        )
        stmt = stmt.where(exists_task.scalar_subquery() > 0)

    # Черновики не показываем в «Недавних» (все / полученные / отправленные) — только GET /documents/drafts.
    stmt = stmt.where(func.upper(func.coalesce(Document.Status, "")) != "DRAFT")

    qn = (q or "").strip()
    if qn:
        # LOCATE вместо LIKE — без ESCAPE; ищем подстроку в ФИО, шаблоне, JSON (fileName/title/note), id.
        q_lit = literal(qn)
        name_match = func.locate(q_lit, User.FullName) > 0
        template_match = exists(
            select(1)
            .select_from(DocumentTemplate)
            .where(
                and_(
                    DocumentTemplate.id == Document.TemplateId,
                    func.locate(q_lit, DocumentTemplate.Name) > 0,
                )
            )
        )
        fn = func.json_unquote(func.json_extract(DocumentContent.DataJson, "$.fileName"))
        tit = func.json_unquote(func.json_extract(DocumentContent.DataJson, "$.title"))
        nt = func.json_unquote(func.json_extract(DocumentContent.DataJson, "$.note"))
        raw_json_text = cast(DocumentContent.DataJson, Text)
        file_match = exists(
            select(1)
            .select_from(DocumentContent)
            .where(
                and_(
                    DocumentContent.DocumentId == Document.id,
                    or_(
                        func.locate(q_lit, fn) > 0,
                        func.locate(q_lit, tit) > 0,
                        func.locate(q_lit, nt) > 0,
                        func.locate(q_lit, raw_json_text) > 0,
                    ),
                )
            )
        )
        id_match = Document.id == int(qn) if qn.isdigit() else None
        parts = [name_match] + ([template_match] if search_in_template_names else []) + [file_match]
        if id_match is not None:
            parts.append(id_match)
        stmt = stmt.where(or_(*parts))

    if archive:
        stmt = stmt.where(or_(Document.CreatedAt.is_(None), Document.CreatedAt < cutoff))
    else:
        # Без поиска — только последние 3 суток; с поиском — до года (иначе «Приказ» и т.п. не находятся вне окна).
        eff_cutoff = (now_utc - timedelta(days=365)) if qn else cutoff
        stmt = stmt.where(and_(Document.CreatedAt.is_not(None), Document.CreatedAt >= eff_cutoff))

    rows = (await db.execute(stmt)).all()

    doc_ids = [int(r.id) for r in rows]
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
            # ignore malformed json
            pass

    # mark docs that current user has already signed (even if doc isn't fully signed yet)
    signed_by_me = set(
        (
            await db.execute(
                select(DocumentTask.DocumentId)
                .where(
                    and_(
                        DocumentTask.AssignedUserId == user.id,
                        DocumentTask.Status == "SIGNED",
                    )
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    active_by = await active_pending_step_by_document(db, doc_ids) if doc_ids else {}
    user_pending = await user_pending_steps_by_document(db, user, doc_ids) if doc_ids else {}

    # Для отправленных мной: сколько подписантов (по пользователям) открыло документ
    initiator_doc_ids = [int(r.id) for r in rows if int(r.InitiatorId) == int(user.id)]
    recipients_by_doc: dict[int, set[int]] = {}
    if initiator_doc_ids:
        ar = (
            await db.execute(
                select(DocumentTask.DocumentId, DocumentTask.AssignedUserId).where(
                    and_(
                        DocumentTask.DocumentId.in_(initiator_doc_ids),
                        DocumentTask.AssignedUserId.is_not(None),
                    )
                )
            )
        ).all()
        for did, uid in ar:
            d, u = int(did), int(uid)
            ini = int(user.id)
            if u == ini:
                continue
            recipients_by_doc.setdefault(d, set()).add(u)

    viewed_pairs: set[tuple[int, int]] = set()
    if initiator_doc_ids:
        vr2 = (
            await db.execute(
                select(DocumentUserView.DocumentId, DocumentUserView.UserId).where(
                    DocumentUserView.DocumentId.in_(initiator_doc_ids)
                )
            )
        ).all()
        for did, uid in vr2:
            viewed_pairs.add((int(did), int(uid)))

    all_recipient_user_ids: set[int] = set()
    for s in recipients_by_doc.values():
        all_recipient_user_ids.update(s)
    names_by_uid: dict[int, str] = {}
    if all_recipient_user_ids:
        nmrows = (
            await db.execute(select(User.id, User.FullName).where(User.id.in_(list(all_recipient_user_ids))))
        ).all()
        for uid, fn in nmrows:
            label = (fn or "").strip()
            names_by_uid[int(uid)] = label if label else f"Пользователь #{int(uid)}"

    def recipient_view_stats(doc_id: int) -> tuple[int, int]:
        rec = recipients_by_doc.get(doc_id, set())
        if not rec:
            return 0, 0
        viewed = sum(1 for u in rec if (doc_id, u) in viewed_pairs)
        return viewed, len(rec)

    def recipient_viewed_names(doc_id: int) -> list[str]:
        rec = recipients_by_doc.get(doc_id, set())
        if not rec:
            return []
        out: list[str] = []
        for u in rec:
            if (doc_id, u) not in viewed_pairs:
                continue
            out.append(names_by_uid.get(u) or f"Пользователь #{u}")
        out.sort(key=str.lower)
        return out

    def recipients_row_caption_for(doc_id: int) -> str:
        rec = recipients_by_doc.get(doc_id, set())
        if len(rec) == 1:
            u = next(iter(rec))
            return names_by_uid.get(u) or f"Пользователь #{u}"
        if len(rec) > 1:
            return f"Отправлено {len(rec)} чел."
        return "Подписанты не назначены"

    return [
        DocumentListItem(
            id=r.id,
            title=file_by_doc.get(int(r.id), f"Документ #{r.id}"),
            status=r.Status,
            created_at=r.CreatedAt,
            initiator_name=r.initiator_name,
            signed_count=int(r.signed_count or 0),
            total_signers=int(r.total_signers or 0),
            my_signed=(r.id in signed_by_me),
            is_sent=(int(r.InitiatorId) == int(user.id)),
            has_viewed=(int(r.id) in viewed_ids),
            waiting_for_other_signers=waiting_for_others_signers(
                int(r.id), int(r.InitiatorId), int(user.id), active_by, user_pending
            ),
            recipients_viewed=recipient_view_stats(int(r.id))[0] if int(r.InitiatorId) == int(user.id) else 0,
            recipients_total=recipient_view_stats(int(r.id))[1] if int(r.InitiatorId) == int(user.id) else 0,
            recipients_viewed_names=(
                recipient_viewed_names(int(r.id)) if int(r.InitiatorId) == int(user.id) else []
            ),
            recipients_row_caption=(
                recipients_row_caption_for(int(r.id)) if int(r.InitiatorId) == int(user.id) else None
            ),
        )
        for r in rows
    ]


@router.post("/{document_id}/view", status_code=status.HTTP_204_NO_CONTENT)
async def mark_document_viewed(
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Фиксирует, что текущий пользователь открыл карточку документа (точка на списке станет серой)."""
    if not await user_can_access_document(db, user, document_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к документу")
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    existing = (
        await db.execute(
            select(DocumentUserView.id).where(
                and_(DocumentUserView.DocumentId == document_id, DocumentUserView.UserId == user.id)
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(DocumentUserView(DocumentId=document_id, UserId=user.id, FirstViewedAt=now))
        await db.commit()
    return None


@router.get("/{document_id}/file")
async def download_document_file(
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await user_can_access_document(db, user, document_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к документу")
    content = (
        await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == document_id))
    ).scalar_one_or_none()
    if not isinstance(content, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Нет содержимого документа")
    b64 = content.get("fileBase64")
    if not isinstance(b64, str) or not b64.strip():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не загружен")
    try:
        raw = base64.b64decode(b64.encode("ascii"), validate=True)
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Повреждённые данные файла")
    mime = content.get("fileMime") if isinstance(content.get("fileMime"), str) else None
    media = (mime or "application/octet-stream").strip() or "application/octet-stream"
    fn = content.get("fileName") if isinstance(content.get("fileName"), str) else None
    name = (fn or f"document-{document_id}").strip() or f"document-{document_id}"
    disp = f"attachment; filename*=UTF-8''{quote(name)}"
    return Response(content=raw, media_type=media, headers={"Content-Disposition": disp})


@router.post("/{document_id}/file", status_code=status.HTTP_204_NO_CONTENT)
async def upload_document_file(
    document_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await user_can_access_document(db, user, document_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к документу")
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None or int(doc.InitiatorId) != int(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Загружать файл может только инициатор")
    body = await file.read()
    if len(body) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Файл больше {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} МБ",
        )
    dc = (
        await db.execute(select(DocumentContent).where(DocumentContent.DocumentId == document_id))
    ).scalar_one_or_none()
    if dc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Нет содержимого документа")
    c = dict(dc.DataJson or {})
    c["fileBase64"] = base64.b64encode(body).decode("ascii")
    c["fileMime"] = (file.content_type or "application/octet-stream").strip() or "application/octet-stream"
    c["fileUploadedAt"] = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat()
    dc.DataJson = c
    from sqlalchemy.orm import attributes

    attributes.flag_modified(dc, "DataJson")
    await db.commit()
    return None


@router.get("/{document_id}/nep-signature.esig")
async def export_my_nep_signature(
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await user_can_access_document(db, user, document_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к документу")
    ds = (
        await db.execute(
            select(DigitalSignature).where(
                and_(DigitalSignature.DocumentId == document_id, DigitalSignature.UserId == user.id)
            )
        )
    ).scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="НЭП-подпись не найдена для этого пользователя")
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one()
    content = (
        await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == document_id))
    ).scalar_one_or_none()
    h = (ds.DocumentHashHex or "").strip().lower() or document_hash_hex(
        document_id=doc.id,
        template_id=doc.TemplateId,
        content=content if isinstance(content, dict) else {},
    )
    title = None
    if isinstance(content, dict):
        fn = content.get("fileName")
        if isinstance(fn, str) and fn.strip():
            title = fn.strip()
    payload = build_esig_payload(
        document_id=doc.id,
        template_id=doc.TemplateId,
        document_hash_hex=h,
        signer_user_id=int(user.id),
        signature_hex=ds.SignatureHex,
        signed_at=ds.SignedAt,
        document_title=title,
    )
    raw = esig_to_bytes(payload)
    safe_name = f"doc-{document_id}-NEP.esig"
    return Response(
        content=raw,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{quote(safe_name)}'},
    )


class SignerNode(BaseModel):
    order: int
    department: str | None = None
    user_id: int | None = None
    user_name: str | None = None
    status: str
    processed_at: datetime | None = None
    signature_type: str | None = None


class DocumentDetailResponse(BaseModel):
    id: int
    status: str | None
    created_at: datetime | None
    initiator_id: int
    initiator_name: str
    template_id: int
    template_name: str
    content: dict | None = None
    signers: list[SignerNode]
    signed_count: int
    total_signers: int
    can_act: bool
    my_task_status: str | None = None
    waiting_for_other_signers: bool = False
    recipients_viewed: int = 0
    recipients_total: int = 0
    has_file_attachment: bool = False
    my_nep_export_available: bool = False
    document_content_hash_hex: str | None = None
    my_nep_document_hash_hex: str | None = None
    my_nep_signature_hex: str | None = None


@router.get("/{document_id}", response_model=DocumentDetailResponse)
async def get_document_detail(
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc_row = (
        await db.execute(
            select(
                Document,
                User.FullName.label("initiator_name"),
                DocumentTemplate.Name.label("template_name"),
            )
            .join(User, User.id == Document.InitiatorId)
            .join(DocumentTemplate, DocumentTemplate.id == Document.TemplateId)
            .where(Document.id == document_id)
        )
    ).first()
    if not doc_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")

    doc: Document = doc_row[0]
    initiator_name: str = doc_row[1]
    template_name: str = doc_row[2]

    content = (
        await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == document_id))
    ).scalar_one_or_none()

    # signature marks (NEP) for this document
    sig_rows = (
        await db.execute(
            select(DigitalSignature.UserId, DigitalSignature.SignatureHex, DigitalSignature.DocumentHashHex).where(
                DigitalSignature.DocumentId == document_id
            )
        )
    ).all()
    nep_sig_by_user: dict[int, tuple[str, str | None]] = {
        int(uid): (str(sig_hex), (str(dhh).strip().lower() if dhh else None)) for (uid, sig_hex, dhh) in sig_rows
    }

    profile_rows = (
        await db.execute(select(SignatureProfile.UserId, SignatureProfile.PublicKey).where(SignatureProfile.UserId.in_(list(nep_sig_by_user.keys()) or [-1])))
    ).all()
    pub_by_user = {int(uid): pk for (uid, pk) in profile_rows if pk}

    doc_hash = document_hash_hex(document_id=doc.id, template_id=doc.TemplateId, content=content if isinstance(content, dict) else {})

    # signer nodes from tasks (assigned user or department/position)
    tasks = (
        await db.execute(
            select(
                DocumentTask.id,
                DocumentTask.Status,
                DocumentTask.AssignedUserId,
                DocumentTask.AssignedDepartmentId,
                DocumentTask.ProcessedByUserId,
                DocumentTask.ProcessedAt,
                User.FullName.label("assigned_user_name"),
                Department.Name.label("department_name"),
                DocumentStep.StepOrder.label("step_order"),
            )
            .join(DocumentStep, DocumentStep.id == DocumentTask.StepId)
            .outerjoin(User, User.id == DocumentTask.AssignedUserId)
            .outerjoin(Department, Department.id == DocumentTask.AssignedDepartmentId)
            .where(DocumentTask.DocumentId == document_id)
            .order_by(DocumentStep.StepOrder.asc(), DocumentTask.id.asc())
        )
    ).all()

    signers: list[SignerNode] = []
    signed_count = 0
    my_status_for_user: list[str] = []
    for t in tasks:
        status = t.Status or "PENDING"
        if status == "SIGNED":
            signed_count += 1
        if t.AssignedUserId == user.id or (
            t.AssignedUserId is None
            and t.AssignedDepartmentId is not None
            and user.DepartmentId is not None
            and t.AssignedDepartmentId == user.DepartmentId
        ):
            my_status_for_user.append(status)
        sig_type = None
        if status == "SIGNED" and t.ProcessedByUserId is not None:
            uid = int(t.ProcessedByUserId)
            if uid in nep_sig_by_user and uid in pub_by_user:
                try:
                    sig_hex, stored_doc_hash = nep_sig_by_user[uid]
                    hash_for_verify = stored_doc_hash if stored_doc_hash else doc_hash
                    ok = verify_hash_hex(
                        public_key=load_public_key(pub_by_user[uid]),
                        doc_hash_hex=hash_for_verify,
                        signature_hex=sig_hex,
                    )
                    sig_type = "NEP" if ok else "NEP_INVALID"
                except Exception:
                    sig_type = "NEP_INVALID"

        signers.append(
            SignerNode(
                order=int(t.step_order),
                department=t.department_name,
                user_id=t.AssignedUserId,
                user_name=t.assigned_user_name,
                status=status,
                processed_at=t.ProcessedAt,
                signature_type=sig_type,
            )
        )

    my_task_status: str | None = None
    if my_status_for_user:
        if any(s == "SIGNED" for s in my_status_for_user):
            my_task_status = "SIGNED"
        elif any(s == "REJECTED" for s in my_status_for_user):
            my_task_status = "REJECTED"
        else:
            my_task_status = my_status_for_user[0]

    total_signers = len(tasks)
    st_up = (doc.Status or "").upper()
    doc_terminal = st_up in {"SIGNED", "REJECTED", "CANCELLED"}
    actionable = await select_actionable_pending_task(db, document_id, user)
    active_by = await active_pending_step_by_document(db, [document_id])
    user_pending = await user_pending_steps_by_document(db, user, [document_id])
    waiting = (
        not doc_terminal
        and waiting_for_others_signers(document_id, int(doc.InitiatorId), int(user.id), active_by, user_pending)
    )
    can_act = (actionable is not None) and not doc_terminal

    rec_ids: set[int] = set()
    for t in tasks:
        if t.AssignedUserId is not None and int(t.AssignedUserId) != int(doc.InitiatorId):
            rec_ids.add(int(t.AssignedUserId))
    rv = rt = 0
    if int(user.id) == int(doc.InitiatorId) and rec_ids:
        rt = len(rec_ids)
        if rt > 0:
            vrows = (
                await db.execute(
                    select(DocumentUserView.UserId).where(
                        and_(DocumentUserView.DocumentId == document_id, DocumentUserView.UserId.in_(list(rec_ids)))
                    )
                )
            ).all()
            rv = len({int(x[0]) for x in vrows})

    has_file = (
        isinstance(content, dict)
        and isinstance(content.get("fileBase64"), str)
        and len(str(content.get("fileBase64")).strip()) > 0
    )
    my_nep_export = int(user.id) in nep_sig_by_user
    my_nep_sig_hex: str | None = None
    my_nep_doc_hash_hex: str | None = None
    if int(user.id) in nep_sig_by_user:
        my_nep_sig_hex, my_nep_doc_hash_hex = nep_sig_by_user[int(user.id)]

    return DocumentDetailResponse(
        id=doc.id,
        status=doc.Status,
        created_at=doc.CreatedAt,
        initiator_id=doc.InitiatorId,
        initiator_name=initiator_name,
        template_id=doc.TemplateId,
        template_name=template_name,
        content=content,
        signers=signers,
        signed_count=signed_count,
        total_signers=total_signers,
        can_act=can_act,
        my_task_status=my_task_status,
        waiting_for_other_signers=waiting,
        recipients_viewed=rv,
        recipients_total=rt,
        has_file_attachment=has_file,
        my_nep_export_available=my_nep_export,
        document_content_hash_hex=doc_hash,
        my_nep_document_hash_hex=my_nep_doc_hash_hex,
        my_nep_signature_hex=my_nep_sig_hex,
    )


class ActionRequest(BaseModel):
    otp_code: str | None = None
    reason: str | None = None


class ActionResponse(BaseModel):
    ok: bool = True
    document_id: int
    document_status: str | None


@router.post("/{document_id}/actions/sign", response_model=ActionResponse)
async def sign_document(
    document_id: int,
    payload: ActionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await select_actionable_pending_task(db, document_id, user)

    if task is None:
        return ActionResponse(ok=False, document_id=document_id, document_status=None)

    if payload.otp_code:
        if not await verify_otp_and_consume(user.id, payload.otp_code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired code",
            )

    task.Status = "SIGNED"
    task.ProcessedByUserId = user.id
    task.ProcessedAt = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    # If OTP provided, treat this as NEP signing and store a cryptographic signature.
    if payload.otp_code:
        profile = (
            await db.execute(select(SignatureProfile).where(SignatureProfile.UserId == user.id, SignatureProfile.isRevoked.is_(False)))
        ).scalar_one_or_none()
        if profile is None:
            pub_b64, priv_enc_b64 = generate_keypair()
            profile = SignatureProfile(UserId=user.id, PublicKey=pub_b64, EncryptedPrivateKey=priv_enc_b64, CreatedAt=task.ProcessedAt, isRevoked=False)
            db.add(profile)
            await db.flush()

        existing = (
            await db.execute(
                select(DigitalSignature.id).where(
                    and_(DigitalSignature.DocumentId == document_id, DigitalSignature.UserId == user.id)
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            doc_content = (
                await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == document_id))
            ).scalar_one_or_none()
            doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one()
            h = document_hash_hex(
                document_id=document_id,
                template_id=doc.TemplateId,
                content=doc_content if isinstance(doc_content, dict) else {},
            )
            priv = load_private_key(profile.EncryptedPrivateKey)
            sig_hex = sign_hash_hex(private_key=priv, doc_hash_hex=h)
            db.add(
                DigitalSignature(
                    DocumentId=document_id,
                    UserId=user.id,
                    SignatureHex=sig_hex,
                    DocumentHashHex=h,
                    SignedAt=task.ProcessedAt,
                )
            )

    # Update document status if all signed
    total = (
        await db.execute(select(func.count()).select_from(DocumentTask).where(DocumentTask.DocumentId == document_id))
    ).scalar_one()
    signed = (
        await db.execute(
            select(func.count())
            .select_from(DocumentTask)
            .where(and_(DocumentTask.DocumentId == document_id, DocumentTask.Status == "SIGNED"))
        )
    ).scalar_one()

    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one()
    if signed >= total and total > 0:
        doc.Status = "SIGNED"
    else:
        doc.Status = doc.Status or "IN_PROGRESS"

    await log_user_activity(db, user_id=int(user.id), action="DOC_SIGN", detail=str(document_id))
    await db.commit()
    fire_notify_initiator(document_id, user.id, event="signed", document_status=doc.Status)
    fire_notify_turn(document_id)
    return ActionResponse(ok=True, document_id=document_id, document_status=doc.Status)


@router.post("/{document_id}/actions/reject", response_model=ActionResponse)
async def reject_document(
    document_id: int,
    _: ActionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await select_actionable_pending_task(db, document_id, user)

    if task is None:
        return ActionResponse(ok=False, document_id=document_id, document_status=None)

    task.Status = "REJECTED"
    task.ProcessedByUserId = user.id
    task.ProcessedAt = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one()
    doc.Status = "REJECTED"

    await log_user_activity(db, user_id=int(user.id), action="DOC_REJECT", detail=str(document_id))
    await db.commit()
    fire_notify_initiator(document_id, user.id, event="rejected", document_status=doc.Status)
    return ActionResponse(ok=True, document_id=document_id, document_status=doc.Status)


@router.post("/{document_id}/actions/cancel", response_model=ActionResponse)
async def cancel_document(
    document_id: int,
    _: ActionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Only initiator can cancel while in progress
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None:
        return ActionResponse(ok=False, document_id=document_id, document_status=None)

    if int(doc.InitiatorId) != int(user.id):
        return ActionResponse(ok=False, document_id=document_id, document_status=doc.Status)

    st = (doc.Status or "").upper()
    if st in {"SIGNED", "REJECTED", "CANCELLED"}:
        return ActionResponse(ok=False, document_id=document_id, document_status=doc.Status)

    doc.Status = "CANCELLED"
    # Cancel all pending tasks so it disappears from inbox and isn't actionable
    pending_tasks = (
        await db.execute(
            select(DocumentTask).where(and_(DocumentTask.DocumentId == document_id, DocumentTask.Status == "PENDING"))
        )
    ).scalars().all()
    for t in pending_tasks:
        t.Status = "CANCELLED"
        t.ProcessedByUserId = user.id
        t.ProcessedAt = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    await log_user_activity(db, user_id=int(user.id), action="DOC_CANCEL", detail=str(document_id))
    await db.commit()
    return ActionResponse(ok=True, document_id=document_id, document_status=doc.Status)

