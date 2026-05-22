"""Формат обмена НЭП-подписью (.esig) — JSON UTF-8."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ESIG_FORMAT = "mpk-esig"
ESIG_VERSION = 1


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_esig_payload(
    *,
    document_id: int,
    template_id: int,
    document_hash_hex: str,
    signer_user_id: int,
    signature_hex: str,
    signed_at: datetime | None,
    document_title: str | None,
) -> dict[str, Any]:
    return {
        "format": ESIG_FORMAT,
        "version": ESIG_VERSION,
        "document_id": int(document_id),
        "template_id": int(template_id),
        "document_hash_hex": document_hash_hex,
        "signer_user_id": int(signer_user_id),
        "signature_hex": signature_hex,
        "signed_at_utc": _iso_utc(signed_at),
        "document_title": (document_title or "").strip() or None,
    }


def esig_to_bytes(payload: dict[str, Any]) -> bytes:
    s = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    return s.encode("utf-8")


@dataclass
class ParsedEsig:
    document_id: int
    template_id: int
    document_hash_hex: str
    signer_user_id: int
    signature_hex: str
    signed_at_utc: str | None
    document_title: str | None


def parse_esig_bytes(raw: bytes) -> ParsedEsig:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("Файл не является корректным JSON") from e
    if not isinstance(data, dict):
        raise ValueError("Ожидался JSON-объект")
    if data.get("format") != ESIG_FORMAT:
        raise ValueError("Неизвестный формат подписи")
    ver = data.get("version")
    if ver != ESIG_VERSION:
        raise ValueError(f"Неподдерживаемая версия: {ver!r}")
    try:
        return ParsedEsig(
            document_id=int(data["document_id"]),
            template_id=int(data["template_id"]),
            document_hash_hex=str(data["document_hash_hex"]).strip().lower(),
            signer_user_id=int(data["signer_user_id"]),
            signature_hex=str(data["signature_hex"]).strip().lower(),
            signed_at_utc=(str(data["signed_at_utc"]).strip() if data.get("signed_at_utc") else None),
            document_title=(str(data["document_title"]).strip() if data.get("document_title") else None),
        )
    except KeyError as e:
        raise ValueError(f"В файле не хватает поля: {e}") from e
