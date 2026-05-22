import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.audit import log_user_activity
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.core.nep import generate_keypair
from app.core.otp import OtpCooldownError, issue_code_for_user
from app.core.smsc import SmscSendError, format_otp_sms_text, send_sms_text, smsc_configured
from app.core.twilio_sms import TwilioSendError, send_sms_twilio, twilio_configured
from app.core.vonage_sms import VonageSendError, send_sms_vonage, vonage_configured
from app.core.smsru_sms import SmsRuSendError, send_sms_smsru, smsru_configured
from app.core.email_auth_otp import (
    issue_email_login_code,
    issue_register_email_code,
    verify_email_login_consume,
    verify_register_email_consume,
)
from app.core.smtp_otp import (
    SmtpSendError,
    build_otp_email_plain_and_html,
    build_registration_otp_email_plain_and_html,
    send_otp_email,
    smtp_configured,
)
from app.db.models import SignatureProfile, User
from app.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

_logger = logging.getLogger(__name__)

# Не используем pydantic.EmailStr: он требует пакет email-validator при загрузке типов.
_REGISTER_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)


def _normalize_register_email(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError("Укажите email")
    s = v.strip().lower()
    if not s or len(s) > 255:
        raise ValueError("Некорректный email")
    if not _REGISTER_EMAIL_RE.fullmatch(s):
        raise ValueError("Некорректный email")
    return s


class LoginRequest(BaseModel):
    phone_number: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    user = (
        await db.execute(select(User).where(User.PhoneNumber == payload.phone_number))
    ).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.PasswordHash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    await log_user_activity(db, user_id=int(user.id), action="LOGIN_PASSWORD", detail=None)
    await db.commit()
    return TokenResponse(access_token=create_access_token(subject=str(user.id)))


class RegisterRequest(BaseModel):
    phone_number: str
    full_name: str
    password: str
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: object) -> str:
        return _normalize_register_email(v)


class RegisterResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> RegisterResponse:
    existing = (
        await db.execute(select(User.id).where(User.PhoneNumber == payload.phone_number))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone number already registered")

    email_taken = (
        await db.execute(
            select(User.id).where(
                User.Email.is_not(None),
                func.lower(User.Email) == payload.email,
            )
        )
    ).scalar_one_or_none()
    if email_taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        PhoneNumber=payload.phone_number,
        Email=payload.email,
        FullName=payload.full_name,
        Status=True,
        PasswordHash=hash_password(payload.password),
        IsAdmin=False,
    )
    db.add(user)
    await db.flush()

    # Create NEP signature profile on registration (so user can sign later).
    pub_b64, priv_enc_b64 = generate_keypair()
    db.add(
        SignatureProfile(
            UserId=user.id,
            PublicKey=pub_b64,
            EncryptedPrivateKey=priv_enc_b64,
            isRevoked=False,
        )
    )

    await log_user_activity(db, user_id=int(user.id), action="REGISTER", detail=payload.phone_number)
    await db.commit()
    await db.refresh(user)

    return RegisterResponse(access_token=create_access_token(subject=str(user.id)))


class OtpSendResponse(BaseModel):
    ok: bool = True
    dev_code: str | None = Field(
        default=None,
        description="Только при OTP_DEV_MODE и OTP_DEV_RETURN_CODE; для отладки без SMS.",
    )


