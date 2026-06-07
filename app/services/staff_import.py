"""Импорт кадрового справочника из CSV / Excel."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Department, Position, StaffDirectoryEntry


@dataclass
class StaffImportStats:
    rows_total: int = 0
    rows_imported: int = 0
    departments_upserted: int = 0
    positions_upserted: int = 0
    staff_upserted: int = 0


_HEADER_ALIASES = {
    "фио": "full_name",
    "full_name": "full_name",
    "fullname": "full_name",
    "должность": "position",
    "position": "position",
    "отдел": "department",
    "department": "department",
}


def _normalize_header(h: str) -> str | None:
    key = (h or "").strip().lower().replace(" ", "_")
    return _HEADER_ALIASES.get(key)


def _parse_rows_from_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    mapping = {fn: _normalize_header(fn) for fn in reader.fieldnames}
    out: list[dict[str, str]] = []
    for row in reader:
        item: dict[str, str] = {}
        for src, dst in mapping.items():
            if dst and row.get(src):
                item[dst] = str(row[src]).strip()
        if item.get("full_name") and item.get("position") and item.get("department"):
            out.append(item)
    return out


def _parse_rows_from_xlsx(raw: bytes) -> list[dict[str, str]]:
    try:
        import openpyxl
    except ImportError as e:
        raise RuntimeError("Для .xlsx установите пакет openpyxl") from e

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(c or "").strip() for c in rows[0]]
    mapping = {i: _normalize_header(h) for i, h in enumerate(headers)}
    out: list[dict[str, str]] = []
    for row in rows[1:]:
        item: dict[str, str] = {}
        for i, cell in enumerate(row):
            dst = mapping.get(i)
            if dst and cell is not None:
                item[dst] = str(cell).strip()
        if item.get("full_name") and item.get("position") and item.get("department"):
            out.append(item)
    return out


def parse_staff_file(filename: str, raw: bytes) -> list[dict[str, str]]:
    lower = (filename or "").lower()
    if lower.endswith(".xlsx") or lower.endswith(".xlsm"):
        return _parse_rows_from_xlsx(raw)
    return _parse_rows_from_csv(raw)


async def _get_or_create_department(db: AsyncSession, name: str, stats: StaffImportStats) -> Department:
    row = (
        await db.execute(select(Department).where(func.lower(Department.Name) == name.lower()))
    ).scalar_one_or_none()
    if row is not None:
        return row
    dept = Department(Name=name, isActive=True)
    db.add(dept)
    await db.flush()
    stats.departments_upserted += 1
    return dept


async def _get_or_create_position(db: AsyncSession, name: str, stats: StaffImportStats) -> Position:
    row = (
        await db.execute(select(Position).where(func.lower(Position.Name) == name.lower()))
    ).scalar_one_or_none()
    if row is not None:
        return row
    pos = Position(Name=name, isActive=True)
    db.add(pos)
    await db.flush()
    stats.positions_upserted += 1
    return pos


async def import_staff_rows(db: AsyncSession, rows: list[dict[str, str]]) -> StaffImportStats:
    stats = StaffImportStats(rows_total=len(rows))
    for row in rows:
        full_name = row["full_name"].strip()
        pos = await _get_or_create_position(db, row["position"].strip(), stats)
        dept = await _get_or_create_department(db, row["department"].strip(), stats)
        existing = (
            await db.execute(
                select(StaffDirectoryEntry).where(
                    func.lower(StaffDirectoryEntry.FullName) == full_name.lower(),
                    StaffDirectoryEntry.PositionId == pos.id,
                    StaffDirectoryEntry.DepartmentId == dept.id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                StaffDirectoryEntry(
                    FullName=full_name,
                    PositionId=pos.id,
                    DepartmentId=dept.id,
                    isActive=True,
                )
            )
            stats.staff_upserted += 1
        else:
            existing.isActive = True
        stats.rows_imported += 1
    await db.commit()
    return stats
