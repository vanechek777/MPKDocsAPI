from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Department(Base):
    __tablename__ = "Departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    OneCId: Mapped[str | None] = mapped_column(String(50), nullable=True)
    Name: Mapped[str] = mapped_column(String(255), nullable=False)
    ParentDepartmentId: Mapped[int | None] = mapped_column(ForeignKey("Departments.id"), nullable=True)
    isActive: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)

    parent: Mapped[Department | None] = relationship(remote_side=[id])


class Position(Base):
    __tablename__ = "Positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    OneCId: Mapped[str | None] = mapped_column(String(50), nullable=True)
    Name: Mapped[str] = mapped_column(String(255), nullable=False)
    isActive: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)


class User(Base):
    __tablename__ = "Users"
    __table_args__ = (UniqueConstraint("PhoneNumber", name="UQ_Users_PhoneNumber"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    OneCId: Mapped[str | None] = mapped_column(String(50), nullable=True)
    PhoneNumber: Mapped[str] = mapped_column(String(20), nullable=False)
    Email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    FullName: Mapped[str] = mapped_column(String(255), nullable=False)
    PositionId: Mapped[int | None] = mapped_column(ForeignKey("Positions.id"), nullable=True)
    DepartmentId: Mapped[int | None] = mapped_column(ForeignKey("Departments.id"), nullable=True)
    Status: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    PasswordHash: Mapped[str] = mapped_column(Text, nullable=False)
    IsAdmin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    position: Mapped[Position | None] = relationship()
    department: Mapped[Department | None] = relationship()


class DocumentCategory(Base):
    __tablename__ = "DocumentCategories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Name: Mapped[str] = mapped_column(String(255), nullable=False)
    SortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)


class DocumentTemplate(Base):
    __tablename__ = "DocumentTemplates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Name: Mapped[str] = mapped_column(String(255), nullable=False)
    TemplatePath: Mapped[str] = mapped_column(Text, nullable=False)
    FormSchema: Mapped[dict] = mapped_column(JSON, nullable=False)
    isActive: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    CategoryId: Mapped[int | None] = mapped_column(ForeignKey("DocumentCategories.id"), nullable=True)

    category: Mapped[DocumentCategory | None] = relationship()


class UserActivityLog(Base):
    __tablename__ = "UserActivityLogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    UserId: Mapped[int | None] = mapped_column(ForeignKey("Users.id"), nullable=True)
    Action: Mapped[str] = mapped_column(String(80), nullable=False)
    Detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    CreatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True, default=datetime.utcnow)

    user: Mapped[User | None] = relationship(foreign_keys=[UserId])


class Document(Base):
    __tablename__ = "Documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    TemplateId: Mapped[int] = mapped_column(ForeignKey("DocumentTemplates.id"), nullable=False)
    InitiatorId: Mapped[int] = mapped_column(ForeignKey("Users.id"), nullable=False)
    Status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="DRAFT")
    FinalDocumentHash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    FinalPDFPath: Mapped[str | None] = mapped_column(Text, nullable=True)
    CreatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True, default=datetime.utcnow)

    template: Mapped[DocumentTemplate] = relationship()
    initiator: Mapped[User] = relationship()