@router.post("/otp/send", response_model=OtpSendResponse)
async def send_signing_otp(user: User = Depends(get_current_user)) -> OtpSendResponse:
    """Код: по SMS (SMSC/smsru/…) или на email через SMTP (Яндекс и др.)."""
    try:
        code = await issue_code_for_user(user.id)
    except OtpCooldownError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Wait before requesting another code",
        ) from None

    if settings.otp_dev_mode:
        _logger.warning(
            "OTP dev mode (SMS отключена): user_id=%s phone=%s code=%s",
            user.id,
            user.PhoneNumber,
            code,
        )
        return OtpSendResponse(
            ok=True,
            dev_code=code if settings.otp_dev_return_code else None,
        )

    provider = (settings.sms_provider or "smsc").strip().lower()
    mes = format_otp_sms_text(settings.smsc_otp_message_template, code)

    if provider == "email":
        if not smtp_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "SMTP не настроен: EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD (или SMTP_*). Для Яндекса см. .env.example. "
                    "Или SMS_PROVIDER=smsc. Локально: OTP_DEV_MODE=true."
                ),
            )
        email_to = (user.Email or "").strip()
        if not email_to:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Укажите email в профиле: PATCH /users/me с полем email.",
            )
        plain, html_body = build_otp_email_plain_and_html(code)
        try:
            await send_otp_email(
                to_addr=email_to,
                subject=settings.otp_email_subject,
                body_plain=plain,
                body_html=html_body,
            )
        except SmtpSendError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            ) from e
        return OtpSendResponse()

    if provider == "twilio":
        if not twilio_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Twilio: задайте TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN и TWILIO_FROM_NUMBER. "
                    "Или SMS_PROVIDER=smsc и учётные данные SMSC. "
                    "Локально без SMS: OTP_DEV_MODE=true."
                ),
            )
        try:
            await send_sms_twilio(to_phone=user.PhoneNumber, text=mes)
        except TwilioSendError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            ) from e
        return OtpSendResponse()

    if provider == "smsc":
        if not smsc_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "SMSC не настроен (SMSC_API_KEY или SMSC_LOGIN+SMSC_PASSWORD). "
                    "Альтернатива: SMS_PROVIDER=twilio / vonage и ключи провайдера. "
                    "Локально без SMS: OTP_DEV_MODE=true."
                ),
            )
        try:
            tr = settings.smsc_otp_translit
            await send_sms_text(
                to_phone=user.PhoneNumber,
                text=mes,
                translit=tr if tr in (1, 2) else None,
            )
        except SmscSendError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            ) from e
        return OtpSendResponse()

    if provider == "smsru":
        if not smsru_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "SMS.RU: задайте SMSRU_API_ID (ключ в кабинете https://sms.ru/). "
                    "Для России также доступен SMS_PROVIDER=smsc. Локально: OTP_DEV_MODE=true."
                ),
            )
        try:
            await send_sms_smsru(to_phone=user.PhoneNumber, text=mes)
        except SmsRuSendError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            ) from e
        return OtpSendResponse()

    if provider == "vonage":
        if not vonage_configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Vonage: задайте VONAGE_API_KEY, VONAGE_API_SECRET и VONAGE_FROM "
                    "(см. https://dashboard.nexmo.com/). "
                    "Или другой SMS_PROVIDER. Локально: OTP_DEV_MODE=true."
                ),
            )
        try:
            await send_sms_vonage(to_phone=user.PhoneNumber, text=mes)
        except VonageSendError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            ) from e
        return OtpSendResponse()

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"Неизвестный SMS_PROVIDER={provider!r}. Варианты: email, smsc, smsru, twilio, vonage.",
    )


# --- Вход по коду на почту / регистрация с подтверждением email -----------------


class EmailLoginSendRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: object) -> str:
        return _normalize_register_email(v)


@router.post("/email/login/send", response_model=OtpSendResponse)
async def email_login_send_code(
    payload: EmailLoginSendRequest,
    db: AsyncSession = Depends(get_db),
) -> OtpSendResponse:
    email = payload.email
    user = (
        await db.execute(
            select(User).where(
                User.Email.is_not(None),
                func.lower(User.Email) == email,
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Аккаунт с таким email не найден. "
                "Войдите по телефону и паролю или зарегистрируйтесь."
            ),
        )

    try:
        code = await issue_email_login_code(email, user.id)
    except OtpCooldownError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Подождите перед повторной отправкой кода.",
        ) from None

    if settings.otp_dev_mode:
        _logger.warning("Email login OTP dev: email=%s code=%s user_id=%s", email, code, user.id)
        return OtpSendResponse(
            ok=True,
            dev_code=code if settings.otp_dev_return_code else None,
        )

    if not smtp_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "SMTP не настроен: задайте EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD. "
                "Или включите OTP_DEV_MODE=true для локальной отладки."
            ),
        )

    to_addr = (user.Email or "").strip()
    plain, html_body = build_otp_email_plain_and_html(code)
    try:
        await send_otp_email(
            to_addr=to_addr,
            subject="Код входа — МПК Документы",
            body_plain=plain,
            body_html=html_body,
        )
    except SmtpSendError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return OtpSendResponse(ok=True, dev_code=None)


