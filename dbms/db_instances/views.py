"""
视图：Department / Cluster / DatabaseInstance 的增删改查 + 端口探测
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from celery.result import AsyncResult
from django.db.models import QuerySet
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.request import Request
from rest_framework.response import Response

from dbms.db_instances.models import (
    Cluster, DatabaseInstance, DBType, Department,
)
from dbms.db_instances.services import probe_tcp
from dbms.db_instances.tasks import batch_probe
from dbms.passwords.crypto import encrypt_password
from dbms.passwords.models import DatabaseCredential

logger = logging.getLogger(__name__)


def _resolve_serializer(path: str):
    """通过字符串路径延迟导入序列化器，避免与 serializers 之间的循环引用。"""
    from django.utils.module_loading import import_string
    return import_string(path)


class DepartmentViewSet(viewsets.ModelViewSet):
    """部门 CRUD。"""

    queryset = Department.objects.filter(is_deleted=False).select_related("parent")
    def get_serializer_class(self):
        return _resolve_serializer("dbms.db_instances.serializers.DepartmentSerializer")
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["is_active", "parent"]
    search_fields = ["name", "code", "manager_name", "description"]
    ordering_fields = ["code", "name", "created_at"]

    def perform_destroy(self, instance: Department) -> None:
        if instance.children.filter(is_deleted=False).exists():
            raise ValueError("该部门下存在子部门，请先处理子部门。")
        if instance.clusters.filter(is_deleted=False).exists():
            raise ValueError("该部门下存在集群，请先迁移集群。")
        instance.delete()


class ClusterViewSet(viewsets.ModelViewSet):
    """集群 CRUD。"""

    queryset = Cluster.objects.filter(is_deleted=False).select_related("department")
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["db_type", "status", "department", "vendor", "topology", "is_active"]
    search_fields = ["name", "region", "availability_zone", "description"]
    ordering_fields = ["name", "node_count", "created_at"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return _resolve_serializer(
                "dbms.db_instances.serializers.ClusterDetailSerializer")
        return _resolve_serializer(
            "dbms.db_instances.serializers.ClusterListSerializer")

    def perform_destroy(self, instance: Cluster) -> None:
        if instance.instances.filter(is_deleted=False).exists():
            raise ValueError("该集群下存在实例，请先迁移或删除实例。")
        instance.delete()


class DatabaseInstanceViewSet(viewsets.ModelViewSet):
    """数据库实例 CRUD。"""

    queryset = DatabaseInstance.objects.filter(is_deleted=False).select_related(
        "department", "cluster",
    )
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = {
        "db_type": ["exact", "in"],
        "environment": ["exact", "in"],
        "status": ["exact", "in"],
        "role": ["exact", "in"],
        "cluster": ["exact"],
        "department": ["exact"],
        "last_probe_reachable": ["exact"],
    }
    search_fields = ["identifier", "name", "host", "owner", "remark", "ops_ticket"]
    ordering_fields = ["identifier", "port", "last_probe_at",
                       "last_password_rotated_at", "created_at"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return _resolve_serializer(
                "dbms.db_instances.serializers.DatabaseInstanceDetailSerializer")
        return _resolve_serializer(
            "dbms.db_instances.serializers.DatabaseInstanceListSerializer")

    def perform_create(self, serializer):
        instance = serializer.save()
        DatabaseCredential.objects.get_or_create(
            instance=instance, username="root",
            defaults={
                "password_ciphertext": encrypt_password("P@ssw0rd-Demo-Only-Change-Me!"),
                "is_active": True,
            },
        )

    @action(detail=True, methods=["post"])
    def probe(self, request: Request, pk: int):
        """
        探测单个实例的 TCP 端口是否可达。

        POST /api/v1/instances/{id}/probe/
        body: {"timeout": 3.0}
        """
        instance = self.get_object()
        serializer = _resolve_serializer(
            "dbms.db_instances.serializers.InstanceProbeSerializer"
        )(data=request.data)
        serializer.is_valid(raise_exception=True)
        timeout = serializer.validated_data.get("timeout", 3.0)

        result = probe_tcp(instance.host, instance.port, timeout)
        instance.last_probe_at = timezone.now()
        instance.last_probe_reachable = result.reachable
        instance.save(update_fields=["last_probe_at", "last_probe_reachable", "updated_at"])

        return Response({
            "id": instance.pk,
            "identifier": instance.identifier,
            "endpoint": f"{instance.host}:{instance.port}",
            "reachable": result.reachable,
            "latency_ms": round(result.latency_ms, 3),
            "error": result.error,
        })

    @action(detail=False, methods=["post"])
    def probe_batch(self, request: Request):
        """
        异步批量探测多个实例的 TCP 端口。

        POST /api/v1/instances/probe_batch/
        body: {"ids": [1, 2, 3], "timeout": 3.0}

        返回 task_id，通过 GET /api/v1/tasks/{task_id}/ 查询结果。
        """
        serializer = _resolve_serializer(
            "dbms.db_instances.serializers.InstanceBatchProbeSerializer"
        )(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = batch_probe.delay(
            serializer.validated_data["ids"],
            timeout=serializer.validated_data.get("timeout", 3.0),
        )
        return Response({
            "task_id": task.id,
            "state": task.state,
            "status_url": f"/api/v1/tasks/{task.id}/",
        }, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def rotate_password(self, request: Request, pk: int):
        """
        手动触发该实例的密码轮换。

        POST /api/v1/instances/{id}/rotate_password/
        body: {"username": "root", "apply_to_database": true}
        """
        from dbms.passwords.tasks import rotate_instance_passwords
        instance = self.get_object()
        username = request.data.get("username", "root")
        apply_to_database = bool(request.data.get("apply_to_database", True))
        operator = request.data.get("operator", "manual")
        result = rotate_instance_passwords(
            instance.id,
            usernames=[username] if username else None,
            apply_to_database=apply_to_database,
            operator=operator,
        )
        return Response(result)
