"""Commitments and sealed delivery.

commitment = sha256(payload + ":" + salt), lower-case hex. Same rule as the contract.
Delivery = NaCl SealedBox (X25519 + XSalsa20-Poly1305) to the buyer's registered key.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from nacl.public import PrivateKey, PublicKey, SealedBox


def canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def new_salt() -> str:
    return secrets.token_hex(16)


def commitment_of(payload: str, salt: str) -> str:
    return hashlib.sha256(f"{payload}:{salt}".encode("utf-8")).hexdigest()


def load_or_create_key(path: Path) -> PrivateKey:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return PrivateKey(base64.b64decode(path.read_text().strip()))
    key = PrivateKey.generate()
    path.write_text(base64.b64encode(bytes(key)).decode())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def public_b64(key: PrivateKey) -> str:
    return base64.b64encode(bytes(key.public_key)).decode()


def seal(payload: str, salt: str, buyer_public_b64: str) -> str:
    box = SealedBox(PublicKey(base64.b64decode(buyer_public_b64)))
    message = json.dumps({"payload": payload, "salt": salt}, separators=(",", ":")).encode()
    return base64.b64encode(box.encrypt(message)).decode()


def open_sealed(ciphertext_b64: str, key: PrivateKey) -> tuple[str, str]:
    data = json.loads(SealedBox(key).decrypt(base64.b64decode(ciphertext_b64)))
    return data["payload"], data["salt"]
