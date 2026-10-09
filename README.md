# 数据库实例管理系统（Database Instance Management System）

企业内部的数据库实例、集群、部门管理后台，覆盖 **CRUD / 端口探测 / 密码定时轮换 / 聚合统计 / 请求耗时统计** 五个核心能力。

## 技术栈

| 层 | 选型 |
|---|---|
| Web 框架 | Django 5.2 + Django REST Framework 3.18 |
| 异步任务 | Celery 5.4 + Redis（broker & backend） |
| 持久化 | SQLite（开发/演示） / MySQL / PostgreSQL |
| 密码安全 | `cryptography.fernet`（AES-128-CBC + HMAC-SHA256），主密钥外置 |

## 一、功能与目录映射

```
dbms/
├── manage.py
├── requirements.txt
├── docker-compose.yml          # 一键起 redis + mysql
├── .env.example
└── dbms/
    ├── settings.py             # 配置：DB / Celery / 加密 / 中间件
    ├── urls.py                 # 总路由
    ├── celery.py               # Celery 应用（含 beat 定时任务）
    ├── wsgi.py / asgi.py
    ├── api/                    # 对外 API：版本、状态、文档聚合
    ├── db_instances/           # 领域模型 + CRUD + 端口探测
    │   ├── models.py           # 部门 / 集群 / 数据库实例（详细字段）
    │   ├── services.py         # TCP 端口探测、分页/过滤/排序
    │   ├── tasks.py            # 异步探测任务
    │   └── ...
    ├── passwords/              # 账号密码维护 + 定时轮换
    │   ├── models.py           # 密码历史（密文）
    │   ├── crypto.py           # Fernet 加解密
    │   ├── rotator.py          # 生成 / 校验 / 落库（原子提交）
    │   └── tasks.py            # 每 12 小时定时轮换
    ├── stats/                  # 聚合统计（部门 / 集群维度）
    │   ├── models.py           # 每日快照
    │   ├── services.py         # 聚合 SQL（defer / count）
    │   └── tasks.py            # 每日 00:00 统计
    ├── common/                 # 审计日志 / 耗时统计中间件 / 工具
    │   ├── middleware.py       # RequestTimingMiddleware
    │   └── models.py           # 请求日志表
    └── tests/                  # 单元测试（pytest + DRF APIClient）
```

## 二、领域模型

### 1. 部门 `Department`
| 字段 | 说明 |
|---|---|
| `name`（唯一） | 部门名称 |
| `code`（唯一） | 编码，用于外部系统对接 |
| `parent` | 自引用 FK，支持多级层级 |
| `manager_name / manager_email / manager_phone` | 负责人信息 |
| `cost_center` | 成本中心 |
| `description` | 备注 |
| `is_active` | 软删除位 |
| `created_at / updated_at` | 审计时间 |

### 2. 集群 `Cluster`
| 字段 | 说明 |
|---|---|
| `name`（唯一） | 集群名 |
| `department` | FK → 部门 |
| `db_type` | 枚举：`mysql / postgresql / redis / mongodb / tidb / other` |
| `version` | 主版本，如 `8.0` |
| `region / availability_zone` | 地域 / 可用区 |
| `vendor` | 云厂商：`self_hosted / aliyun / tencent / aws / huawei / other` |
| `topology` | 拓扑：`single / primary_replica / primary_multi_replica / cluster / sharded` |
| `node_count` | 节点数 |
| `max_connections` | 最大连接数 |
| `cpu_cores / memory_gb / disk_gb` | 规格 |
| `vip / vpc_id / subnet_id` | 网络信息 |
| `status` | 枚举：`planning / running / stopped / maintenance / decommissioned` |
| `launched_at` | 上线时间 |

### 3. 数据库实例 `DatabaseInstance`
| 字段 | 说明 |
|---|---|
| `identifier`（唯一） | 实例标识，如 `db-prod-order-01` |
| `name` | 显示名 |
| `cluster` / `department` | 所属集群、部门（冗余便于统计查询） |
| `db_type / version` | 数据库类型与版本 |
| `host / port` | 连接地址与端口 |
| `role` | 角色：`primary / replica / readonly / arbiter / config` |
| `environment` | 环境：`dev / test / staging / prod` |
| `status` | 状态：`creating / running / stopped / failed / deleting` |
| `engine_config` | JSON：字符集、时区、参数组等 |
| `spec` | JSON：CPU/内存/磁盘规格 |
| `backup_policy` | JSON：备份窗口、保留天数、是否开启 binlog |
| `owner / owner_group` | 业务负责人 |
| `monitor_url / ops_ticket` | 监控面板、工单号 |
| `tags` | JSON 标签 |
| `last_probe_at / last_probe_reachable` | 最近一次 TCP 探测结果 |
| `last_password_rotated_at` | 最近一次密码轮换时间 |
| `remark` | 备注 |

