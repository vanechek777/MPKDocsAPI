-- Выполните один раз, если колонки ещё нет (OTP и обязательный email при регистрации в приложении).
-- Новые пользователи всегда получают непустой Email; старые записи могут быть с NULL до PATCH профиля.
ALTER TABLE `Users` ADD COLUMN `Email` VARCHAR(255) NULL AFTER `PhoneNumber`;
