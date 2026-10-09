"""
Celery 应用实例

- broker / backend 复用 settings.CELERY_*
- 显式 import 任务模块，确保 worker 能发现任务
- 通过 Celery beat 调度：
    · 每 12 小时：轮换全部实例密码
    · 每天 00:00：按部门 / 集群维度统计实例数量并落库
"""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbms.settings")

app = Celery("dbms")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# 定时任务：每 12 小时轮换密码 + 每天 00:00 聚合统计
app.conf.beat_schedule = {
    "rotate-passwords-every-12h": {
        "task": "dbms.passwords.tasks.rotate_all_credentials",
        "schedule": 3600 * 12,
    },
    "daily-instance-aggregate-at-midnight": {
        "task": "dbms.stats.tasks.daily_aggregate",
        "schedule": crontab(hour=0, minute=0),
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:
    return f"Request: {self.request!r}"
