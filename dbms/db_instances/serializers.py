"""
序列化器：Department / Cluster / DatabaseInstance
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from dbms.db_instances.models import (
    Cluster, ClusterStatus, DatabaseInstance, DBType, Department,
    Environment, InstanceRole, InstanceStatus, Topology, Vendor,
)
from dbms.db_instances.services import probe_tcp
from dbms.passwords.models import DatabaseCredential


class DepartmentSerializer(serializers.ModelSerializer):
    children_count = serializers.SerializerMethodField()
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id", "name", "code", "parent", "parent_name",
            "manager_name", "manager_email", "manager_phone",
            "cost_center", "description", "is_active",
            "children_count", "full_name", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at", "children_count", "full_name"]

    parent_name = serializers.CharField(source="parent.name", read_only=True)

    def get_children_count(self, obj: Department) -> int:
        return obj.children.filter(is_deleted=False).count()

    def get_full_name(self, obj: Department) -> str:
        ancestors = obj.ancestors
        names = [a.name for a in ancestors] + [obj.name]
        return " / ".join(names)

    def validate_code(self, value: str) -> str:
        if not value.replace("_", "").replace("-", "").isalnum():
            raise serializers.ValidationError("编码只能包含字母、数字、下划线和连字符。")
        return value.upper()


class ClusterListSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    db_type_display = serializers.CharField(source="get_db_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    instance_count = serializers.SerializerMethodField()

    class Meta:
        model = Cluster
        fields = [
            "id", "name", "department", "department_name", "db_type", "db_type_display",
            "version", "region", "availability_zone", "vendor", "topology",
            "node_count", "max_connections", "cpu_cores", "memory_gb", "disk_gb",
            "vip", "vpc_id", "subnet_id", "status", "status_display",
            "is_active", "description", "launched_at",
            "instance_count", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at", "instance_count",
                            "department_name", "db_type_display", "status_display"]

    def get_instance_count(self, obj: Cluster) -> int:
        return obj.instances.filter(is_deleted=False).count()


class ClusterDetailSerializer(ClusterListSerializer):
    """详情：包含规格、拓扑等完整信息。"""
    pass


class DatabaseInstanceListSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)
    db_type_display = serializers.CharField(source="get_db_type_display", read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    environment_display = serializers.CharField(source="get_environment_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    credential_count = serializers.SerializerMethodField()
    endpoint = serializers.CharField(source="get_endpoint", read_only=True)

    class Meta:
        model = DatabaseInstance
        fields = [
            "id", "identifier", "name", "cluster", "cluster_name",
            "department", "department_name", "db_type", "db_type_display",
            "version", "host", "port", "endpoint", "role", "role_display",
            "environment", "environment_display", "status", "status_display",
            "owner", "owner_group", "tags",
            "last_probe_at", "last_probe_reachable",
            "last_password_rotated_at", "credential_count",
            "remark", "created_at", "updated_at",
        ]
        read_only_fields = [
            "created_at", "updated_at", "credential_count",
            "department_name", "cluster_name",
            "db_type_display", "role_display", "environment_display",
            "status_display", "last_probe_at", "last_probe_reachable",
            "last_password_rotated_at", "endpoint",
        ]

    def get_credential_count(self, obj: DatabaseInstance) -> int:
        return obj.credentials.filter(is_active=True).count()

    def get_endpoint(self, obj: DatabaseInstance) -> str:
        return f"{obj.host}:{obj.port}"


class DatabaseInstanceDetailSerializer(DatabaseInstanceListSerializer):
    """详情：返回完整规格、备份策略等。"""

    class Meta(DatabaseInstanceListSerializer.Meta):
        fields = DatabaseInstanceListSerializer.Meta.fields + [
            "engine_config", "spec", "backup_policy", "monitor_url",
            "ops_ticket", "is_deleted",
        ]


class InstanceProbeSerializer(serializers.Serializer):
    timeout = serializers.FloatField(default=3.0, min_value=0.1, max_value=30.0, required=False)


class InstanceBatchProbeSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=500,
    )
    timeout = serializers.FloatField(default=3.0, min_value=0.1, max_value=30.0, required=False)


class AsyncTaskStatusSerializer(serializers.Serializer):
    task_id = serializers.CharField()
    state = serializers.CharField()
    result = serializers.JSONField(required=False, allow_null=True)
    error = serializers.CharField(required=False, allow_null=True)


class CredentialCreateSerializer(serializers.ModelSerializer):
    """新增 / 重置凭据。密码留空则由系统随机生成。"""

    password = serializers.CharField(write_only=True, required=False, allow_blank=True,
                                     min_length=8, max_length=128)
    plaintext = serializers.SerializerMethodField()

    class Meta:
        model = DatabaseCredential
        fields = [
            "id", "instance", "username", "password",
            "is_active", "plaintext", "rotate_count",
            "last_rotated_at", "last_rotated_by",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "plaintext", "rotate_count",
                            "last_rotated_at", "last_rotated_by",
                            "created_at", "updated_at"]

    def get_plaintext(self, obj: DatabaseCredential) -> str:
        # 只在创建成功后的第一次返回明文，后续查询一律隐藏
        if getattr(obj, "_just_created", False):
            try:
                return obj.get_plaintext()
            except Exception:  # noqa: BLE001
                return ""
        return "***"

    def validate(self, attrs: dict) -> dict:
        if not attrs.get("password"):
            # 由 rotator 生成，这里只是占位
            attrs["password"] = None
        return attrs
