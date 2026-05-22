"""Запись действий для админки (журнал)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserActivityLog


def _utc_naive() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


async def log_user_activity(
    db: AsyncSession,
    *,
    user_id: int | None,
    action: str,
    detail: str | None = None,
) -> None:
    act = (action or "")[:80]
    det = (detail or "")[:4000] if detail else None
    db.add(UserActivityLog(UserId=user_id, Action=act, Detail=det, CreatedAt=_utc_naive()))
