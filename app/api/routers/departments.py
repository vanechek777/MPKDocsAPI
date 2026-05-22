from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import Department, User
from app.db.session import get_db

router = APIRouter(prefix="/departments", tags=["departments"])


class DepartmentListItem(BaseModel):
    id: int
    name: str


@router.get("", response_model=list[DepartmentListItem])
async def list_departments(
    active_only: bool = True,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Department.id, Department.Name).order_by(Department.Name.asc())
    if active_only:
        stmt = stmt.where(Department.isActive.is_(True))
    rows = (await db.execute(stmt)).all()
    return [DepartmentListItem(id=int(r.id), name=r.Name) for r in rows]

