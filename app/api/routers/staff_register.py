"""Публичные методы кадрового справочника для формы регистрации."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Department, Position, StaffDirectoryEntry
from app.db.session import get_db

router = APIRouter(prefix="/public/staff", tags=["staff-register"])


class StaffSuggestItem(BaseModel):
    id: int
    full_name: str
    position_id: int
    position_name: str
    department_id: int
    department_name: str


class StaffPositionItem(BaseModel):
    id: int
    name: str


class StaffDepartmentItem(BaseModel):
    id: int
    name: str


@router.get("/suggest", response_model=list[StaffSuggestItem])
async def suggest_staff(
    q: str = "",
    limit: int = Query(12, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
) -> list[StaffSuggestItem]:
    needle = (q or "").strip()
    if len(needle) < 2:
        return []

    stmt = (
        select(
            StaffDirectoryEntry.id,
            StaffDirectoryEntry.FullName,
            StaffDirectoryEntry.PositionId,
            Position.Name.label("position_name"),
            StaffDirectoryEntry.DepartmentId,
            Department.Name.label("department_name"),
        )
        .join(Position, Position.id == StaffDirectoryEntry.PositionId)
        .join(Department, Department.id == StaffDirectoryEntry.DepartmentId)
        .where(
            StaffDirectoryEntry.isActive.is_(True),
            func.lower(StaffDirectoryEntry.FullName).like(f"%{needle.lower()}%"),
        )
        .order_by(StaffDirectoryEntry.FullName.asc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        StaffSuggestItem(
            id=int(r.id),
            full_name=r.FullName,
            position_id=int(r.PositionId),
            position_name=r.position_name,
            department_id=int(r.DepartmentId),
            department_name=r.department_name,
        )
        for r in rows
    ]


@router.get("/positions", response_model=list[StaffPositionItem])
async def list_staff_positions(db: AsyncSession = Depends(get_db)) -> list[StaffPositionItem]:
    stmt = (
        select(Position.id, Position.Name)
        .join(StaffDirectoryEntry, StaffDirectoryEntry.PositionId == Position.id)
        .where(StaffDirectoryEntry.isActive.is_(True), Position.isActive.is_(True))
        .group_by(Position.id, Position.Name)
        .order_by(Position.Name.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [StaffPositionItem(id=int(r.id), name=r.Name) for r in rows]


@router.get("/departments", response_model=list[StaffDepartmentItem])
async def list_staff_departments(
    position_id: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
) -> list[StaffDepartmentItem]:
    stmt = (
        select(Department.id, Department.Name)
        .join(StaffDirectoryEntry, StaffDirectoryEntry.DepartmentId == Department.id)
        .where(
            StaffDirectoryEntry.isActive.is_(True),
            StaffDirectoryEntry.PositionId == position_id,
            Department.isActive.is_(True),
        )
        .group_by(Department.id, Department.Name)
        .order_by(Department.Name.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [StaffDepartmentItem(id=int(r.id), name=r.Name) for r in rows]
