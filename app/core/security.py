from datetime import datetime, timedelta, timezone

try:
    # Must come from `python-jose`, NOT the unrelated `jose` package.
    from jose import jwt  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "JWT dependency import failed. Install `python-jose[cryptography]` "
        "and remove the unrelated `jose` package.\n"
        "Fix (Windows):\n"
        "  .\\.venv\\Scripts\\python -m pip uninstall -y jose\n"
        "  .\\.venv\\Scripts\\python -m pip install -r requirements1.txt\n"
    ) from e
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        # bcrypt rejects passwords longer than 72 bytes; treat as invalid credentials
        return False


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(*, subject: str) -> str:
    now = datetime.now(tz=timezone.utc)
    exp = now + timedelta(minutes=settings.jwt_expires_minutes)
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

