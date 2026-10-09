"""
TCP 端口探测服务

只做「TCP 三次握手能否完成」的探测，不涉及任何应用层协议，
因此 reachable=True 仅代表端口可达，不代表账号可登录。
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Optional

from dbms.db_instances.models import DatabaseInstance


@dataclass
class ProbeResult:
    host: str
    port: int
    reachable: bool
    latency_ms: float
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "reachable": self.reachable,
            "latency_ms": round(self.latency_ms, 3),
            "error": self.error,
        }


def probe_tcp(host: str, port: int, timeout: float = 3.0) -> ProbeResult:
    """
    探测指定主机的 TCP 端口是否可达。

    使用 SO_REUSEADDR 减少 TIME_WAIT，单次连接探测，不做应用层交互。
    """
    if not 1 <= port <= 65535:
        return ProbeResult(host=host, port=port, reachable=False, latency_ms=0.0,
                           error=f"端口超出合法范围：{port}")

    start = _monotonic_ms()
    sock = None
    try:
        # 先解析域名，避免 socket 在超时前就因 getaddrinfo 抛异常
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            return ProbeResult(host=host, port=port, reachable=False,
                               latency_ms=_monotonic_ms() - start,
                               error=f"DNS 解析失败：{e}")

        sock = socket.socket(infos[0][0], socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.connect((host, port))
        return ProbeResult(host=host, port=port, reachable=True,
                           latency_ms=_monotonic_ms() - start)
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return ProbeResult(host=host, port=port, reachable=False,
                           latency_ms=_monotonic_ms() - start,
                           error=f"{type(e).__name__}: {e}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def probe_instance(instance: DatabaseInstance, timeout: float = 3.0) -> ProbeResult:
    """探测单个实例并回写探测结果到模型。"""
    from django.utils import timezone

    result = probe_tcp(instance.host, instance.port, timeout)
    instance.last_probe_at = timezone.now()
    instance.last_probe_reachable = result.reachable
    # 只更新这两个字段，避免覆盖其他字段；不抛出任何异常
    instance.save(update_fields=["last_probe_at", "last_probe_reachable", "updated_at"])
    return result


def _monotonic_ms() -> float:
    import time
    return time.monotonic() * 1000.0
