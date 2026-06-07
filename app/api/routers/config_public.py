from fastapi import APIRouter

from app.core.runtime_config import get_api_endpoints, get_app_release

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/api-endpoints")
async def public_api_endpoints():
    """Список базовых URL API для клиентов (без авторизации, для экрана входа)."""
    items = get_api_endpoints()
    return {
        "endpoints": [
            {"url": e["url"], "label": e.get("label")}
            for e in items
        ]
    }


@router.get("/app-release")
async def public_app_release():
    """Актуальная версия клиента (без авторизации)."""
    rel = get_app_release()
    if not rel:
        return {"configured": False}
    return {
        "configured": True,
        "version": rel.get("version"),
        "build": rel.get("build"),
        "min_build": rel.get("min_build", 0),
        "mandatory": bool(rel.get("mandatory")),
        "notes": rel.get("notes"),
        "windows_url": rel.get("windows_url"),
        "android_url": rel.get("android_url"),
        "ios_url": rel.get("ios_url"),
        "web_url": rel.get("web_url"),
    }
