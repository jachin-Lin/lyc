"""
总路由
"""
from django.contrib import admin
from django.urls import include, path

from dbms.api import urls as api_urls

admin.autodiscover()

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_urls)),
    path("api/v1/health/", include("dbms.common.health_urls")),
]
