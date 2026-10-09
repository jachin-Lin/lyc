"""
密码轮换 Celery 任务

- rotate_all_credentials：每 12 小时（默认）轮换全部实例的全部账号密码
- rotate_instance_passwords：按需轮换单个实例
"""
from __future__ import annotations

import uuid

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from dbms.db_instances.models import DatabaseInstance
from dbms.passwords.models import CredentialHistory
from dbms.passwords.rotator import rotate_credential


@shared_task(bind=True, name="dbms.passwords.tasks.rotate_all_credentials",
             soft_time_limit=3600, time_limit=3900)
def rotate_all_credentials(self, *, operator: str = "system",
                           apply_to_database: bool = True) -> dict:
    """
    遍历全部实例的全部生效凭据，轮换密码。

    策略：
    - 每次执行生成 task_id，便于审计与幂等去重
    - 单条失败不影响其他凭据，继续轮换
    - 汇总统计并记录到历史
    """
    task_id = f"rotate-{timezone.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
    from dbms.passwords.models import DatabaseCredential

    qs = DatabaseCredential.objects.filter(is_active=True).select_related("instance")
    total = qs.count()

    success = failed = skipped = 0
    details: list[dict] = []

    for cred in qs.iterator(chunk_size=100):
        # 幂等：同一 task_id 下已处理过的凭据跳过
        if CredentialHistory.objects.filter(task_id=task_id, credential=cred).exists():
            continue
        result = rotate_credential(
            cred,
            operator=operator,
            apply_to_database=apply_to_database,
            task_id=task_id,
        )
        if result.success:
            if result.skipped:
                skipped += 1
            else:
                success += 1
        else:
            failed += 1
        details.append({
            "credential_id": cred.pk,
            "instance": cred.instance.identifier,
            "username": cred.username,
            "success": result.success,
            "verified": result.verified,
            "skipped": result.skipped,
            "error": result.error,
            "duration_ms": result.duration_ms,
        })

    summary = {
        "task_id": task_id,
        "started_at": task_id.split("-")[1],  # 保留日期戳便于排查
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "finished_at": timezone.now().isoformat(),
    }
    # 审计日志
    if failed:
        import logging
        logging.getLogger(__name__).error(
            "密码轮换存在失败项：%s", {k: v for k, v in summary.items() if k != "details"})
    return {**summary, "details": details}


@shared_task(name="dbms.passwords.tasks.rotate_instance_passwords")
def rotate_instance_passwords(instance_id: int, *, usernames: list[str] | None = None,
                              apply_to_database: bool = True,
                              operator: str = "system") -> dict:
    """轮换单个实例的密码（API 手动触发入口）。"""
    from dbms.passwords.rotator import rotate_instance
    try:
        instance = DatabaseInstance.objects.get(pk=instance_id, is_deleted=False)
    except DatabaseInstance.DoesNotExist:
        return {"success": False, "error": f"实例 {instance_id} 不存在或已删除"}
    results = rotate_instance(
        instance, operator=operator, apply_to_database=apply_to_database,
        task_id=f"manual-{timezone.now():%Y%m%d%H%M%S}", usernames=usernames,
    )
    return {
        "instance_id": instance_id,
        "identifier": instance.identifier,
        "total": len(results),
        "success": sum(1 for r in results if r.success),
        "failed": sum(1 for r in results if not r.success),
        "details": [
            {
                "username": r.username,
                "success": r.success,
                "verified": r.verified,
                "skipped": r.skipped,
                "error": r.error,
                "duration_ms": r.duration_ms,
            }
            for r in results
        ],
    }


@shared_task(name="dbms.passwords.tasks.ensure_initial_passwords")
def ensure_initial_passwords() -> dict:
    """
    为所有「没有凭据」的实例创建一条默认 root 凭据（密文）。
    用于演示与初始化，生产环境应由密钥管理系统下发。
    """
    from dbms.passwords.crypto import encrypt_password
    from dbms.passwords.models import DatabaseCredential

    created = 0
    for instance in DatabaseInstance.objects.filter(is_deleted=False).iterator(chunk_size=100):
        if not DatabaseCredential.objects.filter(instance=instance).exists():
            DatabaseCredential.objects.create(
                instance=instance,
                username="root",
                password_ciphertext=encrypt_password("P@ssw0rd-Demo-Only-Change-Me!"),
                is_active=True,
            )
            created += 1
    return {"created": created}
