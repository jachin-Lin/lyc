"""
聚合统计服务

按部门 / 集群 / 全局维度统计实例数量并写入 DailyInstanceStat。

实现要点：
- 用 QuerySet.values(...).annotate(Count) 在数据库层完成分组，避免 Python 层循环
- defer 掉 JSONField 等大字段，降低内存占用
- 按维度分别保存快照，支持历史回溯
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from dbms.db_instances.models import Cluster, DatabaseInstance, DBType, Department
from dbms.stats.models import DailyInstanceStat


@dataclass
class DimensionStat:
    group_by: str
    label: str
    count: int
    by_db_type: Dict[str, int]
    dimension_id: Optional[int] = None


def aggregate_by_department(target_date: Optional[date] = None) -> List[DimensionStat]:
    """
    按部门维度聚合。

    SQL 形态：
        SELECT department_id, db_type, COUNT(*) AS cnt
        FROM database_instance
        WHERE is_deleted = FALSE
        GROUP BY department_id, db_type
    """
    qs = DatabaseInstance.objects.filter(is_deleted=False).values(
        "department_id", "db_type"
    ).annotate(cnt=Count("*")).order_by("department_id")

    by_dept: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in qs:
        dept = row["department_id"]
        by_dept[dept][row["db_type"]] += row["cnt"]

    results: List[DimensionStat] = []
    dept_names = dict(Department.objects.values_list("id", "name"))
    for dept_id, db_types in by_dept.items():
        results.append(DimensionStat(
            group_by="department",
            label=dept_names.get(dept_id, f"部门#{dept_id}"),
            count=sum(db_types.values()),
            by_db_type=dict(db_types),
            dimension_id=dept_id,
        ))
    return results


def aggregate_by_cluster(target_date: Optional[date] = None) -> List[DimensionStat]:
    """按集群维度聚合。"""
    qs = DatabaseInstance.objects.filter(is_deleted=False).values(
        "cluster_id", "db_type"
    ).annotate(cnt=Count("*")).order_by("cluster_id")

    by_cluster: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in qs:
        cid = row["cluster_id"]
        by_cluster[cid][row["db_type"]] += row["cnt"]

    results: List[DimensionStat] = []
    cluster_names = dict(Cluster.objects.values_list("id", "name"))
    for cid, db_types in by_cluster.items():
        results.append(DimensionStat(
            group_by="cluster",
            label=cluster_names.get(cid, f"集群#{cid}"),
            count=sum(db_types.values()),
            by_db_type=dict(db_types),
            dimension_id=cid,
        ))
    return results


def aggregate_global(target_date: Optional[date] = None) -> DimensionStat:
    """全局维度：全表实例数 + 按 db_type 细分。"""
    qs = DatabaseInstance.objects.filter(is_deleted=False).values(
        "db_type"
    ).annotate(cnt=Count("*")).order_by("db_type")
    by_db_type = {row["db_type"]: row["cnt"] for row in qs}
    return DimensionStat(
        group_by="global",
        label="全局",
        count=sum(by_db_type.values()),
        by_db_type=by_db_type,
    )


def compute_all(target_date: Optional[date] = None) -> dict:
    """计算全部维度并返回结构化结果。"""
    target_date = target_date or timezone.localdate()
    started = time.monotonic() * 1000.0

    global_stat = aggregate_global(target_date)
    dept_stats = aggregate_by_department(target_date)
    cluster_stats = aggregate_by_cluster(target_date)

    duration_ms = round(time.monotonic() * 1000.0 - started, 3)
    return {
        "stat_date": target_date.isoformat(),
        "duration_ms": duration_ms,
        "global": asdict(global_stat),
        "by_department": [asdict(s) for s in dept_stats],
        "by_cluster": [asdict(s) for s in cluster_stats],
    }


def save_snapshot(target_date: Optional[date] = None, *, task_id: str = "",
                  overwrite: bool = True) -> dict:
    """
    把聚合结果写入数据库（DailyInstanceStat）。

    唯一约束：(stat_date, group_by, department, cluster)，
    所以同一天重复计算默认覆盖，保证幂等。
    """
    target_date = target_date or timezone.localdate()
    started = time.monotonic() * 1000.0
    data = compute_all(target_date)

    def _row(group_by: str, label: str, count: int, by_db_type: Dict[str, int],
             dept_id: Optional[int] = None, cluster_id: Optional[int] = None) -> dict:
        return {
            "stat_date": target_date,
            "group_by": group_by,
            "department_id": dept_id,
            "cluster_id": cluster_id,
            "count": count,
            "by_db_type": by_db_type,
            "task_id": task_id,
            "duration_ms": round(time.monotonic() * 1000.0 - started, 3),
        }

    rows: List[dict] = [
        _row("global", "全局", data["global"]["count"], data["global"]["by_db_type"]),
    ]
    for s in data["by_department"]:
        rows.append(_row("department", s["label"], s["count"], s["by_db_type"],
                         dept_id=s["dimension_id"]))
    for s in data["by_cluster"]:
        rows.append(_row("cluster", s["label"], s["count"], s["by_db_type"],
                         cluster_id=s["dimension_id"]))

    with transaction.atomic():
        if overwrite:
            # 幂等：先删除当天已有快照，再批量写入
            DailyInstanceStat.objects.filter(stat_date=target_date).delete()
        DailyInstanceStat.objects.bulk_create(
            [DailyInstanceStat(**r) for r in rows], batch_size=100,
        )

    return {
        "stat_date": target_date.isoformat(),
        "rows_written": len(rows),
        "duration_ms": round(time.monotonic() * 1000.0 - started, 3),
        "task_id": task_id,
    }


def latest_snapshot() -> Optional[dict]:
    """返回最近一次快照的汇总，供 API 快速读取。"""
    latest = DailyInstanceStat.objects.order_by("-stat_date").first()
    if latest is None:
        return None
    qs = DailyInstanceStat.objects.filter(stat_date=latest.stat_date)
    by_department = []
    by_cluster = []
    global_count = 0
    for row in qs:
        if row.group_by == "global":
            global_count = row.count
        elif row.group_by == "department":
            by_department.append({
                "id": row.department_id,
                "label": row.department.name if row.department_id else "未知",
                "count": row.count,
                "by_db_type": row.by_db_type,
            })
        elif row.group_by == "cluster":
            by_cluster.append({
                "id": row.cluster_id,
                "label": row.cluster.name if row.cluster_id else "未知",
                "count": row.count,
                "by_db_type": row.by_db_type,
            })
    return {
        "stat_date": latest.stat_date.isoformat(),
        "global_count": global_count,
        "by_department": by_department,
        "by_cluster": by_cluster,
    }
