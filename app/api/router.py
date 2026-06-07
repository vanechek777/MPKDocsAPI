from fastapi import APIRouter

from app.api.routers import (
    admin,
    auth,
    config_public,
    departments,
    documents,
    health,
    releases_public,
    signatures,
    signing,
    staff_register,
    templates,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(releases_public.router)
api_router.include_router(config_public.router)
api_router.include_router(admin.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(departments.router)
api_router.include_router(documents.router)
api_router.include_router(signatures.router)
api_router.include_router(signing.router)
api_router.include_router(templates.router)
api_router.include_router(staff_register.router)

