"""Локальные настройки админки (подключение 1С), JSON на диске."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

_lock = Lock()


def _config_path() -> Path:
    """На Amvera постоянное хранилище — /data (см. persistenceMount в amvera.yml)."""
    if os.environ.get("AMVERA") == "1":
        return Path("/data/runtime_config.json")
    data_dir = Path("/data")
    if data_dir.is_dir():
        return data_dir / "runtime_config.json"
    return Path(__file__).resolve().parents[2] / "data" / "runtime_config.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_runtime_config() -> dict:
    path = _config_path()
    with _lock:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


def save_runtime_config(data: dict) -> None:
    path = _config_path()
    with _lock:
        _ensure_parent(path)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_onec_config() -> dict:
    cfg = load_runtime_config().get("onec", {})
    if not isinstance(cfg, dict):
        return {}
    return cfg


def _normalize_api_url(url: str | None) -> str | None:
    if not url or not str(url).strip():
        return None
    trimmed = str(url).strip()
    if not trimmed.startswith(("http://", "https://")):
        return None
    return trimmed.rstrip("/")


def _parse_api_endpoints_raw(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        u = _normalize_api_url(item.get("url"))
        if not u:
            continue
        key = u.lower()
        if key in seen:
            continue
        seen.add(key)
        label = item.get("label")
        lbl = str(label).strip() if isinstance(label, str) and label.strip() else None
        out.append({"url": u, "label": lbl})
    return out


def get_api_endpoints() -> list[dict]:
    from_file = _parse_api_endpoints_raw(load_runtime_config().get("api_endpoints", []))
    if from_file:
        return from_file

    env_json = (os.environ.get("MPK_API_ENDPOINTS_JSON") or "").strip()
    if env_json:
        try:
            parsed = json.loads(env_json)
            from_env = _parse_api_endpoints_raw(parsed)
            if from_env:
                return from_env
        except json.JSONDecodeError:
            pass

    return []


def set_api_endpoints(endpoints: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()
    for item in endpoints:
        if not isinstance(item, dict):
            continue
        u = _normalize_api_url(item.get("url"))
        if not u:
            continue
        key = u.lower()
        if key in seen:
            continue
        seen.add(key)
        label = item.get("label")
        lbl = str(label).strip() if isinstance(label, str) and label.strip() else None
        normalized.append({"url": u, "label": lbl})
    root = load_runtime_config()
    root["api_endpoints"] = normalized
    save_runtime_config(root)
    return normalized


def _normalize_release_url(url: str | None) -> str | None:
    return _normalize_api_url(url)


def get_app_release() -> dict:
    """Актуальная версия клиента для проверки обновлений."""
    from_file = load_runtime_config().get("app_release", {})
    if isinstance(from_file, dict) and from_file.get("version"):
        return dict(from_file)

    env_json = (os.environ.get("MPK_APP_RELEASE_JSON") or "").strip()
    if env_json:
        try:
            parsed = json.loads(env_json)
            if isinstance(parsed, dict) and parsed.get("version"):
                return parsed
        except json.JSONDecodeError:
            pass

    return {}


def set_app_release(data: dict) -> dict:
    version = str(data.get("version") or "").strip()
    if not version:
        raise ValueError("Укажите version (например 1.0.1).")

    try:
        build = int(data.get("build") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("build должен быть целым числом.") from exc
    if build < 1:
        raise ValueError("build должен быть >= 1.")

    try:
        min_build = int(data.get("min_build") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_build должен быть целым числом.") from exc

    notes = data.get("notes")
    notes_str = str(notes).strip() if isinstance(notes, str) and notes.strip() else None

    normalized = {
        "version": version,
        "build": build,
        "min_build": max(0, min_build),
        "mandatory": bool(data.get("mandatory")),
        "notes": notes_str,
        "windows_url": _normalize_release_url(data.get("windows_url")),
        "android_url": _normalize_release_url(data.get("android_url")),
        "ios_url": _normalize_release_url(data.get("ios_url")),
        "web_url": _normalize_release_url(data.get("web_url")),
    }

    root = load_runtime_config()
    root["app_release"] = normalized
    save_runtime_config(root)
    return normalized


def patch_onec_config(*, base_url: str | None, username: str | None, password: str | None) -> dict:
    root = load_runtime_config()
    onec = dict(root.get("onec") or {})
    if base_url is not None:
        onec["base_url"] = base_url.strip() or None
    if username is not None:
        onec["username"] = username.strip() or None
    if password is not None and password.strip():
        onec["password"] = password.strip()
    root["onec"] = onec
    save_runtime_config(root)
    return onec
