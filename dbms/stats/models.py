"""
每日实例数量聚合统计

- DailyInstanceStat：按维度（部门 / 集群 / 全局）保存每日快照
- 维度由 group_by 区分，避免为每种维度建一张表
"""
from __future__ import annotations

from django.db import models

from dbms.db_instances.models import Cluster, Department


class DailyInstanceStat(models.Model):
    """每日实例数量快照。"""

    GROUP_BY_CHOICES = [
        ("global", "全局"),
        ("department", "部门维度"),
        ("cluster", "集群维度"),
        ("db_type", "数据库类型维度"),
        ("environment", "环境维度"),
    ]

    stat_date = models.DateField(db_index=True, verbose_name="统计日期")
    group_by = models.CharField(max_length=16, choices=GROUP_BY_CHOICES, default="global",
                                db_index=True, verbose_name="统计维度")
    # 维度对象：全局维度时为 NULL
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="daily_stats", verbose_name="部门",
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="daily_stats", verbose_name="集群",
    )
    # 数量
    count = models.PositiveIntegerField(default=0, verbose_name="实例数量")
    # 按 db_type 细分，便于前端直接渲染
    by_db_type = models.JSONField(default=dict, blank=True, verbose_name="按类型细分")
    # 元信息
    computed_at = models.DateTimeField(auto_now_add=True, verbose_name="计算时间")
    duration_ms = models.FloatField(default=0.0, verbose_name="计算耗时(ms)")
    task_id = models.CharField(max_length=128, blank=True, default="", verbose_name="任务ID")

    class Meta:
        db_table = "daily_instance_stat"
        unique_together = [("stat_date", "group_by", "department", "cluster")]
        ordering = ["-stat_date", "group_by"]
        indexes = [
            models.Index(fields=["stat_date", "group_by"]),
        ]
        verbose_name = "每日实例统计"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.stat_date} / {self.get_group_by_display()} = {self.count}"
