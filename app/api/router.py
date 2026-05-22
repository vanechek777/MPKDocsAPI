from fastapi import APIRouter

from app.api.routers import admin, auth, departments, documents, health, signatures, signing, templates, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(admin.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(departments.router)
api_router.include_router(documents.router)
api_router.include_router(signatures.router)
api_router.include_router(signing.router)
api_router.include_router(templates.router)

