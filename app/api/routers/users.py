from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.admin_access import user_is_admin
from app.db.models import Department, Position, User
from app.db.session import get_db

router = APIRouter(prefix="/users", tags=["users"])


class MeResponse(BaseModel):
    id: int
    phone_number: str
    full_name: str
    email: str | None = None
    department: str | None = None
    position: str | None = None
    is_admin: bool = False


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    row = (
        await db.execute(
            select(
                User.id,
                User.PhoneNumber,
                User.FullName,
                User.Email,
                Department.Name.label("department_name"),
                Position.Name.label("position_name"),
            )
            .outerjoin(Department, Department.id == User.DepartmentId)
            .outerjoin(Position, Position.id == User.PositionId)
            .where(User.id == user.id),
        )
    ).one()
    u = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
    return MeResponse(
        id=int(row.id),
        phone_number=row.PhoneNumber,
        full_name=row.FullName,
        email=row.Email,
        department=row.department_name,
        position=row.position_name,
        is_admin=user_is_admin(u),
    )


class MePatchRequest(BaseModel):
    full_name: str | None = None
    phone_number: str | None = None
    email: str | None = None

    @field_validator("full_name", "phone_number", "email", mode="before")
    @classmethod
    def strip_strings(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


@router.patch("/me", response_model=MeResponse)
async def patch_me(
    payload: MePatchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    if payload.full_name is not None:
        if not payload.full_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Пустое ФИО",
            )
        user.FullName = payload.full_name

    if payload.phone_number is not None:
        if not payload.phone_number:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Пустой телефон",
            )
        if payload.phone_number != user.PhoneNumber:
            taken = (
                await db.execute(select(User.id).where(User.PhoneNumber == payload.phone_number))
            ).scalar_one_or_none()
            if taken is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Этот номер телефона уже занят",
                )
            user.PhoneNumber = payload.phone_number

    if payload.email is not None:
        norm = payload.email.lower() if payload.email else None
        if norm:
            taken = (
                await db.execute(
                    select(User.id).where(
                        User.Email.is_not(None),
                        func.lower(User.Email) == norm,
                        User.id != user.id,
                    ),
                )
            ).scalar_one_or_none()
            if taken is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Этот email уже занят",
                )
            user.Email = norm
        else:
            user.Email = None

    await db.commit()

    row = (
        await db.execute(
            select(
                User.id,
                User.PhoneNumber,
                User.FullName,
                User.Email,
                Department.Name.label("department_name"),
                Position.Name.label("position_name"),
            )
            .outerjoin(Department, Department.id == User.DepartmentId)
            .outerjoin(Position, Position.id == User.PositionId)
            .where(User.id == user.id),
        )
    ).one()
    u = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
    return MeResponse(
        id=int(row.id),
        phone_number=row.PhoneNumber,
        full_name=row.FullName,
        email=row.Email,
        department=row.department_name,
        position=row.position_name,
        is_admin=user_is_admin(u),
    )


class UserListItem(BaseModel):
    id: int
    full_name: str
    department: str | None = None
    position: str | None = None


@router.get("", response_model=list[UserListItem])
async def list_users(
    active_only: bool = True,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(
            User.id,
            User.FullName,
            Department.Name.label("department_name"),
            Position.Name.label("position_name"),
            User.Status,
        )
        .outerjoin(Department, Department.id == User.DepartmentId)
        .outerjoin(Position, Position.id == User.PositionId)
        .order_by(User.FullName.asc())
    )
    if active_only:
        stmt = stmt.where(User.Status.is_(True))

    rows = (await db.execute(stmt)).all()
    return [
        UserListItem(
            id=int(r.id),
            full_name=r.FullName,
            department=r.department_name,
            position=r.position_name,
        )
        for r in rows
    ]

