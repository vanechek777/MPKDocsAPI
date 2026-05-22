"""Проверка внешнего файла НЭП (.esig)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.esig import build_esig_payload, esig_to_bytes, parse_esig_bytes, ParsedEsig
from app.core.nep import document_hash_hex, load_public_key, verify_hash_hex
from app.db.models import DigitalSignature, Document, DocumentContent, DocumentTemplate, SignatureProfile, User
from app.db.session import get_db

router = APIRouter(prefix="/signatures", tags=["signatures"])


class VerifyQrPayload(BaseModel):
    """JSON из QR в свидетельстве НЭП (см. NepQrCode.BuildMobileVerificationPayload)."""

    mpk: str = "nep"
    document_id: int | None = None
    document_hash_hex: str


class VerifyEsigResponse(BaseModel):
    ok: bool
    crypto_valid: bool
    matches_current_document: bool
    document_id: int | None = None
    document_exists: bool = False
    document_title: str | None = None
    template_name: str | None = None
    signer_user_id: int | None = None
    signer_name: str | None = None
    signed_at_utc: str | None = None
    document_hash_hex: str | None = None
    signature_hex: str | None = None
    current_document_hash_hex: str | None = None
    detail: str | None = None


async def verify_parsed_esig(db: AsyncSession, parsed: ParsedEsig) -> VerifyEsigResponse:
    doc = (await db.execute(select(Document).where(Document.id == parsed.document_id))).scalar_one_or_none()
    if doc is None:
        return VerifyEsigResponse(
            ok=False,
            crypto_valid=False,
            matches_current_document=False,
            document_id=parsed.document_id,
            document_exists=False,
            document_title=parsed.document_title,
            signer_user_id=parsed.signer_user_id,
            signed_at_utc=parsed.signed_at_utc,
            document_hash_hex=parsed.document_hash_hex,
            signature_hex=parsed.signature_hex,
            detail="Документ с таким id не найден в системе",
        )

    content = (
        await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == doc.id))
    ).scalar_one_or_none()
    current_hash = document_hash_hex(
        document_id=doc.id,
        template_id=doc.TemplateId,
        content=content if isinstance(content, dict) else {},
    )
    matches = parsed.document_hash_hex == current_hash.lower()

    profile = (
        await db.execute(
            select(SignatureProfile).where(
                and_(SignatureProfile.UserId == parsed.signer_user_id, SignatureProfile.isRevoked.is_(False))
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        return VerifyEsigResponse(
            ok=False,
            crypto_valid=False,
            matches_current_document=matches,
            document_id=doc.id,
            document_exists=True,
            document_title=parsed.document_title,
            signer_user_id=parsed.signer_user_id,
            signed_at_utc=parsed.signed_at_utc,
            document_hash_hex=parsed.document_hash_hex,
            signature_hex=parsed.signature_hex,
            current_document_hash_hex=current_hash,
            detail="Профиль НЭП подписанта не найден или отозван",
        )

    try:
        pub = load_public_key(profile.PublicKey)
        crypto_ok = verify_hash_hex(
            public_key=pub,
            doc_hash_hex=parsed.document_hash_hex,
            signature_hex=parsed.signature_hex,
        )
    except Exception:
        crypto_ok = False

    signer_row = (await db.execute(select(User.FullName).where(User.id == parsed.signer_user_id))).scalar_one_or_none()
    signer_name = str(signer_row).strip() if signer_row else None

    tn = (await db.execute(select(DocumentTemplate.Name).where(DocumentTemplate.id == doc.TemplateId))).scalar_one_or_none()
    template_name = str(tn) if tn else None

    title = parsed.document_title
    if isinstance(content, dict):
        fn = content.get("fileName")
        if isinstance(fn, str) and fn.strip():
            title = fn.strip()

    detail_msg: str | None = None
    if not crypto_ok:
        detail_msg = "Подпись не проходит проверку (неверна или повреждена)"
    elif not matches:
        detail_msg = "Подпись криптографически верна, но содержимое документа в системе изменилось после подписи"

    return VerifyEsigResponse(
        ok=crypto_ok and matches,
        crypto_valid=crypto_ok,
        matches_current_document=matches,
        document_id=doc.id,
        document_exists=True,
        document_title=title,
        template_name=template_name,
        signer_user_id=parsed.signer_user_id,
        signer_name=signer_name,
        signed_at_utc=parsed.signed_at_utc,
        document_hash_hex=parsed.document_hash_hex,
        signature_hex=parsed.signature_hex,
        current_document_hash_hex=current_hash,
        detail=detail_msg,
    )


async def _find_digital_signature_for_qr(
    db: AsyncSession,
    *,
    document_id: int | None,
    want_hash: str,
) -> tuple[DigitalSignature | None, Document | None, dict]:
    want = want_hash.strip().lower()
    if not want:
        return None, None, {}

    if document_id is not None:
        doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
        if doc is None:
            return None, None, {}
        content = (
            await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == doc.id))
        ).scalar_one_or_none()
        content_dict = content if isinstance(content, dict) else {}
        cur = document_hash_hex(
            document_id=doc.id,
            template_id=doc.TemplateId,
            content=content_dict,
        )
        signatures = (
            await db.scalars(select(DigitalSignature).where(DigitalSignature.DocumentId == document_id))
        ).all()
        for ds in signatures:
            eff = (ds.DocumentHashHex or "").strip().lower()
            if not eff:
                eff = cur.lower()
            if eff == want:
                return ds, doc, content_dict
        return None, doc, content_dict

    q = select(DigitalSignature).where(func.lower(DigitalSignature.DocumentHashHex) == want)
    ds = (await db.execute(q.limit(1))).scalar_one_or_none()
    if ds is None:
        return None, None, {}
    doc = (await db.execute(select(Document).where(Document.id == ds.DocumentId))).scalar_one_or_none()
    if doc is None:
        return None, None, {}
    content = (
        await db.execute(select(DocumentContent.DataJson).where(DocumentContent.DocumentId == doc.id))
    ).scalar_one_or_none()
    content_dict = content if isinstance(content, dict) else {}
    return ds, doc, content_dict


@router.post("/verify-esig", response_model=VerifyEsigResponse)
async def verify_esig_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ = user
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустой файл")
    try:
        parsed = parse_esig_bytes(raw)
    except ValueError as e:
        return VerifyEsigResponse(
            ok=False,
            crypto_valid=False,
            matches_current_document=False,
            detail=str(e),
        )

    return await verify_parsed_esig(db, parsed)


@router.post("/verify-qr-payload", response_model=VerifyEsigResponse)
async def verify_qr_payload(
    payload: VerifyQrPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _ = user
    if (payload.mpk or "").strip().lower() != "nep":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неизвестный тип QR")

    if payload.document_id is not None:
        doc_exists = (
            await db.execute(select(Document.id).where(Document.id == payload.document_id))
        ).scalar_one_or_none()
        if doc_exists is None:
            return VerifyEsigResponse(
                ok=False,
                crypto_valid=False,
                matches_current_document=False,
                document_id=payload.document_id,
                document_exists=False,
                detail="Документ с таким id не найден в системе",
            )

    ds, doc, content_dict = await _find_digital_signature_for_qr(
        db,
        document_id=payload.document_id,
        want_hash=payload.document_hash_hex,
    )
    if ds is None or doc is None:
        msg = (
            "Не удалось найти НЭП-подпись по данным QR для этого документа."
            if payload.document_id is not None
            else "Не удалось найти подпись по ключу. Убедитесь, что QR из свидетельства МПК, или загрузите файл .esig."
        )
        return VerifyEsigResponse(
            ok=False,
            crypto_valid=False,
            matches_current_document=False,
            document_id=payload.document_id,
            document_hash_hex=payload.document_hash_hex.strip().lower() if payload.document_hash_hex else None,
            detail=msg,
        )

    eff_h = (ds.DocumentHashHex or "").strip().lower() or document_hash_hex(
        document_id=doc.id,
        template_id=doc.TemplateId,
        content=content_dict,
    )
    title = None
    if isinstance(content_dict, dict):
        fn = content_dict.get("fileName")
        if isinstance(fn, str) and fn.strip():
            title = fn.strip()
    raw = esig_to_bytes(
        build_esig_payload(
            document_id=doc.id,
            template_id=doc.TemplateId,
            document_hash_hex=eff_h,
            signer_user_id=int(ds.UserId),
            signature_hex=ds.SignatureHex,
            signed_at=ds.SignedAt,
            document_title=title,
        )
    )
    try:
        parsed = parse_esig_bytes(raw)
    except ValueError as e:
        return VerifyEsigResponse(
            ok=False,
            crypto_valid=False,
            matches_current_document=False,
            detail=str(e),
        )

    return await verify_parsed_esig(db, parsed)
