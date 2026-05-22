from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import DocumentCategory, DocumentTemplate, User
from app.db.session import get_db

router = APIRouter(prefix="/templates", tags=["templates"])


class TemplateListItem(BaseModel):
    id: int
    name: str
    category: str | None = None
    is_active: bool | None


@router.get("/category-names", response_model=list[str])
async def list_active_category_names(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Имена активных категорий из справочника (в т.ч. без шаблонов) — для мастера «Создать документ»."""
    stmt = (
        select(DocumentCategory.Name)
        .where(or_(DocumentCategory.isActive.is_(True), DocumentCategory.isActive.is_(None)))
        .order_by(DocumentCategory.SortOrder.asc(), DocumentCategory.Name.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [str(r[0]) for r in rows if r[0]]


@router.get("", response_model=list[TemplateListItem])
async def list_templates(
    active_only: bool = True,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = (await db.execute(select(DocumentTemplate).where(DocumentTemplate.id == template_id))).scalar_one()
    return TemplateDetail(id=t.id, name=t.Name, form_schema=t.FormSchema, template_path=t.TemplatePath)

