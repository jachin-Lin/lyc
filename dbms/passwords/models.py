"""
密码 / 凭据模型

- DatabaseCredential：每个 (instance, username) 唯一，存当前密码密文
- CredentialHistory：每次轮换的审计记录（含密文、校验结果、操作者）
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from dbms.db_instances.models import DatabaseInstance


class DatabaseCredential(models.Model):
    """数据库实例的账号密码（密文存储）。"""

    instance = models.ForeignKey(
        DatabaseInstance, on_delete=models.CASCADE,
        related_name="credentials", verbose_name="所属实例",
    )
    username = models.CharField(max_length=128, verbose_name="账号")
    # 密文：Fernet token，任何人拿到 token 也无法在无密钥的情况下还原
    password_ciphertext = models.TextField(verbose_name="密码密文")
    # 密钥版本，用于密钥轮转后仍能解密历史数据（MultiFernet 兼容）
    key_version = models.PositiveSmallIntegerField(default=1, verbose_name="密钥版本")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="是否生效")
    valid_from = models.DateTimeField(auto_now_add=True, verbose_name="生效时间")
    valid_until = models.DateTimeField(null=True, blank=True, verbose_name="失效时间")
    # 统计
    rotate_count = models.PositiveIntegerField(default=0, verbose_name="已轮换次数")
    last_rotated_at = models.DateTimeField(null=True, blank=True, verbose_name="最近轮换时间")
    last_rotated_by = models.CharField(max_length=128, blank=True, default="", verbose_name="最近轮换人")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "database_credential"
        unique_together = [("instance", "username")]
        indexes = [
            models.Index(fields=["instance", "is_active"]),
        ]
        ordering = ["-created_at"]
        verbose_name = "数据库凭据"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.instance.identifier} / {self.username}"

    def set_plaintext(self, plaintext: str, *, key_version: int = 1) -> None:
        """将明文密码加密后存入密文字段。"""
        from dbms.passwords.crypto import encrypt_password
        self.password_ciphertext = encrypt_password(plaintext)
        self.key_version = key_version

    def get_plaintext(self) -> str:
        """解密并返回明文密码。调用方负责审计。"""
        from dbms.passwords.crypto import decrypt_password
        return decrypt_password(self.password_ciphertext)


class CredentialHistory(models.Model):
    """密码轮换历史 / 审计记录。"""

    ROTATE = "rotate"
    CREATE = "create"
    REVOKE = "revoke"
    ACTION_CHOICES = [
        (ROTATE, "轮换"),
        (CREATE, "创建"),
        (REVOKE, "吊销"),
    ]

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RESULT_CHOICES = [
        (SUCCESS, "成功"),
        (FAILED, "失败"),
        (SKIPPED, "跳过"),
    ]

    credential = models.ForeignKey(
        DatabaseCredential, on_delete=models.SET_NULL, null=True,
        related_name="history", verbose_name="关联凭据",
    )
    instance = models.ForeignKey(
        DatabaseInstance, on_delete=models.SET_NULL, null=True,
        related_name="credential_history", verbose_name="关联实例",
    )
    username = models.CharField(max_length=128, verbose_name="账号")
    password_ciphertext = models.TextField(verbose_name="密码密文")
    key_version = models.PositiveSmallIntegerField(default=1, verbose_name="密钥版本")
    action = models.CharField(max_length=16, choices=ACTION_CHOICES, default=ROTATE, verbose_name="操作")
    result = models.CharField(max_length=16, choices=RESULT_CHOICES, default=SUCCESS, verbose_name="结果")
    # 校验环节的结果
    verified = models.BooleanField(default=False, verbose_name="是否已校验")
    verify_error = models.TextField(blank=True, default="", verbose_name="校验错误")
    # 轮换方式
    applied_to_database = models.BooleanField(default=False, verbose_name="是否已应用到数据库")
    operator = models.CharField(max_length=128, blank=True, default="", verbose_name="操作者")
    task_id = models.CharField(max_length=128, blank=True, default="", verbose_name="任务ID")
    duration_ms = models.FloatField(default=0.0, verbose_name="耗时(ms)")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间")

    class Meta:
        db_table = "credential_history"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["instance", "created_at"]),
            models.Index(fields=["credential", "created_at"]),
        ]
        verbose_name = "凭据历史"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.instance_id} / {self.username} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
