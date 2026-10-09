"""
凭据相关视图：列表 / 创建 / 查看明文（审计）
"""
from __future__ import annotations

from celery.result import AsyncResult
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets, status
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.request import Request
from rest_framework.response import Response

from dbms.passwords.crypto import decrypt_password
from dbms.passwords.models import CredentialHistory, DatabaseCredential
from dbms.db_instances.models import DatabaseInstance
from dbms.db_instances.serializers import CredentialCreateSerializer
from dbms.common.exceptions import DecryptError


class CredentialViewSet(viewsets.GenericViewSet,
                                 mixins.ListModelMixin,
                                 mixins.CreateModelMixin):
    """
    GET    /api/v1/instances/{instance_pk}/credentials/   列出某实例全部凭据
    POST   /api/v1/instances/{instance_pk}/credentials/   新增 / 重置凭据
    """
    serializer_class = CredentialCreateSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["is_active", "username"]
    ordering_fields = ["username", "last_rotated_at", "rotate_count"]

    def get_queryset(self):
        instance_pk = self.kwargs.get("instance_pk") or self.kwargs.get("pk")
        return DatabaseCredential.objects.filter(
            instance_id=instance_pk, is_active=True,
        ).select_related("instance")

    def list(self, request: Request, instance_pk: int | None = None):
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        data = []
        for cred in page or queryset:
            # 列表不返回明文，只返回密文长度摘要
            data.append({
                "id": cred.pk,
                "instance_id": cred.instance_id,
                "username": cred.username,
                "ciphertext_length": len(cred.password_ciphertext),
                "ciphertext_preview": cred.password_ciphertext[:12] + "...(密文，不可还原)",
                "key_version": cred.key_version,
                "is_active": cred.is_active,
                "rotate_count": cred.rotate_count,
                "last_rotated_at": cred.last_rotated_at,
                "last_rotated_by": cred.last_rotated_by,
            })
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)

    def create(self, request: Request, instance_pk: int | None = None):
        instance = DatabaseInstance.objects.get(pk=instance_pk, is_deleted=False)
        username = request.data.get("username", "root")
        plaintext = request.data.get("password") or ""

        # 随机生成 / 使用指定密码
        if not plaintext:
            from dbms.passwords.rotator import PasswordGenerator
            plaintext = PasswordGenerator.generate()

        ciphertext = encrypt_password(plaintext)

        cred, created = DatabaseCredential.objects.update_or_create(
            instance=instance, username=username,
            defaults={
                "password_ciphertext": ciphertext,
                "is_active": True,
                "last_rotated_by": request.data.get("operator", "manual"),
            },
        )
        cred._just_created = True  # type: ignore[attr-defined]
        serializer = self.get_serializer(cred)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CredentialPlaintextView(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    GET /api/v1/credentials/{id}/plaintext/   解密并返回明文密码（审计日志会记录）
    """
    queryset = DatabaseCredential.objects.all()
    serializer_class = CredentialCreateSerializer

    def retrieve(self, request: Request, pk: int):
        cred = self.get_object()
        try:
            plaintext = decrypt_password(cred.password_ciphertext)
        except DecryptError as e:
            return Response({"code": 403, "message": str(e), "data": None}, status=403)
        # 审计
        CredentialHistory.objects.create(
            credential=cred, instance=cred.instance, username=cred.username,
            password_ciphertext=cred.password_ciphertext,
            action=CredentialHistory.ROTATE, result=CredentialHistory.SKIPPED,
            verified=False, operator=request.data.get("operator", "anonymous"),
            task_id=f"plaintext-{pk}",
        )
        return Response({
            "id": cred.pk,
            "instance_id": cred.instance_id,
            "username": cred.username,
            "plaintext": plaintext,
            "warning": "明文密码仅在本次响应中返回，请勿记录到日志或提交到仓库。",
        })


class TaskStatusView(viewsets.GenericViewSet, mixins.RetrieveModelMixin):
    """
    GET /api/v1/tasks/{task_id}/   查询异步任务状态
    """
    serializer_class = None  # type: ignore[assignment]

    def retrieve(self, request: Request, pk: str):
        result = AsyncResult(pk)
        response: dict = {
            "task_id": pk,
            "state": result.state,
            "ready": result.ready(),
            "successful": result.successful() if result.ready() else None,
        }
        if result.ready():
            if result.successful():
                response["result"] = result.result
            else:
                response["error"] = str(result.result)
        return Response(response)
