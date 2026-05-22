import bcrypt, sys
print("PYTHON:", sys.executable)
print("BCRYPT FILE:", getattr(bcrypt, "__file__", None))
print("BCRYPT VERSION:", getattr(bcrypt, "__version__", None))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name)


    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    return app


app = create_app()
