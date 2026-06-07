"""Хранение установщиков клиента на диске (/data/releases на Amvera)."""

from __future__ import annotations

import os
import re
from pathlib import Path

_MAX_BYTES = 200 * 1024 * 1024
_ALLOWED_EXT = {
    "windows": {".exe", ".msi", ".zip"},
    "android": {".apk", ".aab"},
    "ios": {".ipa"},
    "web": {".zip", ".exe"},
}
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def releases_dir() -> Path:
    if os.environ.get("AMVERA") == "1":
        return Path("/data/releases")
    data_dir = Path("/data")
    if data_dir.is_dir():
        return data_dir / "releases"
    return Path(__file__).resolve().parents[2] / "data" / "releases"


def ensure_releases_dir() -> Path:
    path = releases_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._")
    return cleaned or "release.bin"


def build_release_filename(version: str, build: int, platform: str, original_name: str) -> str:
    ext = Path(original_name).suffix.lower() or ".bin"
    safe_ver = _SAFE_NAME.sub("_", version.strip()) or "0"
    return f"MPKDocuments-{safe_ver}-b{build}-{platform}{ext}"


def validate_release_file(platform: str, filename: str, size: int) -> None:
    key = (platform or "").strip().lower()
    if key not in _ALLOWED_EXT:
        raise ValueError("platform должен быть: windows, android, ios или web.")
    if size <= 0:
        raise ValueError("Пустой файл.")
    if size > _MAX_BYTES:
        raise ValueError(f"Файл слишком большой (макс. {_MAX_BYTES // (1024 * 1024)} МБ).")
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXT[key]:
        allowed = ", ".join(sorted(_ALLOWED_EXT[key]))
        raise ValueError(f"Для {key} допустимы: {allowed}.")


def save_release_file(*, platform: str, version: str, build: int, filename: str, data: bytes) -> Path:
    validate_release_file(platform, filename, len(data))
    target_name = build_release_filename(version, build, platform, filename)
    folder = ensure_releases_dir()
    path = folder / target_name
    path.write_bytes(data)
    return path


def list_release_files() -> list[dict]:
    folder = ensure_releases_dir()
    items: list[dict] = []
    for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        st = p.stat()
        items.append(
            {
                "name": p.name,
                "size_bytes": st.st_size,
                "modified_utc": int(st.st_mtime),
            }
        )
    return items


def bump_patch_version(version: str) -> str:
    parts = (version or "1.0.0").strip().split(".")
    if len(parts) >= 3 and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    if len(parts) == 2 and parts[-1].isdigit():
        parts.append("1")
        return ".".join(parts)
    return "1.0.1"


def suggest_next_release() -> dict:
    from app.core.runtime_config import get_app_release

    current = get_app_release()
    cur_build = int(current.get("build") or 0)
    cur_version = str(current.get("version") or "1.0.0")
    return {
        "version": bump_patch_version(cur_version),
        "build": max(1, cur_build + 1),
        "previous_version": cur_version if current else None,
        "previous_build": cur_build if current else None,
    }
