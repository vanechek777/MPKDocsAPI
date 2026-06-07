-- Фиксация «документ открыт пользователем» (индикатор на списке: голубой = не смотрели, серый = смотрели).
-- Выполните один раз.

CREATE TABLE IF NOT EXISTS `DocumentUserViews` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `DocumentId` INT NOT NULL,
  `UserId` INT NOT NULL,
  `FirstViewedAt` DATETIME(6) NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `UQ_DocumentUserViews_Doc_User` (`DocumentId`, `UserId`),
  KEY `IX_DocumentUserViews_UserId` (`UserId`),
  CONSTRAINT `FK_DocumentUserViews_Document`
    FOREIGN KEY (`DocumentId`) REFERENCES `Documents` (`id`)
    ON DELETE CASCADE
    ON UPDATE CASCADE,
  CONSTRAINT `FK_DocumentUserViews_User`
    FOREIGN KEY (`UserId`) REFERENCES `Users` (`id`)
    ON DELETE CASCADE
    ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
