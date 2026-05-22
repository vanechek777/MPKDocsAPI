from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.config import settings


def _fernet() -> Fernet:
    # Derive a stable Fernet key from jwt_secret (dev-friendly).
    # In production this should be a dedicated encryption key rotated separately.
    raw = hashlib.sha256(settings.jwt_secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def canonical_document_bytes(*, document_id: int, template_id: int, content: dict[str, Any] | None) -> bytes:
    payload = {
        "document_id": int(document_id),
        "template_id": int(template_id),
        "content": content or {},
    }
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return s.encode("utf-8")


def document_hash_hex(*, document_id: int, template_id: int, content: dict[str, Any] | None) -> str:
    return hashlib.sha256(canonical_document_bytes(document_id=document_id, template_id=template_id, content=content)).hexdigest()


def generate_keypair() -> tuple[str, str]:
    """
    Returns (public_key_b64, private_key_encrypted_b64).
    """
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    pub_bytes = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )

    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")
    priv_enc = _fernet().encrypt(priv_bytes)
    priv_enc_b64 = base64.b64encode(priv_enc).decode("ascii")
    return pub_b64, priv_enc_b64


def load_private_key(encrypted_b64: str) -> Ed25519PrivateKey:
    enc = base64.b64decode(encrypted_b64.encode("ascii"))
    raw = _fernet().decrypt(enc)
    return Ed25519PrivateKey.from_private_bytes(raw)


def load_public_key(public_b64: str) -> Ed25519PublicKey:
    raw = base64.b64decode(public_b64.encode("ascii"))
    return Ed25519PublicKey.from_public_bytes(raw)


def sign_hash_hex(*, private_key: Ed25519PrivateKey, doc_hash_hex: str) -> str:
    sig = private_key.sign(bytes.fromhex(doc_hash_hex))
    return sig.hex()


def verify_hash_hex(*, public_key: Ed25519PublicKey, doc_hash_hex: str, signature_hex: str) -> bool:
    try:
        public_key.verify(bytes.fromhex(signature_hex), bytes.fromhex(doc_hash_hex))
        return True
    except Exception:
        return False

