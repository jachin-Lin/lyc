"""
权限：占位，演示环境默认开放。
生产环境应接入 SSO / JWT / RBAC。
"""
from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsAuthenticatedOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = getattr(request, "user", None)
        if user is None:
            return False
        return bool(getattr(user, "is_authenticated", False))
