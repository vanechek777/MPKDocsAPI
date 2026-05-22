from typing import Self

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "MPKDocumentsAPI"
    environment: str = "dev"

    # Defaults allow local import/dev without .env; override in production.
    jwt_secret: str = "dev-secret-change-me"
    jwt_issuer: str = "mpk-documents"
    jwt_audience: str = "mpk-documents-clients"
    jwt_expires_minutes: int = 60 * 24 * 7

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = "admin"
    db_name: str = "MPKDocuments"

    # email = код на почту (SMTP Яндекса и др.). smsc/smsru — SMS по РФ.
    sms_provider: str = "smsc"

    # Поддерживаются имена из .env как у Django (EMAIL_*) и прямые SMTP_*.
    # Яндекс: 465+SSL или 587+STARTTLS — задайте EMAIL_PORT / EMAIL_USE_TLS / EMAIL_USE_SSL.
    smtp_host: str = Field(
        default="smtp.yandex.ru",
        validation_alias=AliasChoices("EMAIL_HOST", "SMTP_HOST"),
    )
    smtp_port: int = Field(
        default=465,
        validation_alias=AliasChoices("EMAIL_PORT", "SMTP_PORT"),
    )
    # Django-флаги (если заданы, переопределяют режим шифрования для порта 587 и т.п.)
    email_use_tls: bool = Field(default=False, validation_alias=AliasChoices("EMAIL_USE_TLS"))
    email_use_ssl: bool = Field(default=False, validation_alias=AliasChoices("EMAIL_USE_SSL"))
    smtp_use_ssl: bool = Field(default=True, validation_alias=AliasChoices("SMTP_USE_SSL"))

    smtp_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EMAIL_HOST_USER", "SMTP_USER"),
    )
    smtp_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EMAIL_HOST_PASSWORD", "SMTP_PASSWORD"),
    )
    smtp_from: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEFAULT_FROM_EMAIL", "SMTP_FROM"),
    )
    otp_email_subject: str = Field(
        default="Код подписания — МПК Документы",
        validation_alias=AliasChoices("OTP_EMAIL_SUBJECT"),
    )

    @field_validator(
        "smtp_host",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "otp_email_subject",
        mode="before",
    )
    @classmethod
    def _strip_quotes(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        s = v.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            return s[1:-1].strip()
        return s

    @model_validator(mode="after")
    def _resolve_smtp_encryption(self) -> Self:
        # EMAIL_USE_TLS=true, EMAIL_USE_SSL=false → STARTTLS (как Django на порту 587).
        if self.email_use_tls and not self.email_use_ssl:
            object.__setattr__(self, "smtp_use_ssl", False)
        elif self.email_use_ssl:
            object.__setattr__(self, "smtp_use_ssl", True)
        return self

    # SMSC.ru (OTP для подписания): https://smsc.ru/api/ — либо API-ключ, либо login + psw.
    smsc_login: str | None = None
    smsc_password: str | None = None
    smsc_api_key: str | None = None
    # Имя отправителя — только согласованное в кабинете SMSC (иначе ошибка 6). Пусто = по умолчанию.
    smsc_sender: str | None = None

    # Текст SMS; только {code} как подстановка. Латиница по умолчанию — у SMSC реже ошибка 6, чем с кириллицей.
    smsc_otp_message_template: str = "Code: {code}"

    # 1 — просить SMSC отсылать текст транслитом (если вернули ошибку на кириллический шаблон).
    smsc_otp_translit: int = 0

    # Twilio (https://www.twilio.com/docs/sms) — SMS_PROVIDER=twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # Номер в E.164, выданный Twilio (trial: только на заранее верифицированные номера).
    twilio_from_number: str | None = None

    # Vonage / Nexmo SMS — SMS_PROVIDER=vonage https://dashboard.nexmo.com/
    vonage_api_key: str | None = None
    vonage_api_secret: str | None = None
    vonage_from: str | None = None

    # SMS.RU — российский шлюз, SMS_PROVIDER=smsru https://sms.ru/
    smsru_api_id: str | None = None
    smsru_from: str | None = None

    # Без платного SMS (локальная разработка): см. README. В продакшене держите false.
    otp_dev_mode: bool = False
    otp_dev_return_code: bool = False

    # Уведомления о документах по SMTP (тот же канал, что OTP на email). Без SMTP — тихий no-op.
    document_notify_email_enabled: bool = True

    # Доп. администраторы по id (через запятую), помимо Users.IsAdmin. Пример: "1,5"
    mpk_admin_user_ids: str = Field(default="", validation_alias=AliasChoices("MPK_ADMIN_USER_IDS"))

    @field_validator("mpk_admin_user_ids", mode="before")
    @classmethod
    def _strip_admin_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @property
    def database_url(self) -> str:
        # asyncmy driver
        return (
            f"mysql+asyncmy://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            "?charset=utf8mb4"
        )


settings = Settings()

