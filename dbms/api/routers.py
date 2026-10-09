"""
统一 API 路由聚合

所有路由集中在此文件，避免各模块互相 import 形成环。
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from dbms.common.views import SystemStatusView, APIVersionView
from dbms.db_instances.views import (
    DepartmentViewSet, ClusterViewSet, DatabaseInstanceViewSet,
)
from dbms.passwords.views import (
    CredentialViewSet, CredentialPlaintextView, TaskStatusView,
)
from dbms.stats.views import (
    DailyStatListViewSet, DailyStatGenerateView, RequestTimingListViewSet,
)

router = DefaultRouter()
router.register(r"departments", DepartmentViewSet, basename="department")
router.register(r"clusters", ClusterViewSet, basename="cluster")
router.register(r"instances", DatabaseInstanceViewSet, basename="instance")
router.register(r"credentials", CredentialViewSet, basename="credential")

urlpatterns = [
    # @action 装饰的接口交给 router.urls（DefaultRouter 自动生成）
    *router.urls,
    # 需要额外绑定参数（instance_pk）或非标准动作的接口
    path(
        "instances/<int:instance_pk>/credentials/",
        CredentialViewSet.as_view({"get": "list", "post": "create"}),
        name="instance-credentials",
    ),
    path(
        "credentials/<int:pk>/plaintext/",
        CredentialPlaintextView.as_view({"get": "retrieve"}),
        name="credential-plaintext",
    ),
    path(
        "tasks/<str:pk>/",
        TaskStatusView.as_view({"get": "retrieve"}),
        name="task-status",
    ),
    path("stats/", DailyStatListViewSet.as_view({"get": "list"}), name="daily-stat-list"),
    path("stats/generate/", DailyStatGenerateView.as_view(), name="daily-stat-generate"),
    path(
        "stats/request_timing/",
        RequestTimingListViewSet.as_view({"get": "list"}),
        name="request-timing",
    ),
    path("status/", SystemStatusView.as_view(), name="system-status"),
    path("version/", APIVersionView.as_view(), name="api-version"),
]
