"""
系统状态 / 版本接口
"""
from __future__ import annotations

from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView


class SystemStatusView(APIView):
    """系统状态：不查 DB，避免被中间件统计。"""

    def get(self, request):
        return Response({
            "service": "dbms",
            "status": "ok",
            "debug": settings.DEBUG,
            "timezone": settings.TIME_ZONE,
        })


class APIVersionView(APIView):
    """API 版本。"""

    def get(self, request):
        return Response({
            "name": "Database Instance Management System",
            "version": "1.0.0",
            "api_version": "v1",
        })
