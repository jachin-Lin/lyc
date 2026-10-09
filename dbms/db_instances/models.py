"""
领域模型：部门 / 集群 / 数据库实例

设计要点：
- 三个核心模型通过外键构成「部门 → 集群 → 实例」层级
- 实例冗余保存 department 外键，统计时不必连表
- 全字段审计（created_by / updated_by / created_at / updated_at）
- 统一使用 SoftDeletableModel 做软删除
- JSONField 存规格、备份策略、标签等非结构化信息
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class SoftDeletableModel(models.Model):
    """软删除基类：DELETE 只置位 is_deleted，queryset 默认过滤掉。"""

    is_deleted = models.BooleanField(default=False, db_index=True, verbose_name="已删除")
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name="删除时间")

    class Meta:
        abstract = True

    def delete(self, using: Any = None, keep_parents: bool = False) -> tuple[int, Dict[str, int]]:
        # 强制走软删除，避免误删真实数据
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
        return (1, {self._meta.label: 1})

    def hard_delete(self, using: Any = None, keep_parents: bool = False) -> tuple[int, Dict[str, int]]:
        return super().delete(using=using, keep_parents=keep_parents)


class TimestampedModel(models.Model):
    """时间戳基类。"""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        now = timezone.now()
        if not self.pk:
            self.created_at = now
        self.updated_at = now
        super().save(*args, **kwargs)


class AuditModel(TimestampedModel):
    """审计基类：记录操作者。"""

    created_by = models.CharField(max_length=128, blank=True, default="", verbose_name="创建人")
    updated_by = models.CharField(max_length=128, blank=True, default="", verbose_name="更新人")

    class Meta:
        abstract = True


# ----------------------------------------------------------------------
# 枚举
# ----------------------------------------------------------------------
class DBType(models.TextChoices):
    MYSQL = "mysql", "MySQL"
    POSTGRESQL = "postgresql", "PostgreSQL"
    REDIS = "redis", "Redis"
    MONGODB = "mongodb", "MongoDB"
    TIDB = "tidb", "TiDB"
    OTHER = "other", "其他"


class InstanceRole(models.TextChoices):
    PRIMARY = "primary", "主库"
    REPLICA = "replica", "从库"
    READONLY = "readonly", "只读"
    ARBITER = "arbiter", "仲裁"
    CONFIG = "config", "配置节点"


class InstanceStatus(models.TextChoices):
    CREATING = "creating", "创建中"
    RUNNING = "running", "运行中"
    STOPPED = "stopped", "已停止"
    FAILED = "failed", "异常"
    DELETING = "deleting", "删除中"


class ClusterStatus(models.TextChoices):
    PLANNING = "planning", "规划中"
    RUNNING = "running", "运行中"
    STOPPED = "stopped", "已停止"
    MAINTENANCE = "maintenance", "维护中"
    DECOMMISSIONED = "decommissioned", "已下线"


class Topology(models.TextChoices):
    SINGLE = "single", "单机"
    PRIMARY_REPLICA = "primary_replica", "一主一从"
    PRIMARY_MULTI_REPLICA = "primary_multi_replica", "一主多从"
    CLUSTER = "cluster", "集群"
    SHARDED = "sharded", "分片"


class Vendor(models.TextChoices):
    SELF_HOSTED = "self_hosted", "自建"
    ALIYUN = "aliyun", "阿里云"
    TENCENT = "tencent", "腾讯云"
    AWS = "aws", "AWS"
    HUAWEI = "huawei", "华为云"
    OTHER = "other", "其他"


class Environment(models.TextChoices):
    DEV = "dev", "开发"
    TEST = "test", "测试"
    STAGING = "staging", "预发"
    PROD = "prod", "生产"


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------
class Department(SoftDeletableModel, AuditModel):
    """部门 / 组织架构。"""

    name = models.CharField(max_length=128, unique=True, verbose_name="部门名称")
    code = models.CharField(max_length=64, unique=True, verbose_name="部门编码")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="children", verbose_name="上级部门",
    )
    manager_name = models.CharField(max_length=64, blank=True, default="", verbose_name="负责人姓名")
    manager_email = models.EmailField(blank=True, default="", verbose_name="负责人邮箱")
    manager_phone = models.CharField(max_length=32, blank=True, default="", verbose_name="负责人电话")
    cost_center = models.CharField(max_length=64, blank=True, default="", verbose_name="成本中心")
    description = models.TextField(blank=True, default="", verbose_name="描述")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="是否启用")

    class Meta:
        db_table = "department"
        ordering = ["code"]
        verbose_name = "部门"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"

    def clean(self) -> None:
        if self.parent_id and self.parent_id == self.pk:
            raise ValidationError({"parent": "部门不能将自己设为上级。"})

    @property
    def ancestors(self) -> models.QuerySet["Department"]:
        """祖先路径：此处用递归查询模拟，真实场景可加 closure table。"""
        return Department.objects.filter(
            pk__in=self._ancestor_ids(),
        ).order_by("code")

    def _ancestor_ids(self) -> list[int]:
        ids: list[int] = []
        node = self
        seen: set[int] = set()
        for _ in range(10):  # 防环，最多 10 层
            if not node.parent_id or node.parent_id in seen:
                break
            seen.add(node.parent_id)
            ids.append(node.parent_id)
            try:
                node = node.parent
            except Department.DoesNotExist:
                break
        return ids


class Cluster(SoftDeletableModel, AuditModel):
    """数据库集群。一个部门可拥有多个集群。"""

    name = models.CharField(max_length=128, unique=True, verbose_name="集群名称")
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="clusters",
        verbose_name="所属部门",
    )
    db_type = models.CharField(
        max_length=32, choices=DBType.choices, default=DBType.MYSQL, verbose_name="数据库类型",
    )
    version = models.CharField(max_length=32, blank=True, default="", verbose_name="主版本")
    region = models.CharField(max_length=64, blank=True, default="", verbose_name="地域")
    availability_zone = models.CharField(max_length=64, blank=True, default="", verbose_name="可用区")
    vendor = models.CharField(
        max_length=32, choices=Vendor.choices, default=Vendor.SELF_HOSTED, verbose_name="云厂商",
    )
    topology = models.CharField(
        max_length=32, choices=Topology.choices, default=Topology.SINGLE, verbose_name="拓扑",
    )
    node_count = models.PositiveSmallIntegerField(default=1, verbose_name="节点数")
    max_connections = models.PositiveIntegerField(default=1000, verbose_name="最大连接数")
    cpu_cores = models.PositiveSmallIntegerField(default=0, verbose_name="CPU 核数")
    memory_gb = models.PositiveIntegerField(default=0, verbose_name="内存(GB)")
    disk_gb = models.PositiveIntegerField(default=0, verbose_name="磁盘(GB)")
    vip = models.GenericIPAddressField(null=True, blank=True, verbose_name="VIP")
    vpc_id = models.CharField(max_length=128, blank=True, default="", verbose_name="VPC ID")
    subnet_id = models.CharField(max_length=128, blank=True, default="", verbose_name="子网 ID")
    status = models.CharField(
        max_length=32, choices=ClusterStatus.choices,
        default=ClusterStatus.RUNNING, db_index=True, verbose_name="状态",
    )
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="是否启用")
    description = models.TextField(blank=True, default="", verbose_name="描述")
    launched_at = models.DateField(null=True, blank=True, verbose_name="上线日期")

    class Meta:
        db_table = "cluster"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["department", "status"]),
            models.Index(fields=["db_type", "status"]),
        ]
        verbose_name = "集群"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.name} ({self.get_db_type_display()})"


class DatabaseInstance(SoftDeletableModel, AuditModel):
    """数据库实例。"""

    identifier = models.CharField(max_length=128, unique=True, verbose_name="实例标识")
    name = models.CharField(max_length=128, verbose_name="实例名称")
    cluster = models.ForeignKey(
        Cluster, on_delete=models.PROTECT, related_name="instances", verbose_name="所属集群",
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="instances",
        verbose_name="所属部门",
    )
    db_type = models.CharField(
        max_length=32, choices=DBType.choices, default=DBType.MYSQL, verbose_name="数据库类型",
    )
    version = models.CharField(max_length=32, blank=True, default="", verbose_name="版本")
    host = models.CharField(max_length=255, verbose_name="主机地址")
    port = models.PositiveIntegerField(default=3306, verbose_name="端口")
    role = models.CharField(
        max_length=32, choices=InstanceRole.choices,
        default=InstanceRole.PRIMARY, db_index=True, verbose_name="角色",
    )
    environment = models.CharField(
        max_length=16, choices=Environment.choices,
        default=Environment.PROD, db_index=True, verbose_name="环境",
    )
    status = models.CharField(
        max_length=32, choices=InstanceStatus.choices,
        default=InstanceStatus.RUNNING, db_index=True, verbose_name="状态",
    )

    # 规格（非结构化）
    engine_config = models.JSONField(default=dict, blank=True, verbose_name="引擎配置")
    spec = models.JSONField(default=dict, blank=True, verbose_name="规格")
    backup_policy = models.JSONField(default=dict, blank=True, verbose_name="备份策略")
    tags = models.JSONField(default=dict, blank=True, verbose_name="标签")

    # 业务信息
    owner = models.CharField(max_length=64, blank=True, default="", verbose_name="业务负责人")
    owner_group = models.CharField(max_length=128, blank=True, default="", verbose_name="业务组")
    monitor_url = models.URLField(blank=True, default="", verbose_name="监控面板")
    ops_ticket = models.CharField(max_length=128, blank=True, default="", verbose_name="工单号")
    remark = models.TextField(blank=True, default="", verbose_name="备注")

    # 探测 & 轮换状态
    last_probe_at = models.DateTimeField(null=True, blank=True, verbose_name="最近探测时间")
    last_probe_reachable = models.BooleanField(null=True, blank=True, verbose_name="最近探测是否可达")
    last_password_rotated_at = models.DateTimeField(null=True, blank=True, verbose_name="最近密码轮换时间")

    class Meta:
        db_table = "database_instance"
        ordering = ["identifier"]
        indexes = [
            models.Index(fields=["department", "cluster"]),
            models.Index(fields=["db_type", "environment"]),
            models.Index(fields=["status", "environment"]),
            models.Index(fields=["last_probe_reachable"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(port__gte=1) & models.Q(port__lte=65535),
                name="instance_port_range",
            ),
        ]
        verbose_name = "数据库实例"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.identifier} ({self.host}:{self.port})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # 冗余字段：创建时从集群带出 department / db_type / version，保持一致性
        if self.cluster_id and (not self.department_id or self.department_id != self.cluster.department_id):
            self.department_id = self.cluster.department_id
        if self.cluster_id and not self.db_type:
            self.db_type = self.cluster.db_type
        if self.cluster_id and not self.version:
            self.version = self.cluster.version
        super().save(*args, **kwargs)

    def get_connection_kwargs(self) -> Dict[str, Any]:
        """返回连接真实 DB 所需的参数字典，供密码轮换时的连接校验使用。"""
        return {
            "db_type": self.db_type,
            "host": self.host,
            "port": self.port,
            "identifier": self.identifier,
        }
