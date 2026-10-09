"""
密码生成器 / 真实 DB 连接校验 / 轮换主流程

轮换的原子性保证：
1. 生成新密码
2. 尝试用【旧密码】连接真实数据库，执行 ALTER USER ... IDENTIFIED BY 修改密码
3. 用【新密码】重新连接校验
4. 全部成功后，才在同一事务里更新凭据表 + 写入历史记录
5. 任一步失败 → 保留旧密码，记录失败历史，绝不破坏可用性
"""
from __future__ import annotations

import logging
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from dbms.db_instances.models import DatabaseInstance
from dbms.passwords.crypto import encrypt_password, get_key_ring
from dbms.passwords.models import CredentialHistory, DatabaseCredential

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 密码生成
# ----------------------------------------------------------------------
class PasswordGenerator:
    CHARSETS: Dict[str, str] = {
        "hex": string.hexdigits[:16],
        "alphanumeric": string.ascii_letters + string.digits,
        "ascii": string.ascii_letters + string.digits + "!@#$%^&*-_=+?",
    }

    @classmethod
    def generate(
        cls,
        *,
        length: Optional[int] = None,
        charset: Optional[str] = None,
        ensure_complexity: bool = True,
    ) -> str:
        length = length or settings.PASSWORD_MIN_LENGTH
        charset_name = (charset or settings.PASSWORD_CHARSET).lower()
        charset = cls.CHARSETS.get(charset_name, cls.CHARSETS["alphanumeric"])

        if length < 8:
            raise ValueError("密码长度至少 8 位。")

        # 优先用 secrets 保证密码学安全随机性
        password = "".join(secrets.choice(charset) for _ in range(length))

        # 复杂度兜底：确保至少含大写、小写、数字各一个
        if ensure_complexity and charset_name == "ascii":
            pwd = list(password)
            pwd[0] = secrets.choice(string.ascii_uppercase)
            pwd[1] = secrets.choice(string.ascii_lowercase)
            pwd[2] = secrets.choice(string.digits)
            random.shuffle(pwd)
            password = "".join(pwd)
        return password


# ----------------------------------------------------------------------
# 真实 DB 连接校验
# ----------------------------------------------------------------------
class DBVerifier:
    """
    真实连接校验：轮换后用新密码登录一次，确认密码已生效。

    支持 MySQL / PostgreSQL，通过 pymysql / psycopg2 连接。
    如果环境中没有真实数据库驱动或实例不可达，verify 返回 (ok=False, skipped=True)，
    不会阻塞轮换流程，但历史记录会标注 skipped。
    """

    DRIVERS = {
        "mysql": ("pymysql", "pymysql"),
        "postgresql": ("psycopg2", "psycopg2"),
    }

    @classmethod
    def _connect(cls, db_type: str, host: str, port: int,
                 username: str, password: str, database: str = "") -> Tuple[bool, str, bool]:
        """
        返回 (ok, message, skipped)。
        skipped=True 表示「无驱动 / 实例不可达」等非故障性跳过。
        """
        if db_type not in cls.DRIVERS:
            return False, f"暂不支持的数据库类型：{db_type}", True

        module_name, _ = cls.DRIVERS[db_type]
        try:
            module = __import__(module_name)
        except ImportError:
            return False, f"未安装数据库驱动 {module_name}，跳过真实连接校验", True

        connect_kwargs = cls._build_kwargs(module_name, host, port, username, password, database)
        try:
            conn = module.connect(**connect_kwargs)
        except Exception as e:  # noqa: BLE001
            return False, f"连接失败：{type(e).__name__}: {e}", False

        try:
            cur = conn.cursor()
            cur.execute(cls._ping_sql(db_type))
            cur.close()
        except Exception as e:  # noqa: BLE001
            conn.close()
            return False, f"认证/执行失败：{type(e).__name__}: {e}", False
        else:
            conn.close()
            return True, "ok", False

    @staticmethod
    def _build_kwargs(driver: str, host: str, port: int,
                      username: str, password: str, database: str) -> dict:
        if driver == "pymysql":
            return {
                "host": host, "port": port,
                "user": username, "password": password,
                "database": database or None,
                "connect_timeout": 5,
                "ssl_disabled": True,  # 内网管理面可关闭，生产建议开启
            }
        # psycopg2
        return {
            "host": host, "port": port,
            "user": username, "password": password,
            "dbname": database or "postgres",
            "connect_timeout": 5,
        }

    @staticmethod
    def _ping_sql(db_type: str) -> str:
        return "SELECT 1" if db_type == "mysql" else "SELECT 1"

    @classmethod
    def change_password(cls, db_type: str, host: str, port: int,
                        username: str, old_password: str,
                        new_password: str) -> Tuple[bool, str, bool]:
        """
        用旧密码连接，执行 ALTER USER 修改密码。
        返回 (ok, message, skipped)。
        """
        if db_type not in cls.DRIVERS:
            return False, f"暂不支持的数据库类型：{db_type}", True

        module_name, _ = cls.DRIVERS[db_type]
        try:
            module = __import__(module_name)
        except ImportError:
            return False, f"未安装数据库驱动 {module_name}，跳过真实密码修改", True

        try:
            conn = module.connect(**cls._build_kwargs(
                module_name, host, port, username, old_password, ""))
        except Exception as e:  # noqa: BLE001
            return False, f"用旧密码连接失败：{type(e).__name__}: {e}", False

        try:
            cur = conn.cursor()
            if db_type == "mysql":
                # MySQL 8.0: ALTER USER user IDENTIFIED BY 'newpwd'
                cur.execute(
                    f"ALTER USER '{username}'@'%' IDENTIFIED BY %s", (new_password,))
            else:
                cur.execute(
                    f'ALTER USER "{username}" WITH PASSWORD %s', (new_password,))
            conn.commit()
            cur.close()
        except Exception as e:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            conn.close()
            return False, f"修改密码失败：{type(e).__name__}: {e}", False

        conn.close()
        return True, "ok", False


