## MPKDocumentsAPI (FastAPI)

### Setup

Create `.env` (see `.env.example`) and install deps:

```bash
python -m venv .venv
.venv\\Scripts\\pip install -r requirements1.txt
```

If you previously installed the unrelated `jose` package, remove it:

```bash
.venv\\Scripts\\python -m pip uninstall -y jose
```

### Run

```bash
.venv\\Scripts\\python -m uvicorn main:app --reload
```

### Seed test data

Creates departments/positions/users/templates/documents/tasks so the UI has data.

```bash
.venv\\Scripts\\python seed.py
```

Test user:

- phone: `+79148012594`
- password: `password`

После миграции `../Data/migrations/002_admin_panel_mysql.sql` у этого пользователя в БД выставляется **`Users.IsAdmin = 1`** (сидер делает то же для чистой БД). Дополнительные админы: `MPK_ADMIN_USER_IDS` (id через запятую) или флаг в БД. Админ-API: префикс **`/admin/*`** (JWT + права).

### SMS и OTP

**Россия (+7)** — используйте **только российские шлюзы** из этого API (они же реально доставляют в сети РФ):

| Провайдер | `SMS_PROVIDER` | Ключи в `.env` |
| --- | --- | --- |
| **[SMSC.ru](https://smsc.ru/api/)** | `smsc` | `SMSC_API_KEY` или `SMSC_LOGIN` + `SMSC_PASSWORD` |
| **[SMS.RU](https://sms.ru/)** | `smsru` | `SMSRU_API_ID` (см. личный кабинет); опционально `SMSRU_FROM` |

Текст кода: `SMSC_OTP_MESSAGE_TEMPLATE=Code: {code}` (общий шаблон для всех провайдеров). Для SMSC при ошибке «сообщение запрещено» уберите `SMSC_SENDER` или согласуйте имя в кабинете.

**Без SMS (разработка):** `OTP_DEV_MODE=true` — код в логе uvicorn; опционально `OTP_DEV_RETURN_CODE=true` — поле `dev_code`.

**Не РФ / тест за рубежом:** `SMS_PROVIDER=twilio` или `vonage` — см. `.env.example` (на **+7 обычно не подходят**).

Услуга SMS всегда платная у операторов; «бесплатно навсегда на любой номер» не бывает. Если `OTP_DEV_MODE=true`, внешний шлюз не вызывается.

- `POST /auth/otp/send` (Bearer JWT) — выдаёт 6-значный код (SMS или dev-режим). Клиент MAUI вызывает этот метод при нажатии «Подписать» / перед вводом кода.
- `POST /documents/{id}/actions/sign` с полем `otp_code` — код проверяется; при успехе сохраняется NEP-подпись документа.

### Endpoints

- `GET /health`
- `POST /auth/login` (phone_number + password) → JWT
- `POST /auth/otp/send` (Bearer) → SMS (`SMS_PROVIDER`: для РФ **smsc** или **smsru**)
- `GET /users/me` (Bearer)
- `GET /documents/recent` (Bearer)

### Продакшен (клиенты MAUI / Web)

Публичный API: **`https://mpk-docs.ru.tuna.am`** (без порта — обычно за reverse proxy: nginx/Caddy).

На сервере задайте в `.env` реальную БД, `JWT_SECRET`, SMS/SMTP и т.д. FastAPI можно поднять за прокси с TLS; CORS уже разрешён для любых источников (`*`).

Клиентское приложение берёт базовый URL из `MPKDocumentsMAUI.Shared.Api.ApiOptions.BaseUrl` (по умолчанию совпадает с адресом выше).

