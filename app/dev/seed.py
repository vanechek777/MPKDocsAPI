from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import (
    Department,
    Document,
    DocumentCategory,
    DocumentContent,
    DocumentStep,
    DocumentTask,
    DocumentTemplate,
    Position,
    User,
)
from app.db.session import SessionLocal, engine


def _utc_now_naive() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


async def seed(force: bool = False) -> None:
    """
    Idempotent-ish seed:
    - If Users table already has data, skip unless force=True.
    """
    try:
        async with SessionLocal() as db:
            if force:
                # Keep it safe: do not delete existing data automatically.
                print("Force mode requested, but destructive reset is not implemented. Skipping.")
                return

            existing_users = (await db.execute(select(User.id).limit(1))).first()

            # ---- Reference data ----
            dept_acc = Department(Name="Бухгалтерия", OneCId=None, isActive=True)
            dept_sausage = Department(Name="Колбасный цех", OneCId=None, isActive=True)
            dept_warehouse = Department(Name="Склад", OneCId=None, isActive=True)
            dept_director = Department(Name="Директор", OneCId=None, isActive=True)

            pos_accountant = Position(Name="Старший бухгалтер", OneCId=None, isActive=True)
            pos_worker = Position(Name="Начальник цеха", OneCId=None, isActive=True)
            pos_manager = Position(Name="Заведующий складом", OneCId=None, isActive=True)
            pos_director = Position(Name="Директор", OneCId=None, isActive=True)

            if not existing_users:
                db.add_all([dept_acc, dept_sausage, dept_warehouse, dept_director])
                db.add_all([pos_accountant, pos_worker, pos_manager, pos_director])
                await db.flush()

                # ---- Users ----
                # Password for all test users: password
                pwd = hash_password("password")
                u_initiator = User(
                    PhoneNumber="+79990000001",
                    FullName="Мирзоева Батуина Тагировна",
                    DepartmentId=dept_acc.id,
                    PositionId=pos_accountant.id,
                    Status=True,
                    PasswordHash=pwd,
                    IsAdmin=False,
                )
                u_sausage = User(
                    PhoneNumber="+79990000002",
                    FullName="Герасимов Антон Баирович",
                    DepartmentId=dept_sausage.id,
                    PositionId=pos_worker.id,
                    Status=True,
                    PasswordHash=pwd,
                    IsAdmin=False,
                )
                u_me = User(
                    PhoneNumber="+79148012594",
                    FullName="Голомидов Алексей Викторович",
                    DepartmentId=dept_warehouse.id,
                    PositionId=pos_manager.id,
                    Status=True,
                    PasswordHash=pwd,
                    IsAdmin=True,
                )
                u_director = User(
                    PhoneNumber="+79990000004",
                    FullName="Дьячков Евгений Юрьевич",
                    DepartmentId=dept_director.id,
                    PositionId=pos_director.id,
                    Status=True,
                    PasswordHash=pwd,
                    IsAdmin=False,
                )
                db.add_all([u_initiator, u_sausage, u_me, u_director])
                await db.flush()
            else:
                # DB already has users; do not add reference data to avoid duplicates.
                u_initiator = (await db.execute(select(User).order_by(User.id.asc()).limit(1))).scalar_one()
                u_me = (await db.execute(select(User).order_by(User.id.asc()).offset(2).limit(1))).scalar_one()
                u_sausage = (await db.execute(select(User).order_by(User.id.asc()).offset(1).limit(1))).scalar_one()
                u_director = (await db.execute(select(User).order_by(User.id.asc()).offset(3).limit(1))).scalar_one()

            golomidov = (
                await db.execute(
                    select(User).where(User.FullName == "Голомидов Алексей Викторович").limit(1)
                )
            ).scalar_one_or_none()
            if golomidov is not None:
                golomidov.PhoneNumber = "+79148012594"
                golomidov.IsAdmin = True

            by_phone_admin = (
                await db.execute(select(User).where(User.PhoneNumber == "+79148012594"))
            ).scalar_one_or_none()
            if by_phone_admin is not None:
                by_phone_admin.IsAdmin = True

            # ---- Категории шаблонов ----
            existing_cats = (await db.execute(select(DocumentCategory))).scalars().all()
            if not existing_cats:
                db.add_all(
                    [
                        DocumentCategory(Name="Отчёты", SortOrder=10, isActive=True),
                        DocumentCategory(Name="Приказы", SortOrder=20, isActive=True),
                        DocumentCategory(Name="Прочее", SortOrder=90, isActive=True),
                    ]
                )
                await db.flush()

            cat_reports = (
                await db.execute(select(DocumentCategory).where(DocumentCategory.Name == "Отчёты").limit(1))
            ).scalar_one_or_none()
            cat_orders = (
                await db.execute(select(DocumentCategory).where(DocumentCategory.Name == "Приказы").limit(1))
            ).scalar_one_or_none()

            # ---- Templates ----
            # UI expects "категории" отчётов отдельными карточками (как в макете),
            # поэтому создаём несколько активных шаблонов отчётов.
            existing_templates = (await db.execute(select(DocumentTemplate))).scalars().all()
            by_name = {t.Name: t for t in existing_templates}

            report_templates = [
                ("Отчет по продажам", "/templates/report_sales.docx"),
                ("Отчет по остаткам на складе колбасной продукции", "/templates/report_stock.docx"),
                ("Отчет по остаткам на складе гофрокартонной продукции", "/templates/report_packaging.docx"),
                ("Отчет финансовый за квартал", "/templates/report_fin_quarter.docx"),
            ]

            for name, path in report_templates:
                if name in by_name:
                    continue
                db.add(
                    DocumentTemplate(
                        Name=name,
                        TemplatePath=path,
                        FormSchema={"fields": [], "category": "Отчёты"},
                        isActive=True,
                        CategoryId=cat_reports.id if cat_reports else None,
                    )
                )

            if "Приказ" not in by_name:
                db.add(
                    DocumentTemplate(
                        Name="Приказ",
                        TemplatePath="/templates/order.docx",
                        FormSchema={
                            "fields": [{"name": "title", "type": "string", "label": "Название"}],
                            "category": "Приказы",
                        },
                        isActive=True,
                        CategoryId=cat_orders.id if cat_orders else None,
                    )
                )

            # Старый общий "Отчет" (если был) выключаем, чтобы не путал UI.
            if "Отчет" in by_name:
                by_name["Отчет"].isActive = False

            if cat_reports and cat_orders:
                for tpl in (await db.execute(select(DocumentTemplate))).scalars().all():
                    if tpl.CategoryId is not None:
                        continue
                    low = (tpl.Name or "").lower()
                    if "отчет" in low or "отчёт" in low:
                        tpl.CategoryId = cat_reports.id
                    elif "приказ" in low:
                        tpl.CategoryId = cat_orders.id

            await db.flush()

            # Pick templates for example documents
            tmpl_report = (
                await db.execute(
                    select(DocumentTemplate)
                    .where(DocumentTemplate.Name == "Отчет по продажам")
                    .limit(1)
                )
            ).scalar_one_or_none()
            if tmpl_report is None:
                tmpl_report = (
                    await db.execute(
                        select(DocumentTemplate)
                        .where(DocumentTemplate.isActive.is_(True))
                        .order_by(DocumentTemplate.id.asc())
                        .limit(1)
                    )
                ).scalar_one()

            tmpl_order = (
                await db.execute(
                    select(DocumentTemplate).where(DocumentTemplate.Name == "Приказ").limit(1)
                )
            ).scalar_one()

            now = _utc_now_naive()

            # Helper: build workflow tasks for a doc
            async def create_doc(
                *,
                template: DocumentTemplate,
                initiator: User,
                created_at: datetime,
                status: str,
                steps: list[tuple[int, str]],
                tasks: list[dict],
                content: dict,
            ) -> Document:
                doc = Document(
                    TemplateId=template.id,
                    InitiatorId=initiator.id,
                    Status=status,
                    CreatedAt=created_at,
                )
                db.add(doc)
                await db.flush()

                db.add(DocumentContent(DocumentId=doc.id, DataJson=content))

                step_rows: dict[int, DocumentStep] = {}
                for step_order, approval_mode in steps:
                    st = DocumentStep(
                        DocumentId=doc.id,
                        StepOrder=step_order,
                        ApprovalMode=approval_mode,
                        Status="PENDING",
                    )
                    db.add(st)
                    await db.flush()
                    step_rows[step_order] = st

                for t in tasks:
                    step = step_rows[t["step_order"]]
                    dt = DocumentTask(
                        StepId=step.id,
                        DocumentId=doc.id,
                        AssignedUserId=t.get("assigned_user_id"),
                        AssignedDepartmentId=t.get("assigned_department_id"),
                        AssignedPositionId=t.get("assigned_position_id"),
                        Status=t.get("status", "PENDING"),
                        ProcessedByUserId=t.get("processed_by_user_id"),
                        ProcessedAt=t.get("processed_at"),
                    )
                    db.add(dt)

                return doc

            # ---- Documents ----
            # 1) Pending doc for signing by "me" (warehouse)
            await create_doc(
                template=tmpl_report,
                initiator=u_initiator,
                created_at=now - timedelta(days=1),
                status="IN_PROGRESS",
                steps=[(1, "ANY"), (2, "ANY"), (3, "ANY"), (4, "ANY")],
                tasks=[
                    {
                        "step_order": 1,
                        "assigned_user_id": u_initiator.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_initiator.id,
                        "processed_at": now - timedelta(days=1, hours=2),
                    },
                    {
                        "step_order": 2,
                        "assigned_user_id": u_sausage.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_sausage.id,
                        "processed_at": now - timedelta(days=1, hours=1),
                    },
                    {
                        "step_order": 3,
                        "assigned_user_id": u_me.id,
                        "status": "PENDING",
                    },
                    {
                        "step_order": 4,
                        "assigned_user_id": u_director.id,
                        "status": "PENDING",
                    },
                ],
                content={"fileName": "Отчет_по_продажам.pdf", "note": "Тестовый документ"},
            )

            # 2) Fully signed doc
            await create_doc(
                template=tmpl_order,
                initiator=u_initiator,
                created_at=now - timedelta(days=3),
                status="SIGNED",
                steps=[(1, "ANY"), (2, "ANY"), (3, "ANY"), (4, "ANY")],
                tasks=[
                    {
                        "step_order": 1,
                        "assigned_user_id": u_initiator.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_initiator.id,
                        "processed_at": now - timedelta(days=3, hours=3),
                    },
                    {
                        "step_order": 2,
                        "assigned_user_id": u_sausage.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_sausage.id,
                        "processed_at": now - timedelta(days=3, hours=2),
                    },
                    {
                        "step_order": 3,
                        "assigned_user_id": u_me.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_me.id,
                        "processed_at": now - timedelta(days=3, hours=1),
                    },
                    {
                        "step_order": 4,
                        "assigned_user_id": u_director.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_director.id,
                        "processed_at": now - timedelta(days=3, minutes=30),
                    },
                ],
                content={"fileName": "Приказ.pdf", "note": "Полностью подписан"},
            )

            # 3) Rejected by "me"
            await create_doc(
                template=tmpl_report,
                initiator=u_initiator,
                created_at=now - timedelta(days=5),
                status="REJECTED",
                steps=[(1, "ANY"), (2, "ANY"), (3, "ANY"), (4, "ANY")],
                tasks=[
                    {
                        "step_order": 1,
                        "assigned_user_id": u_initiator.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_initiator.id,
                        "processed_at": now - timedelta(days=5, hours=2),
                    },
                    {
                        "step_order": 2,
                        "assigned_user_id": u_sausage.id,
                        "status": "SIGNED",
                        "processed_by_user_id": u_sausage.id,
                        "processed_at": now - timedelta(days=5, hours=1),
                    },
                    {
                        "step_order": 3,
                        "assigned_user_id": u_me.id,
                        "status": "REJECTED",
                        "processed_by_user_id": u_me.id,
                        "processed_at": now - timedelta(days=5, minutes=10),
                    },
                    {
                        "step_order": 4,
                        "assigned_user_id": u_director.id,
                        "status": "PENDING",
                    },
                ],
                content={"fileName": "Договор_поставки.pdf", "note": "Отклонён мной"},
            )

            await db.commit()
            print("Seed complete.")
            print("Test login users:")
            print("  +79148012594 / password   (это 'я' — склад)")
    finally:
        # Ensure all DB connections are closed before the event loop ends (Windows Proactor).
        await engine.dispose()