### 4. 密码 `DatabaseCredential` & `CredentialHistory`
- 每个 `(instance, username)` 组合只保留一条**当前凭据**，密码为 Fernet 密文。
- 每次轮换写入一条 `CredentialHistory`（含密文、轮换方式、校验结果、操作者），便于审计与回滚定位。
- 明文的获取必须显式调用 `decrypt()`，禁止在日志、序列化结果中输出。

## 三、快速启动

```bash
# 1. 准备环境
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 务必生成 FERNET_KEY

# 2. 起依赖（也可以自己装 redis / mysql）
docker compose up -d redis mysql

# 3. 建表 + 生成密钥 + 灌入演示数据
python manage.py migrate
python manage.py shell -c "from passwords.crypto import ensure_fernet_key; print(ensure_fernet_key())"
python manage.py create_demo_data        # 生成 2 个部门 / 2 个集群 / 6 个实例 + 凭据

# 4. 跑服务
python manage.py runserver 0.0.0.0:8000
celery -A dbms worker -l info
celery -A dbms beat -l info
```

> 没有真实 Redis 时，可用 fakeredis 的 TCP 模式替代：
> `fakeredis-tcp --host 127.0.0.1 --port 6379` 或参考 `scripts/run_with_fakeredis.py`。
> 没有真实 MySQL 时，演示凭据的「真实连接校验」会自动跳过并标记 `skipped`，轮换流程的其他步骤照常执行，不会中断。

## 四、核心 API

所有资源接口统一前缀 `/api/v1/`。

### 4.1 增删改查（RESTful）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/departments/` | 列表，支持 `?search=&is_active=&parent=` |
| POST | `/api/v1/departments/` | 创建 |
| GET/PUT/PATCH/DELETE | `/api/v1/departments/{id}/` | 详情 / 全量改 / 局部改 / 删除 |
| GET | `/api/v1/clusters/` | 列表，支持 `?db_type=&status=&department=&search=` |
| POST | `/api/v1/clusters/` | 创建 |
| GET/PUT/PATCH/DELETE | `/api/v1/clusters/{id}/` | 同上 |
| GET | `/api/v1/instances/` | 列表，支持 `?db_type=&environment=&status=&role=&cluster=&department=&is_reachable=&search=` |
| POST | `/api/v1/instances/` | 创建（自动为该实例创建一条 `root` 凭据） |
| GET/PUT/PATCH/DELETE | `/api/v1/instances/{id}/` | 同上 |
| GET | `/api/v1/instances/{id}/credentials/` | 列出某实例全部账号（**仅返回密文摘要，不含明文**） |
| POST | `/api/v1/instances/{id}/credentials/` | 新增 / 重置某账号密码 |
| GET | `/api/v1/credentials/{id}/plaintext/` | 解密该凭据明文（审计日志会记录） |

### 4.2 端口探测

- **同步探测单个**：`POST /api/v1/instances/{id}/probe/`，body 可选 `{"timeout": 3.0}`，返回 `{reachable, latency_ms, error}` 并回写实例的 `last_probe_at / last_probe_reachable`。
- **异步批量探测**：`POST /api/v1/instances/probe_batch/`，body `{"ids": [...], "timeout": 3.0}`，立即返回 `task_id`，结果通过 `GET /api/v1/tasks/{task_id}/` 轮询。
- 实现：基于 `socket.create_connection(..., timeout)`，`SO_REUSEADDR`，单次探测连接，不发送应用层协议报文，因此**纯 TCP 握手可达**不等于数据库账号可登录，两者职责分离。

### 4.3 密码轮换

