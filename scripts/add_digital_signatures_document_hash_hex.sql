-- Хеш документа на момент НЭП-подписи (для экспорта .esig и проверки).
ALTER TABLE `DigitalSignatures`
    ADD COLUMN `DocumentHashHex` VARCHAR(128) NULL AFTER `SignatureHex`;