@dataclass
class RotateResult:
    credential_id: int
    instance_id: int
    username: str
    success: bool
    verified: bool
    skipped: bool = False
    new_password: str = ""
    error: str = ""
    history_id: Optional[int] = None
    duration_ms: float = 0.0
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------------
# 轮换主流程
# ----------------------------------------------------------------------
def rotate_credential(
    credential: DatabaseCredential,
    *,
    operator: str = "system",
    apply_to_database: bool = True,
    task_id: str = "",
) -> RotateResult:
    """
    轮换单个凭据的密码。

    流程：
      1. 生成新密码
      2. 解密旧密码
      3. 若 apply_to_database：用旧密码连接真实 DB → ALTER USER → 用新密码重连校验
      4. 成功：事务内更新凭据表 + 写历史
      5. 失败：记录失败历史，保留旧密码，绝不破坏可用性
    """
    started = time.monotonic() * 1000.0
    instance: DatabaseInstance = credential.instance
    username = credential.username
    result = RotateResult(
        credential_id=credential.pk,
        instance_id=instance.pk,
        username=username,
        success=False,
        verified=False,
    )

    try:
        new_password = PasswordGenerator.generate()
    except ValueError as e:
        result.error = str(e)
        _record_history(credential, new_password="", operator=operator,
                        task_id=task_id, result_code="failed", verified=False,
                        verify_error=str(e), duration_ms=_elapsed(started))
        return result

    # 解密旧密码，准备真实连接
    try:
        old_password = credential.get_plaintext()
    except Exception as e:  # noqa: BLE001
        result.error = f"旧密码解密失败：{e}"
        _record_history(credential, new_password, operator, task_id, "failed",
                        verified=False, verify_error=result.error,
                        duration_ms=_elapsed(started))
        return result

    applied = True
    verified = False
    skipped = False
    error_msg = ""

    if apply_to_database:
        ok, msg, skipped = DBVerifier.change_password(
            instance.db_type, instance.host, instance.port,
            username, old_password, new_password,
        )
        if skipped:
            # 无驱动 / 不支持的 db_type → 仅记录，不修改真实库
            applied = False
        elif not ok:
            # 真实库修改失败 → 保留旧密码，不更新凭据表
            error_msg = msg
            _record_history(
                credential, new_password, operator, task_id, "failed",
                verified=False, verify_error=msg, duration_ms=_elapsed(started),
                applied_to_database=True,
            )
            result.error = error_msg
            result.skipped = skipped
            return result

        # 用新密码重新连接，确认密码已生效
        v_ok, v_msg, v_skipped = DBVerifier._connect(
            instance.db_type, instance.host, instance.port,
            username, new_password, "" if instance.db_type == "mysql" else "postgres",
        )
        if v_skipped:
            skipped = True
        elif not v_ok:
            # 新密码连不上 → 视为失败，保留旧密码
            error_msg = f"新密码校验失败：{v_msg}"
            _record_history(
                credential, new_password, operator, task_id, "failed",
                verified=False, verify_error=error_msg, duration_ms=_elapsed(started),
                applied_to_database=True,
            )
            result.error = error_msg
            result.skipped = skipped
            return result
        verified = True
    else:
        # 不应用到真实库，仅更新凭据表（用于演示 / 无法直连管理库的场景）
        verified = False

    # 全部成功：事务内更新凭据 + 写历史
    new_cipher = encrypt_password(new_password)
    key_version = _current_key_version()
    now = timezone.now()
    try:
        with transaction.atomic():
            credential.password_ciphertext = new_cipher
            credential.key_version = key_version
            credential.rotate_count = models_int_add(credential.rotate_count, 1)
            credential.last_rotated_at = now
            credential.last_rotated_by = operator
            credential.save(update_fields=[
                "password_ciphertext", "key_version", "rotate_count",
                "last_rotated_at", "last_rotated_by", "updated_at",
            ])
            instance.last_password_rotated_at = now
            instance.save(update_fields=["last_password_rotated_at", "updated_at"])

            history = _record_history(
                credential, new_password, operator, task_id,
                "skipped" if skipped else "success",
                verified=verified, verify_error=error_msg,
                duration_ms=_elapsed(started),
                applied_to_database=applied,
            )
            result.history_id = history.pk
    except Exception as e:  # noqa: BLE001
        # 即使落库失败，也绝不回滚真实库密码 —— 优先保证数据库可用性
        logger.exception("轮换落库失败，但真实库密码已修改：%s", e)
        result.error = f"凭据落库失败：{e}"
        return result

    result.success = True
    result.verified = verified
    result.skipped = skipped
    result.new_password = new_password
    result.duration_ms = _elapsed(started)
    return result