- 手动触发单实例：`POST /api/v1/instances/{id}/rotate_password/`，body `{"username": "root", "apply_to_database": true}`。
- 调度：Celery beat 每 12 小时（`PASSWORD_ROTATE_INTERVAL_HOURS`，默认 `12`，测试环境用 `1` 分钟演示）执行 `passwords.tasks.rotate_all_credentials`，遍历全部凭据调用 `rotator.rotate_credential`：
  1. 生成符合策略的随机密码；
  2. 若 `apply_to_database=True`，用旧凭据连接真实 DB 并执行 `ALTER USER ... IDENTIFIED BY ...`（MySQL）/ `ALTER USER ... WITH PASSWORD ...`（PostgreSQL），**校验成功后才更新凭据表**；
  3. 校验失败则保留旧密码，写入失败记录，不破坏可用性；
  4. 密文入库 + `CredentialHistory` 追加一条审计记录。
- 密钥：`Fernet.generate_key()` 生成 32 字节 base64 密钥，放在 `.env`，禁止提交到仓库。模型字段是密文，即使 SQL 注入拖库也无法直接还原。

### 4.4 聚合统计

- 快照模型 `DailyInstanceStat`：`stat_date / department / cluster / group_by`（维度） +  `count` + `by_db_type`（JSON）。
- 生成：`GET /api/v1/stats/generate/` 立即生成并返回最新快照；定时任务 `stats.tasks.daily_aggregate` 每天 `00:00`（见 `CELERY_BEAT_SCHEDULE`）写入当天快照。
- 查询：`GET /api/v1/stats/?stat_date=2026-10-09&group_by=department`，返回 `[{dimension, count, by_db_type}]`。
- 聚合实现：用 `queryset.values(*keys).annotate(count=Count("*"), by_db_type=JSONObject(...))`，`defer` 掉大字段，避免在 Python 层做 group by。

### 4.5 请求耗时统计

`common.middleware.RequestTimingMiddleware`：

- 在 `process_request` 记录 `time.monotonic()`，`process_response` / `process_exception` 计算耗时；
- 记录 `method / path / view / status_code / latency_ms / ip / user / request_id`；
- 异步写库（攒批 + 超时兜底），避免拖慢业务响应；
- 提供 `GET /api/v1/stats/request_timing/` 查看最近 N 条，支持 `?path=&method=&min_ms=&ordering=-latency_ms`；
- 慢请求自动 WARNING 日志，阈值 `SLOW_REQUEST_THRESHOLD_MS`（默认 1000ms）；
- 内置 `RequestTimingMiddlewareDisabled` 上下文管理器，用于压测或内部 healthcheck 时临时跳过。

## 五、测试

```bash
pytest -v                        # 全部
pytest tests/test_models.py -v   # 模型约束 / 软删除 / 自引用
pytest tests/test_crypto.py -v   # 加密解密、密钥轮转、坏数据
pytest tests/test_probe.py -v    # TCP 端口探测（真连 8000/22/随机关闭端口）
pytest tests/test_api.py -v      # 全量 CRUD + 聚合 + 耗时查询
pytest --cov=dbms --cov-report=term-missing
```

测试配置见 `pytest.ini`、`tests/conftest.py`（自动造 fixture、伪造 redis、自动加载 `.env`）。

## 六、安全与可运维性说明

1. **密码不落日志**：序列化器只输出密文摘要；`plaintext` 接口单独鉴权并记审计。
2. **密钥外置**：`.env` 加入 `.gitignore`；生产建议接入 KMS / Vault，用 `MultiFernet` 做密钥轮转。
3. **轮换不破坏可用性**：真实连接失败 = 旧密码保留 + 告警，绝不静默更新凭据表。
4. **幂等**：定时任务用 `expires` + `lock_timeout`，防止上一次未跑完时重复触发。
5. **统一响应 & 异常处理**：`common/renderers.py`、`common/exceptions.py` 给出 `{code, message, data, request_id}` 结构。
6. **只读 / 软删除**：`Department`、`Cluster` 均支持 `is_active` 软删除，`DatabaseInstance.delete()` 走 `deleting` 状态流转。
7. **分页与过滤**：全局 `PageNumberPagination`（默认 20，最大 200），列表接口支持 `search` 与多字段精确过滤。

## 七、提交方式

```bash
git init
git add .
git commit -m "feat: 数据库实例管理系统初始实现"
git remote add origin <你的仓库地址>
git push -u origin main
```

要求以 `git` 方式提交到 GitHub 或 Gitee，提交完成后把仓库地址发回即可。
