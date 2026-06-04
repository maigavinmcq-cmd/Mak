# -*- coding: utf-8 -*-
from __future__ import annotations
import base64, json, os
from pathlib import Path

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet

from .settings import CONFIG_FILE, SALT_FILE

def get_or_create_salt() -> bytes:
    salt_path = Path(SALT_FILE)
    if salt_path.exists():
        return salt_path.read_bytes()
    salt = os.urandom(16)
    salt_path.write_bytes(salt)
    return salt

def derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=200_000,
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(key)

def encrypt_config(passphrase: str, config_obj: dict) -> bytes:
    salt = get_or_create_salt()
    key = derive_fernet_key(passphrase, salt)
    f = Fernet(key)
    plaintext = json.dumps(config_obj, ensure_ascii=False).encode("utf-8")
    return f.encrypt(plaintext)

def decrypt_config(passphrase: str, token: bytes) -> dict:
    salt = get_or_create_salt()
    key = derive_fernet_key(passphrase, salt)
    f = Fernet(key)
    plaintext = f.decrypt(token)
    return json.loads(plaintext.decode("utf-8"))

def load_encrypted_config(passphrase: str) -> dict | None:
    p = Path(CONFIG_FILE)
    if not p.exists():
        return None
    token = p.read_bytes()
    return decrypt_config(passphrase, token)

def save_encrypted_config(passphrase: str, config_obj: dict):
    token = encrypt_config(passphrase, config_obj)
    Path(CONFIG_FILE).write_bytes(token)
