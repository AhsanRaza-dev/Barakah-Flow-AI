"""
encryption.py — AES-256-GCM envelope for Tawbah OS private fields.

Tawbah OS entries (muhasaba answers, sadaqah niyyah, sin logs, etc.) are stored
encrypted at rest. The master key is read from TAWBAH_MASTER_KEY env var.

Per-user data key is derived via HKDF(master_key, user_id) so the master key
never touches rows directly.
"""
import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

_MASTER_KEY_B64 = os.getenv("TAWBAH_MASTER_KEY")


def _master_key() -> bytes:
    if not _MASTER_KEY_B64:
        raise RuntimeError(
            "TAWBAH_MASTER_KEY env var not set. "
            "Generate one with: python -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\""
        )
    return base64.b64decode(_MASTER_KEY_B64)


def _derive_user_key(user_id: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"tawbah_os_v1",
        info=user_id.encode("utf-8"),
    )
    return hkdf.derive(_master_key())


def encrypt(plaintext: str, user_id: str) -> str:
    if plaintext is None:
        return None
    key = _derive_user_key(user_id)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(ciphertext: str, user_id: str) -> str:
    if ciphertext is None:
        return None
    key = _derive_user_key(user_id)
    blob = base64.b64decode(ciphertext)
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