class DocumentContent(Base):
    __tablename__ = "DocumentContents"
    __table_args__ = (UniqueConstraint("DocumentId", name="UQ_DocumentContents_DocumentId"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    DocumentId: Mapped[int] = mapped_column(ForeignKey("Documents.id"), nullable=False)
    DataJson: Mapped[dict] = mapped_column(JSON, nullable=False)

    document: Mapped[Document] = relationship()


class DocumentStep(Base):
    __tablename__ = "DocumentSteps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    DocumentId: Mapped[int] = mapped_column(ForeignKey("Documents.id"), nullable=False)
    StepOrder: Mapped[int] = mapped_column(Integer, nullable=False)
    ApprovalMode: Mapped[str] = mapped_column(String(50), nullable=False)
    Status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="PENDING")

    document: Mapped[Document] = relationship()


class DocumentTask(Base):
    __tablename__ = "DocumentTasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    StepId: Mapped[int] = mapped_column(ForeignKey("DocumentSteps.id"), nullable=False)
    DocumentId: Mapped[int] = mapped_column(ForeignKey("Documents.id"), nullable=False)
    AssignedUserId: Mapped[int | None] = mapped_column(ForeignKey("Users.id"), nullable=True)
    AssignedPositionId: Mapped[int | None] = mapped_column(ForeignKey("Positions.id"), nullable=True)
    AssignedDepartmentId: Mapped[int | None] = mapped_column(ForeignKey("Departments.id"), nullable=True)
    Status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="PENDING")
    ProcessedByUserId: Mapped[int | None] = mapped_column(ForeignKey("Users.id"), nullable=True)
    ProcessedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    step: Mapped[DocumentStep] = relationship()
    document: Mapped[Document] = relationship(foreign_keys=[DocumentId])
    assigned_user: Mapped[User | None] = relationship(foreign_keys=[AssignedUserId])
    processed_by: Mapped[User | None] = relationship(foreign_keys=[ProcessedByUserId])


class SignatureProfile(Base):
    __tablename__ = "SignatureProfiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    UserId: Mapped[int] = mapped_column(ForeignKey("Users.id"), nullable=False)
    PublicKey: Mapped[str] = mapped_column(Text, nullable=False)
    EncryptedPrivateKey: Mapped[str] = mapped_column(Text, nullable=False)
    CreatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True, default=datetime.utcnow)
    isRevoked: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    user: Mapped[User] = relationship()


class DigitalSignature(Base):
    __tablename__ = "DigitalSignatures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    DocumentId: Mapped[int] = mapped_column(ForeignKey("Documents.id"), nullable=False)
    UserId: Mapped[int] = mapped_column(ForeignKey("Users.id"), nullable=False)
    SignatureHex: Mapped[str] = mapped_column(Text, nullable=False)
    DocumentHashHex: Mapped[str | None] = mapped_column(String(128), nullable=True)
    SignedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True, default=datetime.utcnow)

    document: Mapped[Document] = relationship()
    user: Mapped[User] = relationship()


class DocumentUserView(Base):
    """Первый просмотр карточки документа текущим пользователем (лист «Недавние» / серость точки).

    Дальше по этой таблице можно строить отчёт «кто из получателей открыл» — по UserId + DocumentId."""

    __tablename__ = "DocumentUserViews"
    __table_args__ = (UniqueConstraint("DocumentId", "UserId", name="UQ_DocumentUserViews_Doc_User"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    DocumentId: Mapped[int] = mapped_column(ForeignKey("Documents.id"), nullable=False)
    UserId: Mapped[int] = mapped_column(ForeignKey("Users.id"), nullable=False)
    FirstViewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True, default=datetime.utcnow)

    document: Mapped[Document] = relationship(foreign_keys=[DocumentId])
    user: Mapped[User] = relationship(foreign_keys=[UserId])


class WorkflowTemplate(Base):
    __tablename__ = "WorkflowTemplates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    TemplateId: Mapped[int] = mapped_column(ForeignKey("DocumentTemplates.id"), nullable=False)
    StepOrder: Mapped[int] = mapped_column(Integer, nullable=False)
    TargetUserId: Mapped[int | None] = mapped_column(ForeignKey("Users.id"), nullable=True)
    TargetPositionId: Mapped[int | None] = mapped_column(ForeignKey("Positions.id"), nullable=True)
    TargetDepartmentId: Mapped[int | None] = mapped_column(ForeignKey("Departments.id"), nullable=True)
    ApprovalMode: Mapped[str | None] = mapped_column(String(50), nullable=True, default="ANY")
    ActionType: Mapped[str | None] = mapped_column(String(50), nullable=True, default="SIGN")

