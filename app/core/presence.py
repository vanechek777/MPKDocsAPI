"""Грубая оценка «онлайн»: последний успешный запрос с JWT (touch в get_current_user)."""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_last_seen: dict[int, float] = {}


def touch(user_id: int) -> None:
    with _lock:
        _last_seen[int(user_id)] = time.time()


def online_within_seconds(seconds: float = 300) -> int:
    cutoff = time.time() - seconds
    with _lock:
        return sum(1 for t in _last_seen.values() if t >= cutoff)
