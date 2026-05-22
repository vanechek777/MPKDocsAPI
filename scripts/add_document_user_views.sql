-- Фиксация «документ открыт пользователем» (индикатор на списке: голубой = не смотрели, серый = смотрели).
-- Выполните один раз.

CREATE TABLE IF NOT EXISTS `DocumentUserViews` (
  `Id` INT NOT NULL AUTO_INCREMENT,
  `DocumentId` INT NOT NULL,
  `UserId` INT NOT NULL,
  `FirstViewedAt` DATETIME NULL DEFAULT NULL,
  PRIMARY KEY (`Id`),
  UNIQUE KEY `UQ_DocumentUserViews_Doc_User` (`DocumentId`, `UserId`),
  KEY `IX_DocumentUserViews_UserId` (`UserId`),
  CONSTRAINT `FK_DocumentUserViews_Document` FOREIGN KEY (`DocumentId`) REFERENCES `Documents` (`Id`) ON DELETE CASCADE,
  CONSTRAINT `FK_DocumentUserViews_User` FOREIGN KEY (`UserId`) REFERENCES `Users` (`Id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
