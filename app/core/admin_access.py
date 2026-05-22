"""Кто считается администратором: флаг в БД и/или MPK_ADMIN_USER_IDS в настройках."""

from __future__ import annotations

from app.core.config import settings
from app.db.models import User


def env_admin_user_ids() -> set[int]:
    raw = (settings.mpk_admin_user_ids or "").strip()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def user_is_admin(user: User) -> bool:
    if bool(getattr(user, "IsAdmin", False)):
        return True
    return int(user.id) in env_admin_user_ids()
