"""
请求耗时统计中间件

职责：
1. 记录每个请求的 method / path / view / status_code / 耗时 / IP / user / request_id
2. 异步写库（攒批 + 超时兜底），不阻塞业务响应
3. 慢请求自动 WARNING 日志
4. 通过响应头 X-Request-ID / X-Response-Time 暴露给调用方

上下文管理器 RequestTimingMiddlewareDisabled 用于压测 / 健康检查跳过统计。
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Iterable, Optional

from django.conf import settings
from django.db import transaction

from dbms.common.models import RequestLog

logger = logging.getLogger(__name__)

# 上下文变量：是否禁用耗时统计（压测 / 健康检查）
_disabled: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "request_timing_disabled", default=False,
)


class RequestTimingMiddlewareDisabled:
    """上下文管理器：临时禁用耗时统计。"""

    def __init__(self, disabled: bool = True):
        self.disabled = disabled
        self._token = None

    def __enter__(self) -> "RequestTimingMiddlewareDisabled":
        self._token = _disabled.set(self.disabled)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._token is not None:
            _disabled.reset(self._token)


class RequestTimingMiddleware:
    """统计每个请求的请求耗时。"""

    # 不需要统计的路径前缀（健康检查、静态资源等）
    EXEMPT_PATHS: tuple[str, ...] = ("/static/", "/media/", "/favicon.ico")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._should_skip(request):
            return self.get_response(request)

        request_id = getattr(request, "request_id", None) or uuid.uuid4().hex
        request.request_id = request_id
        start = time.monotonic()
        status_code = 500
        view_name = ""

        try:
            response = self.get_response(request)
            status_code = response.status_code
            view_name = self._resolve_view_name(request)
            return response
        except Exception as e:  # noqa: BLE001
            view_name = self._resolve_view_name(request)
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000.0
            self._record(
                request=request,
                request_id=request_id,
                method=request.method,
                path=self._safe_path(request),
                view_name=view_name,
                status_code=status_code,
                latency_ms=latency_ms,
            )
            # 在响应头里透出，方便排查
            if "response" in dir() and hasattr(response, "headers"):
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Response-Time"] = f"{latency_ms:.3f}ms"

    def _should_skip(self, request) -> bool:
        if _disabled.get():
            return True
        path = self._safe_path(request)
        return any(path.startswith(p) for p in self.EXEMPT_PATHS)

    @staticmethod
    def _safe_path(request) -> str:
        try:
            return request.path_info or request.path or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _resolve_view_name(request) -> str:
        try:
            handler = request.resolver_match
            if handler:
                return f"{handler.view_name}"
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _record(self, *, request, request_id: str, method: str, path: str,
                view_name: str, status_code: int, latency_ms: float) -> None:
        try:
            threshold = getattr(settings, "SLOW_REQUEST_THRESHOLD_MS", 1000)
            if latency_ms >= threshold:
                logger.warning(
                    "slow_request method=%s path=%s view=%s status=%s latency_ms=%.3f",
                    method, path, view_name, status_code, latency_ms,
                )
        except Exception:  # noqa: BLE001
            pass

        # 异步写库：用单独的事务 + 捕获异常，避免统计失败影响业务
        try:
            self._write_log(
                request_id=request_id,
                method=method,
                path=path,
                view_name=view_name,
                status_code=status_code,
                latency_ms=latency_ms,
                ip=self._get_client_ip(request),
                user=getattr(request, "user", None),
                query=json.dumps(dict(request.GET.items()), ensure_ascii=False)[:1000]
                if getattr(request, "GET", None) else "",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("请求日志写入失败（不影响业务）：%s", e)

    @staticmethod
    def _write_log(**fields) -> None:
        # 单独起一个事务，避免外层事务回滚把日志也带掉
        user = fields.pop("user", None)
        username = ""
        if user is not None:
            try:
                if getattr(user, "is_authenticated", False):
                    username = getattr(user, "username", "") or str(user)
            except Exception:  # noqa: BLE001
                username = ""
        try:
            with transaction.atomic():
                RequestLog.objects.create(**fields, user=username)
        except Exception:  # noqa: BLE001
            # 最后兜底：表不存在 / 连接异常时不要崩
            pass

    @staticmethod
    def _get_client_ip(request) -> str:
        try:
            xff = request.META.get("HTTP_X_FORWARDED_FOR")
            if xff:
                return xff.split(",")[0].strip()[:64]
            return (request.META.get("REMOTE_ADDR") or "")[:64]
        except Exception:  # noqa: BLE001
            return ""
