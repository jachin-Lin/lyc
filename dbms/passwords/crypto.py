"""
密码加解密

使用 cryptography.fernet（AES-128-CBC + HMAC-SHA256），
提供对称加密 / 解密 / 主密钥生成 / 密钥版本管理。

设计原则：
- 主密钥 NEVER 出现在代码、日志、序列化结果里
- 支持多密钥（MultiFernet）做密钥轮转，历史密文仍可解密
- 坏数据一律返回 None / 抛 DecryptError，绝不崩溃
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from dbms.common.exceptions import DecryptError


@dataclass(frozen=True)
class KeyRing:
    """密钥环：主密钥用于加密，历史密钥用于解密旧数据。"""

    primary: bytes
    legacy: tuple[bytes, ...] = ()

    def fernet(self) -> MultiFernet:
        keys = [Fernet(self.primary)]
        for k in self.legacy:
            keys.append(Fernet(k))
        return MultiFernet(keys)


_ring: Optional[KeyRing] = None


def get_key_ring() -> KeyRing:
    """返回当前密钥环（惰性单例）。"""
    global _ring
    if _ring is None:
        primary, legacy = _load_keys_from_env()
        _ring = KeyRing(primary=primary, legacy=tuple(legacy))
    return _ring


def reload_key_ring() -> KeyRing:
    """强制重新加载密钥环（运维轮换主密钥后调用）。"""
    global _ring
    _ring = None
    return get_key_ring()


def _load_keys_from_env() -> tuple[bytes, List[bytes]]:
    raw = os.environ.get("FERNET_KEY", "")
    if not raw:
        raise DecryptError("未配置 FERNET_KEY 环境变量，无法加密密码。")
    # 支持逗号分隔的多个密钥：第一个为主密钥
    parts = [p.strip().encode() for p in raw.split(",") if p.strip()]
    if not parts:
        raise DecryptError("FERNET_KEY 配置为空。")
    try:
        # 预校验：确保是合法 base64，避免运行时才报错
        Fernet(parts[0])
    except Exception as e:
        raise DecryptError(f"FERNET_KEY 不合法（需要 URL-safe base64 32 字节）：{e}") from e
    return parts[0], parts[1:]


def generate_key() -> str:
    """生成一个新的 Fernet 主密钥字符串。"""
    return Fernet.generate_key().decode()


def ensure_fernet_key() -> str:
    """返回当前主密钥；若未配置则生成一个并打印提示。"""
    raw = os.environ.get("FERNET_KEY", "")
    if raw:
        return raw.split(",")[0]
    new_key = generate_key()
    print(f"[crypto] 未检测到 FERNET_KEY，已生成新密钥，请写入 .env：\nFERNET_KEY={new_key}")
    return new_key


def encrypt_password(plaintext: str) -> str:
    """将明文密码加密为可持久化的 token 字符串。"""
    if plaintext is None:
        raise DecryptError("明文密码不能为 None。")
    token = get_key_ring().fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_password(ciphertext: str, *, ttl: Optional[int] = None) -> str:
    """
    解密密文得到明文密码。

    ttl: 若指定，超过 ttl 秒的 token 视为过期。
    """
    if not ciphertext:
        raise DecryptError("密文为空。")
    try:
        token = get_key_ring().fernet().decrypt(ciphertext.encode("utf-8"), ttl=ttl)
    except InvalidToken as e:
        raise DecryptError(f"密文无效或已过期：{e}") from e
    return token.decode("utf-8")


def is_ciphertext_valid(ciphertext: str) -> bool:
    """仅校验密文是否可解密，不返回明文（用于健康检查）。"""
    try:
        decrypt_password(ciphertext)
        return True
    except DecryptError:
        return False
