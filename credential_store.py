# -*- coding: utf-8 -*-
"""139 云盘凭证的本机加密存储。"""
from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ENCRYPTED_FILE = DATA_DIR / "credentials.enc"
KEY_FILE = DATA_DIR / ".credential.key"
LEGACY_FILE = ROOT / "config.json"
AAD = b"zhicuo-ai-mistake-book:credentials:v1"
VERSION = 1


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _master_key() -> tuple[bytes, str]:
    """环境变量优先；否则生成仅当前系统账户可读的随机主密钥。"""
    env_key = os.environ.get("MISTAKE_BOOK_MASTER_KEY", "").strip()
    if env_key:
        try:
            key = base64.urlsafe_b64decode(env_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("MISTAKE_BOOK_MASTER_KEY 不是有效的 URL-safe Base64") from exc
        if len(key) != 32:
            raise RuntimeError("MISTAKE_BOOK_MASTER_KEY 解码后必须为 32 字节")
        return key, "environment"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes()
        if len(key) != 32:
            raise RuntimeError("本地主密钥长度异常，请先备份数据并检查 .credential.key")
        _chmod_private(KEY_FILE)
        return key, "local-key-file"
    key = secrets.token_bytes(32)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key, "local-key-file"


def save(credentials: dict[str, Any]) -> None:
    key, _ = _master_key()
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(credentials, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, AAD)
    payload = {
        "version": VERSION,
        "algorithm": "AES-256-GCM",
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = ENCRYPTED_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _chmod_private(temporary)
    temporary.replace(ENCRYPTED_FILE)
    _chmod_private(ENCRYPTED_FILE)


def load() -> tuple[dict[str, Any], str]:
    if not ENCRYPTED_FILE.exists():
        return {}, "none"
    key, key_source = _master_key()
    try:
        payload = json.loads(ENCRYPTED_FILE.read_text(encoding="utf-8"))
        if payload.get("version") != VERSION or payload.get("algorithm") != "AES-256-GCM":
            raise RuntimeError("不支持的凭证加密格式")
        nonce = base64.urlsafe_b64decode(payload["nonce"])
        ciphertext = base64.urlsafe_b64decode(payload["ciphertext"])
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, AAD)
        return json.loads(plaintext.decode("utf-8")), key_source
    except Exception as exc:
        raise RuntimeError("139 云盘凭证解密失败，密钥可能丢失或文件已损坏") from exc


def load_legacy() -> dict[str, Any]:
    if not LEGACY_FILE.exists():
        return {}
    raw = LEGACY_FILE.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def remove_legacy() -> bool:
    """覆盖后删除旧明文文件，尽量降低普通文件恢复的风险。"""
    if not LEGACY_FILE.exists():
        return False
    size = LEGACY_FILE.stat().st_size
    try:
        with LEGACY_FILE.open("r+b", buffering=0) as file:
            file.write(secrets.token_bytes(size))
            file.flush()
            os.fsync(file.fileno())
    finally:
        LEGACY_FILE.unlink(missing_ok=True)
    return True


def status() -> dict[str, Any]:
    key_source = "environment" if os.environ.get("MISTAKE_BOOK_MASTER_KEY", "").strip() else "local-key-file"
    return {
        "encrypted": ENCRYPTED_FILE.exists(),
        "algorithm": "AES-256-GCM" if ENCRYPTED_FILE.exists() else "",
        "keySource": key_source if ENCRYPTED_FILE.exists() else "",
        "legacyPlaintext": LEGACY_FILE.exists(),
    }
