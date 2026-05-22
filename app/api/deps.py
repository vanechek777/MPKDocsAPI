from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
try:
    from jose import JWTError, jwt  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "JWT dependency import failed. Install `python-jose[cryptography]` "
        "and remove the unrelated `jose` package.\n"
        "Fix (Windows):\n"
        "  .\\.venv\\Scripts\\python -m pip uninstall -y jose\n"
        "  .\\.venv\\Scripts\\python -m pip install -r requirements1.txt\n"
    ) from e
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.admin_access import user_is_admin
from app.core.config import settings
from app.core import presence
from app.db.session import get_db
from app.db.models import User


bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token = creds.credentials
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        user_id = int(sub)
    except (JWTError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    presence.touch(int(user.id))
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user_is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нужны права администратора")
    return user

