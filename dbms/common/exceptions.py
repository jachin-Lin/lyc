"""
自定义异常：统一的错误响应 + 加解密异常
"""
from __future__ import annotations

from rest_framework.views import exception_handler as drf_exception_handler


class DecryptError(Exception):
    """密码解密失败。"""


def custom_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        # 非 DRF 可识别异常，返回 500
        from rest_framework.response import Response
        response = Response(
            {"detail": str(exc) if str(exc) else "服务器内部错误"}, status=500,
        )
    return response
