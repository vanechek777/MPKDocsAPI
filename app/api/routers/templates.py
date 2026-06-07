from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import Department, DocumentCategory, DocumentTemplate, Position, User
from app.db.session import get_db

router = APIRouter(prefix="/templates", tags=["templates"])


def _template_access_allows(
    form_schema: object,
    *,
    position_name: str | None,
    department_name: str | None,
) -> bool:
    """Доступ только если в FormSchema задан access и пользователь подходит."""
    if not isinstance(form_schema, dict):
        return False
    access = form_schema.get("access")
    if not isinstance(access, dict) or not access:
        return False

    positions = access.get("positions")
    if isinstance(positions, list) and positions:
        if not position_name or position_name not in positions:
            return False

    departments = access.get("departments")
    if isinstance(departments, list) and departments:
        if not department_name or department_name not in departments:
            return False

    exclude_positions = access.get("exclude_positions")
    if isinstance(exclude_positions, list) and position_name and position_name in exclude_positions:
        return False

    exclude_departments = access.get("exclude_departments")
    if isinstance(exclude_departments, list) and department_name and department_name in exclude_departments:
        return False

    return True


async def _resolve_user_org(
    db: AsyncSession,
    user: User,
) -> tuple[str | None, str | None]:
    position_name = department_name = None
    if user.PositionId is not None:
        position_name = (
            await db.execute(select(Position.Name).where(Position.id == user.PositionId))
        ).scalar_one_or_none()
    if user.DepartmentId is not None:
        department_name = (
            await db.execute(select(Department.Name).where(Department.id == user.DepartmentId))
        ).scalar_one_or_none()
    return position_name, department_name


def _user_can_access_template(
    user: User,
    form_schema: object,
    *,
    position_name: str | None,
    department_name: str | None,
) -> bool:
    if bool(getattr(user, "IsAdmin", False)):
        return True
    return _template_access_allows(
        form_schema,
        position_name=position_name,
        department_name=department_name,
    )


class TemplateListItem(BaseModel):
    id: int
    name: str
    category: str | None = None
    is_active: bool | None


@router.get("/category-names", response_model=list[str])
async def list_active_category_names(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Категории, доступные текущему пользователю (по шаблонам с access в FormSchema)."""
    position_name, department_name = await _resolve_user_org(db, user)
    stmt = (
        select(DocumentTemplate, DocumentCategory.Name.label("cat_table_name"))
        .outerjoin(DocumentCategory, DocumentCategory.id == DocumentTemplate.CategoryId)
        .where(DocumentTemplate.isActive.is_(True))
        .order_by(DocumentCategory.SortOrder.asc(), DocumentCategory.Name.asc(), DocumentTemplate.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    seen: set[str] = set()
    ordered: list[str] = []
    for t, cat_table in rows:
        if not _user_can_access_template(
            user,
            t.FormSchema,
            position_name=position_name,
            department_name=department_name,
        ):
            continue
        cat = cat_table
        if not cat and isinstance(t.FormSchema, dict):
            cat = t.FormSchema.get("category")
        if not cat:
            continue
        cat = str(cat)
        if cat not in seen:
            seen.add(cat)
            ordered.append(cat)
    return ordered


@router.get("", response_model=list[TemplateListItem])
async def list_templates(
    active_only: bool = True,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    position_name, department_name = await _resolve_user_org(db, user)
    stmt = (
        select(DocumentTemplate, DocumentCategory.Name.label("cat_table_name"))
        .outerjoin(DocumentCategory, DocumentCategory.id == DocumentTemplate.CategoryId)
        .order_by(DocumentTemplate.id.asc())
    )
    if active_only:
        stmt = stmt.where(DocumentTemplate.isActive.is_(True))
    rows = (await db.execute(stmt)).all()
    items: list[TemplateListItem] = []
    for t, cat_table in rows:
        if not _user_can_access_template(
            user,
            t.FormSchema,
            position_name=position_name,
            department_name=department_name,
        ):
            continue
        cat = cat_table
        if not cat:
            try:
                if isinstance(t.FormSchema, dict):
                    cat = t.FormSchema.get("category")
            except Exception:
                cat = None

        if not cat:
            if isinstance(t.Name, str) and t.Name.strip().lower().startswith("отчет"):
                cat = "Отчет"
            elif isinstance(t.Name, str) and t.Name.strip().lower().startswith("приказ"):
                cat = "Приказ"
        items.append(TemplateListItem(id=t.id, name=t.Name, category=cat, is_active=t.isActive))
    return items


class TemplateDetail(BaseModel):
    id: int
    name: str
    form_schema: dict
    template_path: str


@router.get("/{template_id}", response_model=TemplateDetail)
async def get_template(
    template_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = (await db.execute(select(DocumentTemplate).where(DocumentTemplate.id == template_id))).scalar_one()
    position_name, department_name = await _resolve_user_org(db, user)
    if not _user_can_access_template(
        user,
        t.FormSchema,
        position_name=position_name,
        department_name=department_name,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Шаблон не найден")
    return TemplateDetail(id=t.id, name=t.Name, form_schema=t.FormSchema, template_path=t.TemplatePath)

