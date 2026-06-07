-- Кадровый справочник для проверки при регистрации (MySQL / MariaDB).
CREATE TABLE IF NOT EXISTS StaffDirectory (
    id INT AUTO_INCREMENT PRIMARY KEY,
    FullName VARCHAR(255) NOT NULL,
    PositionId INT NOT NULL,
    DepartmentId INT NOT NULL,
    OneCId VARCHAR(50) NULL,
    isActive TINYINT(1) NULL DEFAULT 1,
    CONSTRAINT FK_StaffDirectory_Position FOREIGN KEY (PositionId) REFERENCES Positions(id),
    CONSTRAINT FK_StaffDirectory_Department FOREIGN KEY (DepartmentId) REFERENCES Departments(id),
    INDEX IX_StaffDirectory_FullName (FullName),
    INDEX IX_StaffDirectory_Position (PositionId)
);
