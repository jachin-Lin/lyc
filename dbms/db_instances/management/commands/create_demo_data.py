"""
生成演示数据：2 个部门 / 2 个集群 / 6 个实例 + 凭据

python manage.py create_demo_data
"""
from django.core.management.base import BaseCommand

from dbms.db_instances.models import Cluster, DatabaseInstance, DBType, Department
from dbms.passwords.crypto import encrypt_password
from dbms.passwords.models import DatabaseCredential


class Command(BaseCommand):
    help = "创建演示数据：2 个部门 / 2 个集群 / 6 个实例 + 凭据"

    def handle(self, *args, **options):
        dept_a, _ = Department.objects.get_or_create(
            code="RD", defaults={"name": "研发部", "manager_name": "张三",
                                 "manager_email": "zhangsan@example.com"})
        dept_b, _ = Department.objects.get_or_create(
            code="FIN", defaults={"name": "财务部", "manager_name": "李四",
                                 "manager_email": "lisi@example.com"})

        cluster_a, _ = Cluster.objects.get_or_create(
            name="prod-mysql-cluster-01", defaults={
                "department": dept_a, "db_type": DBType.MYSQL, "version": "8.0",
                "region": "ap-guangzhou", "availability_zone": "ap-guangzhou-3",
                "topology": "primary_replica", "node_count": 3,
                "cpu_cores": 16, "memory_gb": 64, "disk_gb": 500,
                "vip": "10.0.1.100", "status": "running",
            })
        cluster_b, _ = Cluster.objects.get_or_create(
            name="prod-pg-cluster-01", defaults={
                "department": dept_b, "db_type": DBType.POSTGRESQL, "version": "15",
                "region": "ap-shanghai", "availability_zone": "ap-shanghai-2",
                "topology": "cluster", "node_count": 3,
                "cpu_cores": 8, "memory_gb": 32, "disk_gb": 200,
                "status": "running",
            })

        instances = [
            ("db-prod-order-01", "127.0.0.1", 3306, "primary", dept_a, cluster_a, DBType.MYSQL),
            ("db-prod-order-02", "127.0.0.1", 3307, "replica", dept_a, cluster_a, DBType.MYSQL),
            ("db-prod-user-01", "127.0.0.1", 3308, "primary", dept_a, cluster_a, DBType.MYSQL),
            ("db-prod-bill-01", "127.0.0.1", 5432, "primary", dept_b, cluster_b, DBType.POSTGRESQL),
            ("db-prod-bill-02", "127.0.0.1", 5433, "replica", dept_b, cluster_b, DBType.POSTGRESQL),
            ("db-prod-report-01", "127.0.0.1", 5434, "readonly", dept_b, cluster_b, DBType.POSTGRESQL),
        ]

        created = 0
        for ident, host, port, role, dept, cluster, db_type in instances:
            inst, is_new = DatabaseInstance.objects.get_or_create(
                identifier=ident, defaults={
                    "name": ident, "department": dept, "cluster": cluster,
                    "db_type": db_type, "version": cluster.version,
                    "host": host, "port": port, "role": role,
                    "environment": "prod", "status": "running",
                    "owner": "dba-team", "owner_group": dept.code,
                    "spec": {"cpu": 8, "memory_gb": 32},
                    "backup_policy": {"retention_days": 7, "binlog": True},
                })
            if is_new:
                created += 1
            if not DatabaseCredential.objects.filter(instance=inst).exists():
                DatabaseCredential.objects.create(
                    instance=inst, username="root",
                    password_ciphertext=encrypt_password("P@ssw0rd-Demo-Only-Change-Me!"),
                    is_active=True,
                )
        self.stdout.write(self.style.SUCCESS(
            f"演示数据就绪：{created} 个新实例，部门 {dept_a.code}/{dept_b.code}，"
            f"集群 {cluster_a.name}/{cluster_b.name}"))
