"""
数据库实例管理系统 —— Django 配置

覆盖：
- 多数据库引擎（SQLite / MySQL / PostgreSQL），通过 DATABASE_URL 切换
- Celery（broker + beat 定时任务：每 12 小时密码轮换、每天 00:00 聚合统计）
- Fernet 对称加密（密码密文落库）
- 全局分页 / 过滤 / 限流
- 耗时统计中间件
"""
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

from dotenv import load_dotenv

load_dotenv()

import sys


BASE_DIR = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------
# 密钥
# ------------------------------------------------------------------
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-dev-key-change-me-now-please-1234567890")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]


# ------------------------------------------------------------------
# 应用
# ------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
PROJECT_APPS = [
    "dbms.common",
    "dbms.db_instances",
    "dbms.passwords",
    "dbms.stats",
    "dbms.api",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "django_filters",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + PROJECT_APPS

ROOT_URLCONF = "dbms.urls"

WSGI_APPLICATION = "dbms.wsgi.application"
ASGI_APPLICATION = "dbms.asgi.application"


# ------------------------------------------------------------------
# 数据库（DATABASE_URL）
#   sqlite:///db.sqlite3
#   mysql://user:pass@host:3306/dbname?charset=utf8mb4
#   postgres://user:pass@host:5432/dbname
# ------------------------------------------------------------------
def _parse_database_url(url: str) -> dict:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path == ":memory:":
            return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / path}
    parsed = urlparse(url)
    engine = {
        "mysql": "django.db.backends.mysql",
        "postgres": "django.db.backends.postgresql",
        "postgresql": "django.db.backends.postgresql",
    }[parsed.scheme]
    options = dict(parse_qsl(parsed.query))
    conf = {
        "ENGINE": engine,
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "localhost",
        "PORT": str(parsed.port) if parsed.port else "",
    }
    if engine == "django.db.backends.mysql":
        conf.setdefault("OPTIONS", {})
        conf["OPTIONS"].update({"charset": options.pop("charset", "utf8mb4")})
    if options:
        conf.setdefault("OPTIONS", {}).update(options)
    return conf


DATABASES = {
    "default": _parse_database_url(
        os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
    )
}


# ------------------------------------------------------------------
# 密码校验器（本系统密码由系统生成，这里只做 Django 后台账号的最低校验）
# ------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]


# ------------------------------------------------------------------
# 国际化 / 时区
# ------------------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True


# ------------------------------------------------------------------
# 静态资源
# ------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 模板（admin 必需）
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ------------------------------------------------------------------
# DRF
# ------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_PAGINATION_CLASS": "dbms.common.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "dbms.common.renderers.StandardJSONRenderer",
    ],
    "EXCEPTION_HANDLER": "dbms.common.exceptions.custom_exception_handler",
}


# ------------------------------------------------------------------
# 中间件（耗时统计放在最外层）
# ------------------------------------------------------------------
MIDDLEWARE = [
    "dbms.common.middleware.RequestTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# 必须在导入项目模块前完成，避免 celery 配置被提前缓存
load_dotenv(override=True)

SLOW_REQUEST_THRESHOLD_MS = int(os.environ.get("SLOW_REQUEST_THRESHOLD_MS", "1000"))


# ------------------------------------------------------------------
# Celery
# ------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "false").lower() in ("1", "true", "yes")
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# 防止同一任务上一次未跑完又触发一次
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_DEFAULT_QUEUE = "dbms"

CELERY_BEAT_SCHEDULE = {
    # 每 12 小时轮换一次全部实例密码
    "rotate-passwords-every-12h": {
        "task": "dbms.passwords.tasks.rotate_all_credentials",
        "schedule": 3600 * 12,
    },
    # 每天 00:00 按部门和集群维度统计实例数量并写入数据库
    "daily-instance-aggregate-at-midnight": {
        "task": "dbms.stats.tasks.daily_aggregate",
        "schedule": 3600 * 24,
        "kwargs": {"hour": 0, "minute": 0},
    },
}


# ------------------------------------------------------------------
# 密码轮换策略
# ------------------------------------------------------------------
PASSWORD_ROTATE_INTERVAL_HOURS = int(os.environ.get("PASSWORD_ROTATE_INTERVAL_HOURS", "12"))
PASSWORD_MIN_LENGTH = int(os.environ.get("PASSWORD_MIN_LENGTH", "24"))
PASSWORD_CHARSET = os.environ.get("PASSWORD_CHARSET", "alphanumeric")  # alphanumeric / ascii / hex
FERNET_KEY = os.environ.get("FERNET_KEY", "")
