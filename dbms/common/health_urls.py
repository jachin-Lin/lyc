"""
健康检查：不依赖任何数据库查询，避免被中间件统计拖慢。
"""
from django.http import JsonResponse
from django.urls import path


def health_check(request):
    return JsonResponse({"status": "ok", "service": "dbms"})


urlpatterns = [
    path("", health_check, name="health-check"),
]
