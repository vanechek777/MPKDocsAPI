"""Правила кадрового справочника и регистрации."""

from __future__ import annotations

# Отделы, под которыми нельзя регистрироваться (сравнение без учёта регистра).
REGISTRATION_BLOCKED_DEPARTMENT_NAMES: frozenset[str] = frozenset({"администратор"})


def is_department_registration_blocked(name: str | None) -> bool:
    return (name or "").strip().casefold() in REGISTRATION_BLOCKED_DEPARTMENT_NAMES