class EmailLoginVerifyRequest(BaseModel):
    email: str
    code: str

    @field_validator("email")
    @classmethod
    def _norm_email_verify(cls, v: object) -> str:
        return _normalize_register_email(v)


@router.post("/email/login/verify", response_model=TokenResponse)
async def email_login_verify_code(
    payload: EmailLoginVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    email = payload.email
    user_id = await verify_email_login_consume(email, payload.code)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или просроченный код.",
        )
    await log_user_activity(db, user_id=int(user_id), action="LOGIN_EMAIL", detail=email)
    await db.commit()
    return TokenResponse(access_token=create_access_token(subject=str(user_id)))


@router.post("/email/register/start", response_model=OtpSendResponse)
async def email_register_start(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> OtpSendResponse:
    existing = (
        await db.execute(select(User.id).where(User.PhoneNumber == payload.phone_number))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот номер телефона уже зарегистрирован",
        )

    email_taken = (
        await db.execute(
            select(User.id).where(
                User.Email.is_not(None),
                func.lower(User.Email) == payload.email,
            )
        )
    ).scalar_one_or_none()
    if email_taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    try:
        code = await issue_register_email_code(
            payload.email,
            phone=payload.phone_number.strip(),
            full_name=payload.full_name.strip(),
            password_hash=hash_password(payload.password),
        )
    except OtpCooldownError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Подождите перед повторной отправкой кода.",
        ) from None

    if settings.otp_dev_mode:
        _logger.warning("Email register OTP dev: email=%s code=%s", payload.email, code)
        return OtpSendResponse(
            ok=True,
            dev_code=code if settings.otp_dev_return_code else None,
        )

    if not smtp_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "SMTP не настроен: задайте EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD "
                "или OTP_DEV_MODE=true."
            ),
        )

    plain, html_body = build_registration_otp_email_plain_and_html(code)
    try:
        await send_otp_email(
            to_addr=payload.email,
            subject="Подтверждение регистрации — МПК Документы",
            body_plain=plain,
            body_html=html_body,
        )
    except SmtpSendError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return OtpSendResponse(ok=True, dev_code=None)


class RegisterEmailVerifyRequest(BaseModel):
    email: str
    code: str

    @field_validator("email")
    @classmethod
    def _norm_reg_verify(cls, v: object) -> str:
        return _normalize_register_email(v)


@router.post("/email/register/verify", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def email_register_verify(
    payload: RegisterEmailVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    email = payload.email
    pending = await verify_register_email_consume(email, payload.code)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или просроченный код.",
        )
    phone_number, full_name, password_hash = pending

    existing_phone = (
        await db.execute(select(User.id).where(User.PhoneNumber == phone_number))
    ).scalar_one_or_none()
    if existing_phone is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот номер телефона уже зарегистрирован",
        )
    email_taken = (
        await db.execute(
            select(User.id).where(
                User.Email.is_not(None),
                func.lower(User.Email) == email,
            )
        )
    ).scalar_one_or_none()
    if email_taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        PhoneNumber=phone_number,
        Email=email,
        FullName=full_name,
        Status=True,
        PasswordHash=password_hash,
        IsAdmin=False,
    )
    db.add(user)
    await db.flush()

    pub_b64, priv_enc_b64 = generate_keypair()
    db.add(
        SignatureProfile(
            UserId=user.id,
            PublicKey=pub_b64,
            EncryptedPrivateKey=priv_enc_b64,
            isRevoked=False,
        )
    )

    await log_user_activity(db, user_id=int(user.id), action="REGISTER_EMAIL", detail=email)
    await db.commit()
    await db.refresh(user)

    return RegisterResponse(access_token=create_access_token(subject=str(user.id)))

