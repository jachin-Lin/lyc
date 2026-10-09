"""
数据库实例相关 Celery 任务：端口探测
"""
from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from dbms.db_instances.models import DatabaseInstance
from dbms.db_instances.services import probe_tcp


@shared_task(bind=True, name="dbms.db_instances.tasks.batch_probe",
             autoretry_for=(Exception,), max_retries=2, default_retry_delay=60)
def batch_probe(self, instance_ids: list[int], timeout: float = 3.0) -> dict:
    """
    批量 TCP 端口探测（异步）。

    入参：instance_ids: list[int], timeout: float
    出参：{total, reachable, unreachable, details: [...]}
    """
    results: list[dict] = []
    reachable = 0
    for pk in instance_ids or []:
        try:
            instance = DatabaseInstance.objects.get(pk=pk, is_deleted=False)
        except DatabaseInstance.DoesNotExist:
            results.append({"id": pk, "error": "实例不存在或已删除"})
            continue

        result = probe_tcp(instance.host, instance.port, timeout)
        instance.last_probe_at = timezone.now()
        instance.last_probe_reachable = result.reachable
        instance.save(update_fields=["last_probe_at", "last_probe_reachable", "updated_at"])
        if result.reachable:
            reachable += 1
        results.append({
            "id": instance.pk,
            "identifier": instance.identifier,
            "host": instance.host,
            "port": instance.port,
            "reachable": result.reachable,
            "latency_ms": round(result.latency_ms, 3),
            "error": result.error,
        })

    return {
        "total": len(results),
        "reachable": reachable,
        "unreachable": len(results) - reachable,
        "details": results,
        "finished_at": timezone.now().isoformat(),
    }