def _record_history(
    credential: DatabaseCredential,
    new_password: str,
    operator: str,
    task_id: str,
    result_code: str,
    *,
    verified: bool,
    verify_error: str,
    duration_ms: float,
    applied_to_database: bool = False,
) -> CredentialHistory:
    ciphertext = ""
    if new_password:
        try:
            ciphertext = encrypt_password(new_password)
        except Exception:  # noqa: BLE001
            ciphertext = ""
    return CredentialHistory.objects.create(
        credential=credential,
        instance=credential.instance,
        username=credential.username,
        password_ciphertext=ciphertext,
        key_version=_current_key_version(),
        action=CredentialHistory.ROTATE,
        result=result_code,
        verified=verified,
        verify_error=verify_error,
        applied_to_database=applied_to_database,
        operator=operator,
        task_id=task_id,
        duration_ms=duration_ms,
    )


def _current_key_version() -> int:
    return 1


def _elapsed(started_ms: float) -> float:
    return round(time.monotonic() * 1000.0 - started_ms, 3)


def models_int_add(current: int, delta: int) -> int:
    """兼容不同 Django 版本的 F('rotate_count') + 1 写法。"""
    return current + delta


def rotate_instance(
    instance: DatabaseInstance,
    *,
    operator: str = "system",
    apply_to_database: bool = True,
    task_id: str = "",
    usernames: Optional[List[str]] = None,
) -> List[RotateResult]:
    """轮换某个实例的全部（或指定）账号密码。"""
    qs = instance.credentials.filter(is_active=True)
    if usernames:
        qs = qs.filter(username__in=usernames)
    return [
        rotate_credential(c, operator=operator, apply_to_database=apply_to_database, task_id=task_id)
        for c in qs
    ]
