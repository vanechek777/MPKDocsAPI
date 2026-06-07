"""Публичная страница со списком релизов клиента."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.core.releases import list_release_files
from app.core.runtime_config import get_api_endpoints, get_app_release

router = APIRouter(tags=["releases"])


def _resolve_public_base_url(request: Request) -> str:
    if settings.public_base_url and str(settings.public_base_url).strip():
        return str(settings.public_base_url).strip().rstrip("/")
    endpoints = get_api_endpoints()
    if endpoints and endpoints[0].get("url"):
        return str(endpoints[0]["url"]).rstrip("/")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _format_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _render_page(*, base_url: str, release: dict, files: list[dict]) -> str:
    platforms: list[tuple[str, str, str]] = [
        ("Windows", release.get("windows_url"), "windows"),
        ("Android", release.get("android_url"), "android"),
        ("iOS", release.get("ios_url"), "ios"),
        ("Web", release.get("web_url"), "web"),
    ]

    current_block = ""
    if release.get("version"):
        version = html.escape(str(release["version"]))
        build = html.escape(str(release.get("build") or ""))
        notes = release.get("notes")
        notes_html = ""
        if isinstance(notes, str) and notes.strip():
            notes_html = (
                f'<p class="notes">{html.escape(notes.strip()).replace(chr(10), "<br>")}</p>'
            )

        links_html = ""
        for label, url, _ in platforms:
            if url and str(url).strip():
                safe_url = html.escape(str(url).strip())
                links_html += (
                    f'<li><a href="{safe_url}">{html.escape(label)}</a></li>'
                )

        links_section = ""
        if links_html:
            links_section = f'<ul class="platform-links">{links_html}</ul>'

        mandatory = ""
        if release.get("mandatory"):
            mandatory = '<span class="badge">обязательное обновление</span>'

        current_block = f"""
        <section class="card">
          <h2>Текущая версия</h2>
          <p class="version">v{version} <span class="muted">(сборка {build})</span> {mandatory}</p>
          {notes_html}
          {links_section}
        </section>
        """

    file_rows = ""
    for item in files:
        name = html.escape(item["name"])
        href = html.escape(f"{base_url}/releases/{item['name']}")
        size = html.escape(_format_size(int(item["size_bytes"])))
        modified = html.escape(_format_utc(int(item["modified_utc"])))
        file_rows += f"""
        <li class="file-row">
          <a class="file-name" href="{href}">{name}</a>
          <span class="file-meta">{size} · {modified}</span>
        </li>
        """

    if not file_rows:
        file_rows = '<li class="empty">Пока нет загруженных файлов.</li>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>МПК.Документы — релизы</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f4f6f8;
      --card: #ffffff;
      --text: #1a1d21;
      --muted: #5c6670;
      --border: #d8dee4;
      --link: #0b6bcb;
      --accent: #00a000;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0f1114;
        --card: #1a1d23;
        --text: #e8eaed;
        --muted: #9aa3ad;
        --border: #2d333b;
        --link: #58a6ff;
        --accent: #3ddc63;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .wrap {{
      max-width: 720px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 1.5rem;
      font-weight: 700;
    }}
    .lead {{
      margin: 0 0 24px;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 1rem;
      font-weight: 600;
    }}
    .version {{
      margin: 0 0 8px;
      font-size: 1.1rem;
      font-weight: 600;
    }}
    .muted {{ color: var(--muted); font-weight: 500; }}
    .badge {{
      display: inline-block;
      font-size: 0.75rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 999px;
      background: rgba(255, 120, 0, 0.15);
      color: #c45c00;
      vertical-align: middle;
    }}
    @media (prefers-color-scheme: dark) {{
      .badge {{ color: #ffb347; }}
    }}
    .notes {{
      margin: 8px 0 12px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .platform-links {{
      margin: 0;
      padding: 0;
      list-style: none;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .platform-links a {{
      display: inline-block;
      padding: 8px 14px;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.9rem;
    }}
    .file-list {{
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .file-row {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: 14px 0;
      border-bottom: 1px solid var(--border);
    }}
    .file-row:last-child {{ border-bottom: none; }}
    .file-name {{
      color: var(--link);
      text-decoration: none;
      font-weight: 500;
      word-break: break-all;
    }}
    .file-name:hover {{ text-decoration: underline; }}
    .file-meta {{
      font-size: 0.85rem;
      color: var(--muted);
    }}
    .empty {{
      padding: 12px 0;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>МПК.Документы</h1>
    <p class="lead">Загрузки клиента</p>
    {current_block}
    <section class="card">
      <h2>Все файлы</h2>
      <ul class="file-list">
        {file_rows}
      </ul>
    </section>
  </div>
</body>
</html>"""


@router.get("/releases", response_class=HTMLResponse, include_in_schema=False)
@router.get("/releases/", response_class=HTMLResponse, include_in_schema=False)
async def releases_index(request: Request) -> HTMLResponse:
    base_url = _resolve_public_base_url(request)
    release = get_app_release()
    files = list_release_files()
    body = _render_page(base_url=base_url, release=release, files=files)
    return HTMLResponse(content=body)
