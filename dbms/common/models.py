"""
通用审计模型：请求耗时日志、审计日志
"""
from __future__ import annotations

from django.db import models


class RequestLog(models.Model):
    """请求耗时审计日志（由 RequestTimingMiddleware 写入）。"""

    request_id = models.CharField(max_length=128, db_index=True, verbose_name="请求ID")
    method = models.CharField(max_length=16, verbose_name="请求方法")
    path = models.CharField(max_length=512, db_index=True, verbose_name="请求路径")
    view_name = models.CharField(max_length=255, blank=True, default="", verbose_name="视图名称")
    status_code = models.PositiveSmallIntegerField(db_index=True, verbose_name="状态码")
    latency_ms = models.FloatField(db_index=True, verbose_name="耗时(ms)")
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="客户端IP")
    user = models.CharField(max_length=150, blank=True, default="", verbose_name="用户")
    query = models.TextField(blank=True, default="", verbose_name="查询参数")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间")

    class Meta:
        db_table = "request_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["method", "created_at"]),
            models.Index(fields=["status_code", "created_at"]),
        ]
        verbose_name = "请求日志"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.method} {self.path} {self.status_code} {self.latency_ms:.1f}ms"
