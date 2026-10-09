"""
统计相关视图：
- 每日快照列表
- 手动触发聚合
- 请求耗时查询
"""
from __future__ import annotations

from django.db.models import Count, FloatField, Q
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response
from rest_framework.views import APIView

from dbms.common.models import RequestLog
from dbms.stats.models import DailyInstanceStat
from dbms.stats.services import compute_all, latest_snapshot, save_snapshot


class DailyStatListViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """每日实例统计快照列表。"""

    queryset = DailyInstanceStat.objects.select_related("department", "cluster").all()
    filterset_fields = ["stat_date", "group_by", "department", "cluster"]
    ordering_fields = ["stat_date", "group_by", "count"]

    def get_queryset(self):
        qs = super().get_queryset()
        # 支持按日期区间过滤
        start = self.request.query_params.get("start_date")
        end = self.request.query_params.get("end_date")
        if start:
            qs = qs.filter(stat_date__gte=start)
        if end:
            qs = qs.filter(stat_date__lte=end)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        # 按维度聚合展示
        group_by = request.query_params.get("group_by", "")
        data = []
        for row in queryset:
            item = {
                "id": row.pk,
                "stat_date": row.stat_date.isoformat(),
                "group_by": row.group_by,
                "group_by_display": row.get_group_by_display(),
                "count": row.count,
                "by_db_type": row.by_db_type,
                "computed_at": row.computed_at,
                "duration_ms": row.duration_ms,
            }
            if row.group_by == "department":
                item["dimension_id"] = row.department_id
                item["dimension_name"] = row.department.name if row.department_id else "未知"
            elif row.group_by == "cluster":
                item["dimension_id"] = row.cluster_id
                item["dimension_name"] = row.cluster.name if row.cluster_id else "未知"
            data.append(item)

        # 非按 group_by 过滤时，额外返回汇总
        if not group_by:
            summary = latest_snapshot()
            if summary:
                data = {"summary": summary, "rows": data}
        return Response(data)


class DailyStatGenerateView(APIView):
    """手动触发一次聚合统计。POST /api/v1/stats/generate/"""

    def post(self, request):
        target = request.data.get("stat_date") or None
        result = save_snapshot(target, overwrite=True)
        return Response({"code": 0, "message": "统计完成", "data": result})

    def get(self, request):
        """返回最近一次快照汇总。"""
        summary = latest_snapshot()
        if summary is None:
            return Response({"code": 404, "message": "暂无统计数据，请先调用 POST 生成"})
        return Response({"code": 0, "message": "success", "data": summary})


class RequestTimingListViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """请求耗时查询。"""

    queryset = RequestLog.objects.all()
    filterset_fields = ["method", "status_code"]
    ordering_fields = ["latency_ms", "created_at", "status_code"]

    def get_queryset(self):
        qs = super().get_queryset()
        path = self.request.query_params.get("path")
        min_ms = self.request.query_params.get("min_ms")
        start = self.request.query_params.get("start")
        end = self.request.query_params.get("end")
        if path:
            qs = qs.filter(path__icontains=path)
        if min_ms:
            try:
                qs = qs.filter(latency_ms__gte=float(min_ms))
            except ValueError:
                pass
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        # 实时聚合：平均 / P95 / P99 / 最大 / 总数
        stats = self._aggregate(queryset)
        page = self.paginate_queryset(queryset[:5000])  # 单次最多返回 5000 条明细
        data = [{
            "id": r.pk,
            "request_id": r.request_id,
            "method": r.method,
            "path": r.path,
            "view_name": r.view_name,
            "status_code": r.status_code,
            "latency_ms": round(r.latency_ms, 3),
            "ip": r.ip,
            "user": r.user,
            "created_at": r.created_at,
        } for r in page or queryset[:5000]]
        return self.get_paginated_response({"stats": stats, "logs": data})

    @staticmethod
    def _aggregate(qs) -> dict:
        total = qs.count()
        if not total:
            return {"total": 0}
        import math
        values = sorted(qs.values_list("latency_ms", flat=True))
        avg = sum(values) / len(values)
        p95 = values[int(math.ceil(0.95 * len(values))) - 1]
        p99 = values[int(math.ceil(0.99 * len(values))) - 1]
        return {
            "total": total,
            "avg_ms": round(avg, 3),
            "p95_ms": round(p95, 3),
            "p99_ms": round(p99, 3),
            "max_ms": round(values[-1], 3),
            "min_ms": round(values[0], 3),
        }
