"""
每日聚合统计 Celery 定时任务

每天 00:00（Asia/Shanghai）统计各部门 / 各集群的实例数量并写入数据库。
"""
from __future__ import annotations

import uuid

from celery import shared_task
from django.utils import timezone

from dbms.stats.services import save_snapshot


@shared_task(bind=True, name="dbms.stats.tasks.daily_aggregate",
             autoretry_for=(Exception,), max_retries=3, default_retry_delay=300,
             expires=3600 * 6)
def daily_aggregate(self, *, hour: int = 0, minute: int = 0,
                    target_date=None) -> dict:
    """
    每天定时执行：
    - 校验当前时间确实落在目标时间点附近，避免 beat 漂移导致重复统计
    - 计算部门 / 集群维度
    - 幂等写入（同一天重复执行覆盖旧快照）
    """
    now = timezone.localtime()
    if target_date is None:
        # 只在目标时刻附近执行，防止 beat 重放
        if now.hour != hour or now.minute != minute:
            return {"skipped": True, "reason": "not at target time", "now": now.isoformat()}
        target_date = now.date()

    task_id = f"daily-{target_date:%Y%m%d}-{uuid.uuid4().hex[:8]}"
    result = save_snapshot(target_date, task_id=task_id, overwrite=True)
    return {**result, "computed_at": now.isoformat()}
