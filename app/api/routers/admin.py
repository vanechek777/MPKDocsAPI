"""Админ-панель: пользователи, категории, шаблоны, сводка и журнал."""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timezone

import time

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.admin_access import env_admin_user_ids, user_is_admin
from app.core.audit import log_user_activity
from app.core.presence import online_within_seconds
from app.core.runtime_config import get_onec_config, patch_onec_config
from app.db.models import Department, Document, DocumentCategory, DocumentTemplate, Position, StaffDirectoryEntry, User, UserActivityLog
from app.services.staff_import import import_staff_rows, parse_staff_file
from app.db.session import get_db

router = APIRouter(prefix="/admin", tags=["admin"])

APP_VERSION = "1.0.0"


class DashboardResponse(BaseModel):
    server_time_utc: str
    app_version: str
    database_ok: bool
    database_latency_ms: float | None = None
    online_users_5m: int = 0
    users_total: int = 0
    documents_total: int = 0
    templates_active: int = 0
    categories_total: int = 0


@router.get("/dashboard", response_model=DashboardResponse)
async def admin_dashboard(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DashboardResponse:
    t0 = time.perf_counter()
    db_ok = True
    lat: float | None
    try:
        await db.execute(text("SELECT 1"))
        lat = (time.perf_counter() - t0) * 1000.0
    except Exception:
        db_ok = False
        lat = None

    now = datetime.now(tz=timezone.utc)
    users_total = int((await db.execute(select(func.count()).select_from(User))).scalar_one() or 0)
    documents_total = int((await db.execute(select(func.count()).select_from(Document))).scalar_one() or 0)
    templates_active = int(
        (
            await db.execute(
                select(func.count()).select_from(DocumentTemplate).where(DocumentTemplate.isActive.is_(True))
            )
        ).scalar_one()
        or 0
    )
    categories_total = int((await db.execute(select(func.count()).select_from(DocumentCategory))).scalar_one() or 0)

    return DashboardResponse(
        server_time_utc=now.isoformat(),
        app_version=APP_VERSION,
        database_ok=db_ok,
        database_latency_ms=lat,
        online_users_5m=online_within_seconds(300),
        users_total=users_total,
        documents_total=documents_total,
        templates_active=templates_active,
        categories_total=categories_total,
    )


class ActivityItem(BaseModel):
    id: int
    created_at: str | None
    user_id: int | None
    user_name: str | None = None
    action: str
    detail: str | None = None


def _activity_created_at_bounds(
    from_date: date | None,
    to_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    """Границы суток UTC для фильтра CreatedAt (в БД — naive UTC)."""
    start: datetime | None = None
    end: datetime | None = None
    if from_date is not None:
        start = datetime.combine(from_date, dt_time.min)
    if to_date is not None:
        end = datetime.combine(to_date, dt_time(23, 59, 59, 999999))
    return start, end


async def _fetch_activity_rows(
    db: AsyncSession,
    *,
    limit: int,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list:
    limit = min(max(limit, 1), 250)
    start, end = _activity_created_at_bounds(from_date, to_date)
    stmt = (
        select(
            UserActivityLog.id,
            UserActivityLog.CreatedAt,
            UserActivityLog.UserId,
            UserActivityLog.Action,
            UserActivityLog.Detail,
            User.FullName,
        )
        .outerjoin(User, User.id == UserActivityLog.UserId)
        .order_by(UserActivityLog.id.desc())
        .limit(limit)
    )
    if start is not None:
        stmt = stmt.where(UserActivityLog.CreatedAt >= start)
    if end is not None:
        stmt = stmt.where(UserActivityLog.CreatedAt <= end)
    return (await db.execute(stmt)).all()


@router.get("/activity/export")
async def admin_activity_export(
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    limit: int = Query(250, ge=1, le=250),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Текстовый журнал за период (до 250 записей, новые сверху)."""
    from fastapi.responses import PlainTextResponse

    rows = await _fetch_activity_rows(db, limit=limit, from_date=from_date, to_date=to_date)
    lines: list[str] = [
        "Журнал действий — МПК Документы",
        f"Сформировано (UTC): {datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat()}",
    ]
    if from_date is not None or to_date is not None:
        f = from_date.isoformat() if from_date else "…"
        t = to_date.isoformat() if to_date else "…"
        lines.append(f"Период: {f} — {t}")
    lines.append(f"Записей в выгрузке: {len(rows)} (лимит {limit})")
    lines.append("")
    lines.append("Время (UTC)\tПользователь\tДействие\tДетали")
    lines.append("-" * 72)
    for r in rows:
        ca = r.CreatedAt.isoformat() if r.CreatedAt else ""
        uname = (r.FullName or "").strip() or (str(r.UserId) if r.UserId is not None else "")
        detail = (r.Detail or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")
        lines.append(f"{ca}\t{uname}\t{r.Action}\t{detail}")
    body = "\n".join(lines) + "\n"
    fname = "mpk-activity-log.txt"
    if from_date and to_date:
        fname = f"mpk-activity_{from_date.isoformat()}_{to_date.isoformat()}.txt"
    elif from_date:
        fname = f"mpk-activity_from_{from_date.isoformat()}.txt"
    elif to_date:
        fname = f"mpk-activity_to_{to_date.isoformat()}.txt"
    return PlainTextResponse(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/activity", response_model=list[ActivityItem])
async def admin_activity(
    limit: int = Query(10, ge=1, le=250),
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[ActivityItem]:
    rows = await _fetch_activity_rows(db, limit=limit, from_date=from_date, to_date=to_date)
    out: list[ActivityItem] = []
    for r in rows:
        ca = r.CreatedAt.isoformat() if r.CreatedAt else None
        out.append(
            ActivityItem(
                id=int(r.id),
                created_at=ca,
                user_id=int(r.UserId) if r.UserId is not None else None,
                user_name=r.FullName,
                action=r.Action,
                detail=r.Detail,
            )
        )
    return out


class AdminUserRow(BaseModel):
    id: int
    phone_number: str
    full_name: str
    email: str | None = None
    is_admin: bool
    status: bool | None = None


@router.get("/users", response_model=list[AdminUserRow])
async def admin_list_users(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminUserRow]:
    users = (await db.execute(select(User).order_by(User.id.asc()))).scalars().all()
    return [
        AdminUserRow(
            id=int(u.id),
            phone_number=u.PhoneNumber,
            full_name=u.FullName,
            email=u.Email,
            is_admin=user_is_admin(u),
            status=u.Status,
        )
        for u in users
    ]


class SetAdminBody(BaseModel):
    is_admin: bool


@router.patch("/users/{user_id}/admin", response_model=AdminUserRow)
async def admin_set_user_admin(
    user_id: int,
    body: SetAdminBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserRow:
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    if not body.is_admin:
        n_admins = int(
            (await db.execute(select(func.count()).select_from(User).where(User.IsAdmin.is_(True)))).scalar_one() or 0
        )
        if target.IsAdmin and n_admins <= 1 and not env_admin_user_ids():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Нельзя снять последнего администратора (задайте MPK_ADMIN_USER_IDS или другого админа в БД).",
            )

    target.IsAdmin = body.is_admin
    await log_user_activity(
        db,
        user_id=int(admin.id),
        action="ADMIN_SET_ADMIN",
        detail=f"user_id={user_id}, is_admin={body.is_admin}",
    )
    await db.commit()
    await db.refresh(target)
    return AdminUserRow(
        id=int(target.id),
        phone_number=target.PhoneNumber,
        full_name=target.FullName,
        email=target.Email,
        is_admin=user_is_admin(target),
        status=target.Status,
    )


class PromoteByPhoneBody(BaseModel):
    phone_number: str

    @field_validator("phone_number", mode="before")
    @classmethod
    def strip_phone(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


@router.post("/users/promote-by-phone", response_model=AdminUserRow)
async def admin_promote_by_phone(
    body: PromoteByPhoneBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserRow:
    if not body.phone_number:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Укажите телефон")
    target = (await db.execute(select(User).where(User.PhoneNumber == body.phone_number))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь с таким номером не найден")
    target.IsAdmin = True
    await log_user_activity(
        db,
        user_id=int(admin.id),
        action="ADMIN_PROMOTE_PHONE",
        detail=body.phone_number,
    )
    await db.commit()
    await db.refresh(target)
    return AdminUserRow(
        id=int(target.id),
        phone_number=target.PhoneNumber,
        full_name=target.FullName,
        email=target.Email,
        is_admin=user_is_admin(target),
        status=target.Status,
    )


class CategoryOut(BaseModel):
    id: int
    name: str
    sort_order: int
    is_active: bool | None


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sort_order: int = 0


@router.get("/categories", response_model=list[CategoryOut])
async def admin_list_categories(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[CategoryOut]:
    rows = (await db.execute(select(DocumentCategory).order_by(DocumentCategory.SortOrder, DocumentCategory.id))).scalars().all()
    return [CategoryOut(id=c.id, name=c.Name, sort_order=c.SortOrder, is_active=c.isActive) for c in rows]


@router.post("/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
async def admin_create_category(
    body: CategoryCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> CategoryOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустое имя")
    c = DocumentCategory(Name=name, SortOrder=body.sort_order, isActive=True)
    db.add(c)
    await log_user_activity(db, user_id=int(admin.id), action="ADMIN_CATEGORY_CREATE", detail=name)
    await db.commit()
    await db.refresh(c)
    return CategoryOut(id=c.id, name=c.Name, sort_order=c.SortOrder, is_active=c.isActive)


class CategoryPatch(BaseModel):
    is_active: bool | None = None
    name: str | None = None
    sort_order: int | None = None


@router.patch("/categories/{category_id}", response_model=CategoryOut)
async def admin_patch_category(
    category_id: int,
    body: CategoryPatch,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> CategoryOut:
    c = (await db.execute(select(DocumentCategory).where(DocumentCategory.id == category_id))).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Категория не найдена")
    if body.is_active is not None:
        c.isActive = body.is_active
    if body.name is not None and body.name.strip():
        c.Name = body.name.strip()
    if body.sort_order is not None:
        c.SortOrder = body.sort_order
    await log_user_activity(db, user_id=int(admin.id), action="ADMIN_CATEGORY_PATCH", detail=f"id={category_id}")
    await db.commit()
    await db.refresh(c)
    return CategoryOut(id=c.id, name=c.Name, sort_order=c.SortOrder, is_active=c.isActive)


class AdminTemplateRow(BaseModel):
    id: int
    name: str
    category_id: int | None = None
    category_name: str | None = None
    is_active: bool | None
    template_path: str


@router.get("/templates", response_model=list[AdminTemplateRow])
async def admin_list_templates(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminTemplateRow]:
    rows = (
        await db.execute(
            select(DocumentTemplate, DocumentCategory.Name.label("cat_name"))
            .outerjoin(DocumentCategory, DocumentCategory.id == DocumentTemplate.CategoryId)
            .order_by(DocumentTemplate.id.asc())
        )
    ).all()
    out: list[AdminTemplateRow] = []
    for t, cat_name in rows:
        out.append(
            AdminTemplateRow(
                id=t.id,
                name=t.Name,
                category_id=t.CategoryId,
                category_name=cat_name,
                is_active=t.isActive,
                template_path=t.TemplatePath,
            )
        )
    return out


class AdminTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category_id: int | None = None
    template_path: str | None = None
    form_schema: dict | None = None


@router.post("/templates", response_model=AdminTemplateRow, status_code=status.HTTP_201_CREATED)
async def admin_create_template(
    body: AdminTemplateCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTemplateRow:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустое имя")
    if body.category_id is not None:
        c = (await db.execute(select(DocumentCategory).where(DocumentCategory.id == body.category_id))).scalar_one_or_none()
        if c is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Категория не найдена")
    path = (body.template_path or "").strip() or "/templates/custom.docx"
    schema = body.form_schema if isinstance(body.form_schema, dict) else {"fields": []}
    t = DocumentTemplate(Name=name, TemplatePath=path, FormSchema=schema, isActive=True, CategoryId=body.category_id)
    db.add(t)
    await log_user_activity(db, user_id=int(admin.id), action="ADMIN_TEMPLATE_CREATE", detail=name)
    await db.commit()
    await db.refresh(t)
    cat_name = None
    if t.CategoryId:
        cat_name = (
            await db.execute(select(DocumentCategory.Name).where(DocumentCategory.id == t.CategoryId))
        ).scalar_one_or_none()
    return AdminTemplateRow(
        id=t.id,
        name=t.Name,
        category_id=t.CategoryId,
        category_name=cat_name,
        is_active=t.isActive,
        template_path=t.TemplatePath,
    )


class AdminTemplatePatch(BaseModel):
    is_active: bool | None = None
    category_id: int | None = None
    name: str | None = None


@router.patch("/templates/{template_id}", response_model=AdminTemplateRow)
async def admin_patch_template(
    template_id: int,
    body: AdminTemplatePatch,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTemplateRow:
    t = (await db.execute(select(DocumentTemplate).where(DocumentTemplate.id == template_id))).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Шаблон не найден")
    if body.name is not None and body.name.strip():
        t.Name = body.name.strip()
    if body.is_active is not None:
        t.isActive = body.is_active
    if body.category_id is not None:
        if body.category_id == 0:
            t.CategoryId = None
        else:
            c = (await db.execute(select(DocumentCategory).where(DocumentCategory.id == body.category_id))).scalar_one_or_none()
            if c is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Категория не найдена")
            t.CategoryId = body.category_id
    await log_user_activity(db, user_id=int(admin.id), action="ADMIN_TEMPLATE_PATCH", detail=f"id={template_id}")
    await db.commit()
    await db.refresh(t)
    cat_name = None
    if t.CategoryId:
        cat_name = (
            await db.execute(select(DocumentCategory.Name).where(DocumentCategory.id == t.CategoryId))
        ).scalar_one_or_none()
    return AdminTemplateRow(
        id=t.id,
        name=t.Name,
        category_id=t.CategoryId,
        category_name=cat_name,
        is_active=t.isActive,
        template_path=t.TemplatePath,
    )


class StaffStatsResponse(BaseModel):
    staff_total: int
    positions_total: int
    departments_total: int


@router.get("/staff/stats", response_model=StaffStatsResponse)
async def staff_stats(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> StaffStatsResponse:
    staff_total = int(
        (
            await db.execute(
                select(func.count()).select_from(StaffDirectoryEntry).where(StaffDirectoryEntry.isActive.is_(True))
            )
        ).scalar_one()
        or 0
    )
    positions_total = int((await db.execute(select(func.count()).select_from(Position))).scalar_one() or 0)
    departments_total = int((await db.execute(select(func.count()).select_from(Department))).scalar_one() or 0)
    return StaffStatsResponse(
        staff_total=staff_total,
        positions_total=positions_total,
        departments_total=departments_total,
    )


class StaffImportResponse(BaseModel):
    rows_total: int
    rows_imported: int
    departments_upserted: int
    positions_upserted: int
    staff_upserted: int


@router.post("/staff/import", response_model=StaffImportResponse)
async def staff_import_file(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
) -> StaffImportResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустой файл")
    try:
        rows = parse_staff_file(file.filename or "import.csv", raw)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не найдены строки. Ожидаются колонки: ФИО, Должность, Отдел.",
        )
    stats = await import_staff_rows(db, rows)
    await log_user_activity(
        db,
        user_id=int(admin.id),
        action="ADMIN_STAFF_IMPORT",
        detail=f"rows={stats.rows_imported}",
    )
    return StaffImportResponse(
        rows_total=stats.rows_total,
        rows_imported=stats.rows_imported,
        departments_upserted=stats.departments_upserted,
        positions_upserted=stats.positions_upserted,
        staff_upserted=stats.staff_upserted,
    )


class OneCConfigResponse(BaseModel):
    base_url: str | None = None
    username: str | None = None
    has_password: bool = False


class OneCConfigUpdateRequest(BaseModel):
    base_url: str | None = None
    username: str | None = None
    password: str | None = None


@router.get("/onec/config", response_model=OneCConfigResponse)
async def onec_get_config(_: User = Depends(require_admin)) -> OneCConfigResponse:
    cfg = get_onec_config()
    return OneCConfigResponse(
        base_url=cfg.get("base_url"),
        username=cfg.get("username"),
        has_password=bool(cfg.get("password")),
    )


@router.put("/onec/config", response_model=OneCConfigResponse)
async def onec_put_config(
    body: OneCConfigUpdateRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> OneCConfigResponse:
    cfg = patch_onec_config(
        base_url=body.base_url,
        username=body.username,
        password=body.password,
    )
    await log_user_activity(db, user_id=int(admin.id), action="ADMIN_ONEC_CONFIG", detail=cfg.get("base_url"))
    await db.commit()
    return OneCConfigResponse(
        base_url=cfg.get("base_url"),
        username=cfg.get("username"),
        has_password=bool(cfg.get("password")),
    )


class OneCTestResponse(BaseModel):
    ok: bool
    latency_ms: float | None = None
    message: str | None = None


@router.post("/onec/test", response_model=OneCTestResponse)
async def onec_test_connection(_: User = Depends(require_admin)) -> OneCTestResponse:
    cfg = get_onec_config()
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        return OneCTestResponse(ok=False, message="Укажите URL сервера 1С в мастере подключения.")
    url = base if base.endswith("/health") else f"{base}/health"
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            auth = None
            user = cfg.get("username")
            pwd = cfg.get("password")
            if user and pwd:
                auth = (str(user), str(pwd))
            res = await client.get(url, auth=auth)
        ms = (time.perf_counter() - t0) * 1000.0
        if res.status_code >= 400:
            return OneCTestResponse(
                ok=False,
                latency_ms=ms,
                message=f"HTTP {res.status_code} от {url}",
            )
        return OneCTestResponse(ok=True, latency_ms=ms, message="Соединение с 1С установлено.")
    except Exception as e:
        return OneCTestResponse(ok=False, message=str(e))
