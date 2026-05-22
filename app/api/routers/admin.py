"""Админ-панель: пользователи, категории, шаблоны, сводка и журнал."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.admin_access import env_admin_user_ids, user_is_admin
from app.core.audit import log_user_activity
from app.core.presence import online_within_seconds
from app.db.models import Document, DocumentCategory, DocumentTemplate, User, UserActivityLog
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


@router.get("/activity", response_model=list[ActivityItem])
async def admin_activity(
    limit: int = 40,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[ActivityItem]:
    limit = min(max(limit, 1), 200)
    rows = (
        await db.execute(
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
    ).all()
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
