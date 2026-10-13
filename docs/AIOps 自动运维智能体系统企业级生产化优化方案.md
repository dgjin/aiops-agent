# AIOps 自动运维智能体系统企业级生产化优化方案

> 版本：v1.0 | 日期：2026-10-03 | 定位：从高质量演示参考实现 → 企业级生产就绪系统
> 前置文档：《AIOps 自动运维智能体系统设计方案》《实施计划》《部署手册》
>
> 更新提示（2026-10-13）：本方案为四期基线设计文档，保留 2026-10-03 时点的评估与规划框架；其中指标端口与配置模板已同步为单机生产化整改（批次 A–I）后的现行口径（worker 指标 production 默认 9091 / demo 9090，均仅 loopback）；整改落实现状见《实施计划》第 13 节与《设计说明书》10.10。

---

## 目录

- [一、现状评估与企业级差距分析](#一现状评估与企业级差距分析)
- [二、生产/演示双模架构设计](#二生产演示双模架构设计)
- [三、企业级能力补齐方案](#三企业级能力补齐方案)
- [四、分阶段开发计划](#四分阶段开发计划)
- [五、风险与依赖](#五风险与依赖)
- [六、验收标准](#六验收标准)

---

## 一、现状评估与企业级差距分析

### 1.1 当前系统能力基线

| 维度 | 现状 | 评分 |
|------|------|------|
| 架构分层 | 五层架构（感知/分析/决策/验证/执行）+ 三道闸门，Temporal 编排 | ★★★★★ |
| 代码质量 | 7,959 行核心代码，pydantic 强校验，安全侧降级全覆盖 | ★★★★☆ |
| 测试覆盖 | 305 单测全绿，含真实容器集成测试 | ★★★★★ |
| 安全防御 | RBAC 三角色、fail-closed、SAST、沙箱隔离、令牌轮换 | ★★★★☆ |
| 可观测性 | 结构化日志 + 审计 JSONL + 操作留痕 | ★★★☆☆ |
| 高可用 | 单点部署，无 HA/容灾 | ★★☆☆☆ |
| 数据持久化 | 文件系统 JSONL/JSON，无数据库 | ★★☆☆☆ |
| 配置管理 | YAML 启动快照，无配置中心 | ★★★☆☆ |
| 部署形态 | docker-compose 单机，无 K8s 编排 | ★★☆☆☆ |
| 多租户 | 单租户，无隔离 | ★☆☆☆☆ |

### 1.2 企业级差距清单（按优先级）

#### P0 — 阻断生产上线

| 编号 | 差距 | 影响 | 当前状态 |
|------|------|------|----------|
| GAP-01 | **无数据库**：审计/令牌/被监控应用清单全部走文件系统 | 并发写入损坏、无法查询聚合、无备份恢复 | `data/*.json` / `data/web-audit/*.jsonl` |
| GAP-02 | **无高可用**：worker/BFF/webhook 单进程，Temporal 单节点 | 任一进程崩溃即服务中断 | `docker-compose.yml` 单容器 |
| GAP-03 | **无 K8s 部署形态**：仅 docker-compose，无法纳入企业容器平台 | 无法利用 K8s 调度/扩缩容/自愈 | 仅 `docker-compose.yml` |
| GAP-04 | **无 HTTPS/TLS**：BFF 明文 HTTP，令牌可被抓包 | 凭证泄露风险 | `uvicorn --host 127.0.0.1` |
| GAP-05 | **无速率限制**：BFF 无 rate limiting，可被暴力破解/DoS | 安全风险 | 无 |
| GAP-06 | **演示桩与生产代码混杂**：`fix_agent._DEMO_STUB_MARKERS` 等演示分支硬编码在生产代码路径中 | 生产环境可能误触发演示逻辑 | `fix_agent.py:56` |

#### P1 — 企业级必要能力

| 编号 | 差距 | 影响 | 当前状态 |
|------|------|------|----------|
| GAP-07 | **无 Prometheus 指标暴露**：系统自身无 `/metrics` | 无法纳入企业监控体系 | 仅日志 |
| GAP-08 | **无分布式追踪**：无 OTel trace 贯穿 worker→activity→外部调用 | 排障困难 | 仅 trace_id 日志提取 |
| GAP-09 | **无结构化审计数据库**：审计走 JSONL 文件，无法高效查询/合规报表 | 合规审计困难 | `data/web-audit/*.jsonl` |
| GAP-10 | **无 kill switch**：设计方案提及紧急停止，代码未实现 | 无法紧急止血 | 未实现 |
| GAP-11 | **无数据保留策略**：`data/sandbox/`、`data/notify/`、`data/web-audit/` 无 TTL 清理 | 磁盘膨胀 | 无清理 |
| GAP-12 | **无密钥管理**：LLM token / webhook secret 走环境变量明文 | 密钥泄露风险 | `.env` 文件 |
| GAP-13 | **SAST 过简**：正则黑名单，误报/漏报率高 | 安全扫描不可靠 | `sandbox.py:48-57` |
| GAP-14 | **无 MR 真实创建**：`create_merge_request` 仍为桩 | 无法对接真实 Git 流程 | `activities.py` 桩 |

#### P2 — 规模化与体验

| 编号 | 差距 | 影响 | 当前状态 |
|------|------|------|----------|
| GAP-15 | **bff/app.py 过长**：907 行单文件，职责混杂 | 可维护性下降 | 单文件 |
| GAP-16 | **无多租户/命名空间隔离**：多团队共用一套系统时无法隔离 | 权限边界模糊 | 单租户 |
| GAP-17 | **无 SSE/WebSocket 实时推送**：前端轮询，体验差 | 实时性不足 | 轮询 |
| GAP-18 | **无工作流搜索/过滤**：控制台无法按服务/时间/状态过滤 | 运维效率低 | 无 |
| GAP-19 | **无告警抑制/静默**：重复告警无法抑制 | 告警风暴 | 无 |
| GAP-20 | **demo-app/tests 缺 `__init__.py`**：沙箱容器内 `unittest discover` 报 ImportError | 演示流程在沙箱环节失败 | 已知缺陷 |

---

## 二、生产/演示双模架构设计

### 2.1 核心原则

1. **单一代码库，双模运行**：同一套代码通过配置切换生产/演示模式，不维护两套分支
2. **演示完全隔离**：演示数据、演示流程、演示依赖与生产零干扰
3. **生产默认安全**：未显式配置时，系统行为偏向安全侧（fail-closed）
4. **演示一键启动**：保留现有 `demo-up.sh` 体验，演示环境不影响生产

### 2.2 模式切换机制

```
┌─────────────────────────────────────────────────────────┐
│                    AIOPS_MODE 环境变量                    │
│                                                         │
│   production（默认）          demo                      │
│   ┌─────────────────┐        ┌─────────────────┐        │
│   │ MySQL 8.0       │        │ SQLite/文件      │        │
│   │ Redis 缓存       │        │ 内存缓存         │        │
│   │ K8s 沙箱        │        │ Docker 沙箱      │        │
│   │ ArgoCD 发布     │        │ Docker 滚动      │        │
│   │ 飞书/钉钉通知   │        │ 留痕文件         │        │
│   │ 真实 LLM        │        │ 确定性桩         │        │
│   │ Prometheus 指标 │        │ 日志输出         │        │
│   │ OTel 追踪       │        │ 无追踪           │        │
│   │ 速率限制        │        │ 无限制           │        │
│   │ TLS 强制        │        │ HTTP 明文        │        │
│   └─────────────────┘        └─────────────────┘        │
└─────────────────────────────────────────────────────────┘
```

### 2.3 模式判定与隔离边界

```python
# aiops_agent/mode.py（新增）
AIOPS_MODE = os.environ.get("AIOPS_MODE", "production").strip().lower()
IS_DEMO = AIOPS_MODE == "demo"
IS_PRODUCTION = not IS_DEMO

# 演示模式下的数据目录隔离
DATA_ROOT = Path(os.environ.get("AIOPS_DATA_ROOT", "")) or (
    BASE_DIR / "data" / "demo" if IS_DEMO else BASE_DIR / "data"
)
```

**隔离边界**：

| 资源 | 生产模式 | 演示模式 |
|------|----------|----------|
| 数据目录 | `data/` | `data/demo/` |
| Temporal 命名空间 | `aiops-production` | `aiops-demo` |
| 策略文件 | `release-gate-policy.yaml` | `demo-policy.yaml` |
| 被监控应用清单 | `data/monitored_apps.json` | `data/demo/monitored_apps.json` |
| 沙箱工作区 | `data/sandbox/` | `data/demo/sandbox/` |
| 通知留痕 | 真实 webhook | `data/demo/notify/` |
| 演示桩标记 | **禁用**（生产环境忽略 `_DEMO_STUB_MARKERS`） | 启用 |
| 日志标签 | `service=<真实服务>` | `service=demo-<服务>` |

### 2.4 演示桩的安全隔离

```
当前问题：fix_agent.py 中 _DEMO_STUB_MARKERS 硬编码在生产代码路径
解决方案：演示桩逻辑抽离为独立模块，仅在 demo 模式下注册

aiops_agent/
├── mode.py              # 模式判定（新增）
├── demo_stubs.py        # 演示桩实现（新增，从 fix_agent/activities 抽离）
└── fix_agent.py         # 移除 _DEMO_STUB_MARKERS，改为调用 demo_stubs
```

```python
# fix_agent.py 改造后
def run_fix(...):
    from . import mode
    if mode.IS_DEMO:
        from . import demo_stubs
        stub = demo_stubs.maybe_stub_fix(alert, root_cause, attempt, ...)
        if stub is not None:
            return stub
    # 生产路径：真实 LLM / Qoder 修复
    ...
```

---

## 三、企业级能力补齐方案

### 3.1 数据持久化层（GAP-01 / GAP-09）

**选型**：MySQL 8.0（生产，InnoDB 引擎）/ SQLite（演示）

**驱动选型**：`aiomysql`（异步）+ `PyMySQL`（同步，Alembic 迁移用）

**Schema 设计**：

```sql
-- 审计事件表
CREATE TABLE audit_events (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    actor       VARCHAR(128) NOT NULL,        -- 操作者（web-console / webhook / system）
    action      VARCHAR(64) NOT NULL,         -- approval / deploy-command / queue-patch / ...
    workflow_id VARCHAR(255) NOT NULL,
    stage       VARCHAR(32) NOT NULL,
    detail      JSON NOT NULL,                -- 变更前后值、审批意见等
    mode        VARCHAR(16) NOT NULL DEFAULT 'production',  -- production / demo
    INDEX idx_audit_wf (workflow_id),
    INDEX idx_audit_ts (ts),
    INDEX idx_audit_actor (actor)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 被监控应用表
CREATE TABLE monitored_apps (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(128) NOT NULL UNIQUE,
    base_url    VARCHAR(512) NOT NULL,
    log_path    VARCHAR(512),
    loki_service VARCHAR(128) NOT NULL,
    code_index_path VARCHAR(512),
    enabled     TINYINT(1) NOT NULL DEFAULT 1,
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 令牌注册表（替代 data/console_tokens.json）
CREATE TABLE console_tokens (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    token_hash  VARCHAR(64) NOT NULL UNIQUE,  -- sha256(token)
    user_name   VARCHAR(128) NOT NULL,
    role        ENUM('viewer','operator','admin') NOT NULL,
    status      ENUM('active','previous','revoked') NOT NULL DEFAULT 'active',
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at  DATETIME(6),
    rotated_at  DATETIME(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 工作流执行记录（补充 Temporal 历史，供控制台快速查询）
CREATE TABLE workflow_runs (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    workflow_id VARCHAR(255) NOT NULL UNIQUE,
    service     VARCHAR(128) NOT NULL,
    alert_id    VARCHAR(128) NOT NULL,
    stage       VARCHAR(32) NOT NULL,
    confidence  FLOAT,
    patch_id    VARCHAR(128),
    version     VARCHAR(64),
    started_at  DATETIME(6) NOT NULL,
    finished_at DATETIME(6),
    duration_s  FLOAT,
    result      JSON,
    mode        VARCHAR(16) NOT NULL DEFAULT 'production',
    INDEX idx_runs_service (service),
    INDEX idx_runs_stage (stage),
    INDEX idx_runs_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**MySQL 与 PostgreSQL 的关键差异处理**：

| 特性 | PostgreSQL | MySQL 8.0 | 适配方式 |
|------|-----------|-----------|----------|
| JSON 类型 | `JSONB`（二进制，支持索引） | `JSON`（文本，支持虚拟列索引） | 用 `JSON` 类型；需索引的字段提取为虚拟列 |
| 自增主键 | `BIGSERIAL` | `BIGINT AUTO_INCREMENT` | DDL 语法替换 |
| 时间类型 | `TIMESTAMPTZ`（带时区） | `DATETIME(6)`（微秒精度，时区由应用层处理） | 应用层统一 UTC 写入 |
| 枚举 | `CHECK` 约束 | `ENUM` 类型 | MySQL 原生 `ENUM` 更简洁 |
| 布尔 | `BOOLEAN` | `TINYINT(1)` | SQLAlchemy 自动映射 |
| 文本 | `TEXT`（无限长） | `VARCHAR(n)` / `TEXT` | 按实际长度选 `VARCHAR`，超长用 `TEXT` |
| `now()` | `now()` | `CURRENT_TIMESTAMP(6)` | DDL 语法替换 |
| 更新时间 | 需触发器 | `ON UPDATE CURRENT_TIMESTAMP(6)` | MySQL 原生支持 |

**ORM 选型**：SQLAlchemy 2.0（异步，`aiomysql` 驱动）+ Alembic（迁移）

**连接池配置**：
```python
# aiops_agent/db.py
engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,           # 生产：每 worker 10 连接
    max_overflow=20,        # 突发流量额外 20 连接
    pool_recycle=3600,      # MySQL wait_timeout 默认 28800s，1h 回收防断连
    pool_pre_ping=True,     # 每次取连接前 ping，防 MySQL 断连后使用死连接
    echo=False,
)
```

**改动点**：
- `bff/audit.py` → 写 MySQL（生产）/ JSONL（演示降级）
- `bff/auth.py` → 令牌注册表读 MySQL
- `bff/monitored_apps.py` → CRUD 走 MySQL
- `bff/aggregator.py` → 工作流列表从 `workflow_runs` 表查询（替代 Temporal list 全量扫描）

### 3.2 高可用架构（GAP-02 / GAP-03）

**K8s 部署形态**：

```yaml
# deploy/k8s/
├── namespace.yaml           # aiops-system 命名空间
├── temporal/
│   ├── temporal-server.yaml    # Temporal 集群（3 节点，MySQL 后端）
│   └── temporal-ui.yaml
├── worker/
│   ├── deployment.yaml         # 2 副本，PodDisruptionBudget
│   ├── hpa.yaml               # 基于 CPU/自定义指标扩缩容
│   └── serviceaccount.yaml
├── bff/
│   ├── deployment.yaml         # 2 副本
│   ├── service.yaml            # ClusterIP
│   ├── ingress.yaml            # NGINX Ingress + TLS
│   └── hpa.yaml
├── webhook/
│   ├── deployment.yaml         # 2 副本
│   └── service.yaml
├── mysql/
│   ├── statefulset.yaml        # 或外部 RDS（MySQL 8.0）
│   ├── service.yaml
│   └── configmap.yaml          # my.cnf（innodb_buffer_pool_size 等调优）
├── redis/
│   ├── deployment.yaml         # 缓存 + 速率限制
│   └── service.yaml
└── networkpolicy.yaml          # default-deny + 白名单
```

**关键配置**：
- Worker：`replicas: 2`，Temporal 任务队列天然支持多 worker 竞争消费
- BFF：`replicas: 2`，无状态（会话走 Bearer Token，无服务端 session）
- Webhook：`replicas: 2`，Alertmanager 侧配置多个 webhook URL
- Temporal：生产集群模式（非 `start-dev`），后端 MySQL 8.0（Temporal 官方支持，需使用 `mysql8` SQL 插件；单库吞吐达约 1000 事件/s，超出时建议拆分至 Cassandra 或 Temporal Cloud）
- PDB：`minAvailable: 1`，滚动更新不中断服务

### 3.3 安全加固（GAP-04 / GAP-05 / GAP-12 / GAP-13）

**TLS 终止**：
- Ingress 层 TLS 终止（cert-manager 自动签发 Let's Encrypt）
- BFF 容器内仍走 HTTP（集群内 mTLS 可选，Istio/Linkerd）

**速率限制**：
- Redis + sliding window 算法
- 按 token 维度：读接口 100 req/min，写接口 20 req/min
- 按 IP 维度（未认证请求）：10 req/min
- 超限返回 429 + Retry-After

**密钥管理**：
- 生产：K8s Secret + External Secrets Operator（对接 Vault/AWS SM）
- 演示：`.env` 文件（`.gitignore` 排除）
- 代码层：`os.environ.get()` 不变，密钥注入方式由部署层决定

**SAST 增强**：
- 保留现有正则黑名单作为第一道快速扫描
- 新增 Bandit（Python AST 级 SAST）作为第二道深度扫描
- 沙箱内执行：`bandit -r /work --format json`
- 结果合并到 `TestReport.sast` 字段

### 3.4 可观测性（GAP-07 / GAP-08）

**Prometheus 指标**：

```python
# aiops_agent/metrics.py（新增）
from prometheus_client import Counter, Histogram, Gauge

# 工作流指标
workflow_started = Counter("aiops_workflow_started_total", "工作流启动数", ["service", "mode"])
workflow_completed = Counter("aiops_workflow_completed_total", "工作流完成数", ["service", "stage", "mode"])
workflow_duration = Histogram("aiops_workflow_duration_seconds", "工作流耗时", ["service", "mode"],
                              buckets=[10, 30, 60, 120, 300, 600])

# 修复指标
fix_attempts = Counter("aiops_fix_attempts_total", "修复尝试数", ["provider", "degraded", "mode"])
fix_duration = Histogram("aiops_fix_duration_seconds", "修复耗时", ["provider", "mode"])

# 沙箱指标
sandbox_runs = Counter("aiops_sandbox_runs_total", "沙箱执行数", ["passed", "mode"])
sandbox_duration = Histogram("aiops_sandbox_duration_seconds", "沙箱耗时", ["mode"])

# 发布指标
canary_deploys = Counter("aiops_canary_deploys_total", "金丝雀发布数", ["healthy", "mode"])
canary_error_rate = Histogram("aiops_canary_error_rate", "金丝雀错误率", ["mode"])

# BFF 指标
bff_requests = Counter("aiops_bff_requests_total", "BFF 请求数", ["method", "path", "status"])
bff_request_duration = Histogram("aiops_bff_request_duration_seconds", "BFF 请求耗时", ["method", "path"])

# 闸门指标
gate_events = Counter("aiops_gate_events_total", "闸门事件数", ["gate", "decision", "mode"])
```

**暴露方式**：
- Worker：`/metrics` 端点（prometheus_client 独立 HTTP 服务，端口 9091（production）/ 9090（demo），默认仅监听 loopback）
- BFF：FastAPI 中间件自动采集 + `/metrics` 端点
- K8s：ServiceMonitor / PodMonitor 自动发现

**OTel 追踪**：
- Worker：Temporal SDK 自带 OTel interceptor
- Activity：`opentelemetry-instrumentation` 自动埋点（urllib/subprocess）
- BFF：`opentelemetry-instrumentation-fastapi`
- 导出：OTLP → Jaeger/Tempo

### 3.5 Kill Switch（GAP-10）

**设计**：

```python
# aiops_agent/kill_switch.py（新增）
class KillSwitch:
    """紧急停止开关：激活后所有新工作流拒绝启动，进行中工作流在当前 activity 完成后暂停。"""

    def __init__(self, redis_client):
        self._key = "aiops:kill_switch"
        self._redis = redis_client

    async def is_active(self) -> bool:
        return await self._redis.get(self._key) == b"1"

    async def activate(self, reason: str, actor: str) -> None:
        await self._redis.set(self._key, "1")
        await self._redis.set(f"{self._key}:reason", reason)
        await self._redis.set(f"{self._key}:actor", actor)
        await self._redis.set(f"{self._key}:ts", datetime.now(timezone.utc).isoformat())

    async def deactivate(self, actor: str) -> None:
        await self._redis.delete(self._key, f"{self._key}:reason", f"{self._key}:actor", f"{self._key}:ts")
```

**集成点**：
- `alert_webhook.py`：启动 workflow 前检查 kill switch，激活则返回 503
- `workflows.py`：每个 activity 执行前检查（通过 `workflow.execute_activity` 的 `cancellation_type`）
- BFF：`POST /api/system/kill-switch`（admin 角色）+ `GET /api/system/kill-switch`（状态查询）
- 控制台：系统状态页显示 kill switch 状态，激活时全局横幅

### 3.6 数据保留与清理（GAP-11）

**策略**：

| 数据类型 | 生产保留期 | 演示保留期 | 清理方式 |
|----------|-----------|-----------|----------|
| 审计事件 | 365 天 | 30 天 | 定时任务 DELETE |
| 沙箱工作区 | 7 天 | 1 天 | 定时任务 rm -rf |
| 通知留痕 | 90 天 | 7 天 | 定时任务删除文件 |
| Qoder 工作区 | 7 天 | 1 天 | 定时任务 rm -rf |
| ArgoCD 留痕 | 90 天 | 7 天 | 定时任务删除文件 |
| 工作流执行记录 | 365 天 | 30 天 | 定时任务 DELETE |
| Temporal 历史 | 30 天（Temporal 自身 retention） | 7 天 | Temporal namespace 配置 |

**实现**：K8s CronJob（生产）/ `demo_cli.py cleanup`（演示）

### 3.7 MR 真实创建（GAP-14）

**设计**：

```python
# aiops_agent/git_integration.py（新增）
class MergeRequestCreator:
    """真实 MR 创建：支持 GitLab / GitHub / Gitee。"""

    def create(self, patch: Patch, repo_url: str, branch: str, token: str) -> dict:
        provider = self._detect_provider(repo_url)
        if provider == "gitlab":
            return self._create_gitlab_mr(patch, repo_url, branch, token)
        elif provider == "github":
            return self._create_github_pr(patch, repo_url, branch, token)
        raise ValueError(f"不支持的 Git 平台: {repo_url}")
```

**配置**：
```yaml
# 策略文件新增
git:
  repo_url: "https://gitlab.example.com/ops/demo-app"
  token_secret: "aiops-git-token"  # K8s Secret 名称
  default_branch: "main"
  mr_labels: ["aiops", "auto-fix"]
```

### 3.8 BFF 拆分（GAP-15）

```
bff/
├── app.py              # FastAPI 入口 + 中间件 + 启动/关闭钩子（~150 行）
├── deps.py             # 依赖注入（TemporalGateway / DB / Redis）（新增）
├── middleware.py       # 鉴权 / 速率限制 / 指标 / CORS（新增）
├── routes/
│   ├── __init__.py
│   ├── flows.py        # 流程看板 / 详情 / 结果（~200 行）
│   ├── approvals.py    # 审批中心 / 写操作（~150 行）
│   ├── window.py       # 发布窗口（~100 行）
│   ├── audit.py        # 审计回看（~80 行）
│   ├── system.py       # 系统状态 / kill switch / 配置（~150 行）
│   └── monitored.py    # 被监控应用 CRUD（~100 行）
├── services/
│   ├── __init__.py
│   ├── flow_service.py     # 流程聚合逻辑（从 aggregator.py 迁移）
│   └── approval_service.py # 审批业务逻辑
├── aggregator.py       # 保留：产物聚合（data/ 文件读取）
├── audit.py            # 改造：写 MySQL
├── auth.py             # 保留：RBAC + 令牌轮换
├── config_items.py     # 保留：配置项元数据
├── monitored_apps.py   # 改造：CRUD 走 MySQL
└── temporal_gateway.py # 保留：Temporal 网关
```

---

## 四、分阶段开发计划

### 阶段总览

```
Phase 1（2 周）：地基 —— 数据库 + 模式隔离 + 演示桩抽离
Phase 2（2 周）：安全 —— TLS + 速率限制 + 密钥管理 + SAST 增强
Phase 3（2 周）：可观测 —— Prometheus 指标 + OTel 追踪 + 审计数据库
Phase 4（2 周）：高可用 —— K8s 部署 + 多副本 + kill switch
Phase 5（1 周）：集成 —— MR 真实创建 + BFF 拆分 + 数据清理
Phase 6（1 周）：验收 —— 端到端测试 + 文档更新 + 性能压测
```

---

### Phase 1：地基（第 1-2 周）

**目标**：建立数据库层，实现生产/演示模式隔离，抽离演示桩

| 编号 | 任务 | 负责模块 | 预估工时 | 验收标准 |
|------|------|----------|----------|----------|
| P1-01 | 新增 `aiops_agent/mode.py`：模式判定 + 数据目录隔离 | `mode.py` | 0.5d | `AIOPS_MODE=demo` 时数据目录切换到 `data/demo/` |
| P1-02 | 新增 `aiops_agent/demo_stubs.py`：从 `fix_agent.py` / `activities.py` 抽离演示桩 | `demo_stubs.py` / `fix_agent.py` / `activities.py` | 1d | 生产模式下 `_DEMO_STUB_MARKERS` 不生效；演示模式行为不变 |
| P1-03 | 新增 `aiops_agent/db.py`：SQLAlchemy 引擎 + session 管理 + Alembic 迁移；requirements 新增 `sqlalchemy[asyncio]` / `alembic` / `aiomysql` / `pymysql` | `db.py` / `alembic/` / `requirements.txt` | 1d | `alembic upgrade head` 建表成功；演示模式自动用 SQLite |
| P1-04 | 改造 `bff/audit.py`：审计事件写 MySQL（生产）/ JSONL（演示降级） | `bff/audit.py` | 1d | 审计事件落库，控制台审计页从 DB 查询 |
| P1-05 | 改造 `bff/auth.py`：令牌注册表读 MySQL | `bff/auth.py` | 1d | 令牌 CRUD 走 DB，轮换逻辑不变，600 权限文件不再使用 |
| P1-06 | 改造 `bff/monitored_apps.py`：CRUD 走 MySQL | `bff/monitored_apps.py` | 0.5d | 被监控应用 CRUD 落库，热生效不变 |
| P1-07 | 新增 `workflow_runs` 表写入：workflow 完成时落库 | `workflows.py` / `activities.py` | 1d | 每次 workflow 完成后 `workflow_runs` 表有记录 |
| P1-08 | 修复 `demo-app/tests/__init__.py` 缺失 | `demo-app/tests/` | 0.5d | 沙箱容器内 `unittest discover` 不再报 ImportError |
| P1-09 | 单测：新增模式隔离 / DB 层 / 演示桩抽离的测试用例 | `tests/` | 1d | 全量测试通过，新增 ≥20 用例 |
| P1-10 | 更新 `.env.example`：新增 `AIOPS_MODE` / `AIOPS_DATABASE_URL` | `.env.example` | 0.5d | 文档与代码一致 |

**Phase 1 里程碑**：`AIOPS_MODE=demo bash scripts/demo-up.sh` 行为与改造前完全一致；`AIOPS_MODE=production` 时审计/令牌/被监控应用走 MySQL。

---

### Phase 2：安全（第 3-4 周）

| 编号 | 任务 | 负责模块 | 预估工时 | 验收标准 |
|------|------|----------|----------|----------|
| P2-01 | BFF 速率限制中间件：Redis sliding window | `bff/middleware.py` | 1d | 超限返回 429；按 token/IP 双维度 |
| P2-02 | TLS 配置：Ingress + cert-manager | `deploy/k8s/` | 1d | HTTPS 访问正常，HTTP 自动跳转 |
| P2-03 | 密钥管理：K8s Secret + External Secrets | `deploy/k8s/` | 1d | 敏感配置不落盘，从 Secret 注入 |
| P2-04 | SAST 增强：Bandit 集成到沙箱 | `sandbox.py` | 1d | 沙箱测试报告含 Bandit 结果 |
| P2-05 | 安全头：CORS / CSP / X-Frame-Options / HSTS | `bff/middleware.py` | 0.5d | 响应头包含安全头 |
| P2-06 | 输入校验加固：所有写接口 pydantic 校验 | `bff/routes/` | 0.5d | 非法输入返回 422 |
| P2-07 | 单测：速率限制 / 安全头 / 输入校验 | `tests/` | 1d | 全量测试通过 |
| P2-08 | 安全扫描：Trivy 容器镜像扫描 + pip-audit 依赖扫描 | CI | 0.5d | 无 Critical/High 漏洞 |

---

### Phase 3：可观测（第 5-6 周）

| 编号 | 任务 | 负责模块 | 预估工时 | 验收标准 |
|------|------|----------|----------|----------|
| P3-01 | 新增 `aiops_agent/metrics.py`：Prometheus 指标定义 | `metrics.py` | 1d | `/metrics` 端点暴露全部指标 |
| P3-02 | Worker 指标采集：activity 执行埋点 | `activities.py` / `worker.py` | 1d | 工作流/修复/沙箱/发布指标有数据 |
| P3-03 | BFF 指标采集中间件 | `bff/middleware.py` | 0.5d | BFF 请求指标有数据 |
| P3-04 | OTel 追踪：Worker + BFF 自动埋点 | `worker.py` / `bff/app.py` | 1d | Jaeger 可见完整 trace |
| P3-05 | Grafana Dashboard：AIOps 系统概览面板 | `deploy/grafana/` | 1d | 面板展示工作流成功率/耗时/闸门分布 |
| P3-06 | 告警规则：系统自身健康告警（Prometheus rules） | `deploy/prometheus/` | 0.5d | worker 下线 / BFF 错误率高时触发告警 |
| P3-07 | 审计报表：按时间/操作者/动作聚合查询 | `bff/routes/audit.py` | 1d | 控制台审计页支持聚合查询 |
| P3-08 | 单测：指标采集 / 审计查询 | `tests/` | 1d | 全量测试通过 |

---

### Phase 4：高可用（第 7-8 周）

| 编号 | 任务 | 负责模块 | 预估工时 | 验收标准 |
|------|------|----------|----------|----------|
| P4-01 | K8s manifests：namespace / deployment / service / ingress | `deploy/k8s/` | 2d | `kubectl apply -f deploy/k8s/` 一键部署 |
| P4-02 | Temporal 生产集群：MySQL 后端 + 3 节点 | `deploy/k8s/temporal/` | 1d | Temporal UI 可见集群状态 |
| P4-03 | Worker 多副本 + HPA | `deploy/k8s/worker/` | 0.5d | 2 副本运行，CPU >70% 自动扩容 |
| P4-04 | BFF 多副本 + PDB | `deploy/k8s/bff/` | 0.5d | 滚动更新不中断服务 |
| P4-05 | Kill switch 实现 + 控制台集成 | `kill_switch.py` / `bff/routes/system.py` / `web/` | 1.5d | 激活后新工作流拒绝启动，控制台显示状态 |
| P4-06 | NetworkPolicy：default-deny + 白名单 | `deploy/k8s/` | 0.5d | 非白名单 Pod 间通信被拒绝 |
| P4-07 | 数据清理 CronJob | `deploy/k8s/cronjob.yaml` | 0.5d | 过期数据自动清理 |
| P4-08 | 单测：kill switch / 多副本健康检查 | `tests/` | 1d | 全量测试通过 |
| P4-09 | 故障演练：kill -9 worker / 删除 Pod / 网络分区 | 手动 | 0.5d | 服务自动恢复，无数据丢失 |

---

### Phase 5：集成（第 9 周）

| 编号 | 任务 | 负责模块 | 预估工时 | 验收标准 |
|------|------|----------|----------|----------|
| P5-01 | MR 真实创建：GitLab/GitHub 集成 | `git_integration.py` / `activities.py` | 2d | 审批通过后自动创建 MR |
| P5-02 | BFF 拆分：按域拆分 routes | `bff/routes/` | 1.5d | `bff/app.py` ≤200 行，路由按域分离 |
| P5-03 | 数据清理：TTL 策略实现 | `aiops_agent/cleanup.py` | 1d | 过期沙箱/通知/审计数据自动清理 |
| P5-04 | 单测：MR 创建 / BFF 拆分 / 清理 | `tests/` | 1d | 全量测试通过 |

---

### Phase 6：验收（第 10 周）

| 编号 | 任务 | 负责模块 | 预估工时 | 验收标准 |
|------|------|----------|----------|----------|
| P6-01 | 端到端测试：生产模式全流程 | `tests/e2e/` | 1d | 告警→修复→审批→发布全链路通过 |
| P6-02 | 端到端测试：演示模式全流程回归 | `tests/e2e/` | 0.5d | `demo-up.sh` 行为与改造前一致 |
| P6-03 | 性能压测：BFF 1000 req/s，worker 并发 10 workflow | 手动 | 1d | P99 <200ms，无内存泄漏 |
| P6-04 | 文档更新：部署手册 / 用户操作手册 / 设计说明书 | `docs/` | 1d | 文档与代码一致 |
| P6-05 | 安全渗透测试：OWASP ZAP / 手动渗透 | 手动 | 0.5d | 无 Critical/High 漏洞 |
| P6-06 | 代码评审：全量 CodeReview | CodeReview | 0.5d | 无 Critical/Warning |

---

### 里程碑时间线

```
Week 1-2   ████████ Phase 1：地基
Week 3-4   ████████ Phase 2：安全
Week 5-6   ████████ Phase 3：可观测
Week 7-8   ████████ Phase 4：高可用
Week 9     ████     Phase 5：集成
Week 10    ████     Phase 6：验收
           ──────────────────────────►
           M1        M2        M3
           DB就绪    安全就绪   生产就绪
```

---

## 五、风险与依赖

### 5.1 技术风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| Temporal 生产集群运维复杂度高 | 中 | 高 | 提供 Helm Chart + 运维手册；考虑 Temporal Cloud 托管 |
| MySQL 迁移导致数据丢失 | 低 | 高 | Alembic 迁移 + 迁移前 mysqldump 全量备份 + 回滚方案 |
| MySQL `wait_timeout` 断连导致长时 workflow 中断 | 中 | 中 | 连接池 `pool_recycle=3600` + `pool_pre_ping=True`；MySQL 侧调大 `wait_timeout` |
| 演示桩抽离引入回归 | 中 | 中 | 抽离前后各跑一次全量测试 + E2E 演示回归 |
| K8s 沙箱（K8sJobSandboxRunner）未在真实 K8s 验证 | 中 | 中 | Phase 4 在真实 K8s 集群验证 |
| Bandit 误报率高导致正常补丁被拦截 | 中 | 低 | Bandit 结果仅作为警告，不阻断流程（与 SAST 黑名单分级） |

### 5.2 外部依赖

| 依赖 | 用途 | 可用性要求 |
|------|------|-----------|
| MySQL 8.0 | 主数据库（InnoDB） | 生产环境需要（RDS 或自建） |
| Redis 7 | 缓存 + 速率限制 + kill switch | 生产环境需要 |
| Temporal Server | 工作流编排 | 生产集群（3 节点）或 Temporal Cloud |
| Prometheus | 指标采集 | 企业监控体系已有 |
| Jaeger/Tempo | 分布式追踪 | 可选 |
| GitLab/GitHub | MR 创建 | 企业 Git 平台 |
| K8s 1.28+ | 容器编排 | 生产环境需要 |
| cert-manager | TLS 证书自动签发 | K8s 环境 |

### 5.3 团队依赖

| 角色 | 职责 | 投入 |
|------|------|------|
| 后端开发 | Phase 1-5 全部开发任务 | 1 人 × 10 周 |
| 运维/SRE | K8s 集群准备 / Temporal 部署 / 监控接入 | 0.5 人 × 4 周（Phase 2/4） |
| 安全工程师 | 渗透测试 / 安全评审 | 0.5 人 × 1 周（Phase 6） |
| 前端开发 | 控制台 kill switch / 审计报表页面 | 0.5 人 × 2 周（Phase 3/4） |

---

## 六、验收标准

### 6.1 功能验收

- [ ] `AIOPS_MODE=demo bash scripts/demo-up.sh` 一键启动，行为与改造前完全一致
- [ ] `AIOPS_MODE=production kubectl apply -f deploy/k8s/` 一键部署到 K8s
- [ ] 生产模式下审计/令牌/被监控应用走 MySQL，不再依赖文件系统
- [ ] 演示桩标记（low-conf/protected/test-fail 等）在生产模式下不生效
- [ ] Kill switch 激活后新工作流拒绝启动，进行中工作流安全暂停
- [ ] MR 真实创建（GitLab/GitHub）在审批通过后自动触发
- [ ] BFF 速率限制生效，超限返回 429

### 6.2 非功能验收

- [ ] BFF P99 延迟 <200ms（1000 req/s 压测）
- [ ] Worker 支持 10 并发 workflow 无内存泄漏
- [ ] 全量单测通过（≥350 用例）
- [ ] 容器镜像无 Critical/High 漏洞（Trivy）
- [ ] 依赖无 Critical/High 漏洞（pip-audit）
- [ ] HTTPS 强制，HTTP 自动跳转
- [ ] Prometheus 指标覆盖：工作流/修复/沙箱/发布/闸门/BFF
- [ ] Grafana Dashboard 可视化系统健康状态

### 6.3 文档验收

- [ ] 《部署手册》更新：K8s 部署 / TLS / 密钥管理 / 故障恢复
- [ ] 《用户操作手册》更新：kill switch 操作 / 审计查询 / 多环境切换
- [ ] 《设计说明书》更新：双模架构 / 数据库 schema / 高可用设计
- [ ] `.env.example` 更新：新增全部环境变量说明

---

## 附录 A：新增文件清单

```
aiops-agent/
├── aiops_agent/
│   ├── mode.py                  # 模式判定（新增）
│   ├── demo_stubs.py            # 演示桩抽离（新增）
│   ├── db.py                    # 数据库引擎 + session（新增）
│   ├── metrics.py               # Prometheus 指标（新增）
│   ├── kill_switch.py           # 紧急停止开关（新增）
│   ├── git_integration.py       # MR 真实创建（新增）
│   └── cleanup.py               # 数据清理 TTL（新增）
├── alembic/
│   ├── alembic.ini
│   ├── env.py
│   └── versions/
│       └── 001_initial.py       # 初始 schema
├── bff/
│   ├── deps.py                  # 依赖注入（新增）
│   ├── middleware.py            # 鉴权/速率限制/指标/CORS（新增）
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── flows.py
│   │   ├── approvals.py
│   │   ├── window.py
│   │   ├── audit.py
│   │   ├── system.py
│   │   └── monitored.py
│   └── services/
│       ├── __init__.py
│       ├── flow_service.py
│       └── approval_service.py
├── deploy/
│   ├── k8s/
│   │   ├── namespace.yaml
│   │   ├── temporal/
│   │   ├── worker/
│   │   ├── bff/
│   │   ├── webhook/
│   │   ├── mysql/
│   │   ├── redis/
│   │   ├── cronjob.yaml
│   │   └── networkpolicy.yaml
│   ├── grafana/
│   │   └── aiops-dashboard.json
│   └── prometheus/
│       └── aiops-rules.yml
└── tests/
    ├── test_mode.py             # 模式隔离测试（新增）
    ├── test_db.py               # 数据库层测试（新增）
    ├── test_metrics.py          # 指标采集测试（新增）
    ├── test_kill_switch.py      # Kill switch 测试（新增）
    ├── test_cleanup.py          # 数据清理测试（新增）
    ├── test_git_integration.py  # MR 创建测试（新增）
    └── e2e/
        ├── test_production_e2e.py  # 生产模式 E2E（新增）
        └── test_demo_e2e.py        # 演示模式 E2E 回归（新增）
```

## 附录 B：环境变量新增

```bash
# ---------- 【必需】运行模式 ----------
# production（默认）/ demo
AIOPS_MODE=production

# ---------- 【必需】数据库 ----------
# MySQL 8.0 连接串（生产，aiomysql 异步驱动）；演示模式自动用 SQLite
AIOPS_DATABASE_URL=mysql+aiomysql://aiops:password@mysql:3306/aiops?charset=utf8mb4

# ---------- 【必需】Redis ----------
# 缓存 + 速率限制 + kill switch
AIOPS_REDIS_URL=redis://redis:6379/0

# ---------- 【生产】Git 集成 ----------
# MR 创建目标仓库
AIOPS_GIT_REPO_URL=https://gitlab.example.com/ops/demo-app
AIOPS_GIT_TOKEN_SECRET=aiops-git-token
AIOPS_GIT_DEFAULT_BRANCH=main

# ---------- 【生产】可观测 ----------
# Prometheus 指标端口（worker；production 默认 9091，demo 9090，仅 loopback，置 0 关闭）
AIOPS_METRICS_PORT=9091
# OTel 导出端点
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317

# ---------- 【生产】数据保留 ----------
# 审计保留天数（默认 365）
AIOPS_AUDIT_RETENTION_DAYS=365
# 沙箱工作区保留天数（默认 7）
AIOPS_SANDBOX_RETENTION_DAYS=7
```

## 附录 C：Python 依赖变更（requirements.txt 新增）

```
# ---------- 数据库（Phase 1） ----------
sqlalchemy[asyncio]>=2.0    # ORM（异步）
alembic>=1.13               # 数据库迁移
aiomysql>=0.2               # MySQL 异步驱动（生产）
pymysql>=1.1                # MySQL 同步驱动（Alembic 迁移用）+ cryptography 依赖
cryptography>=42            # PyMySQL 的 caching_sha2_password 认证依赖（MySQL 8 默认认证插件）

# ---------- 缓存/速率限制（Phase 2/4） ----------
redis>=5.0                  # 缓存 + 速率限制 + kill switch

# ---------- 可观测（Phase 3） ----------
prometheus-client>=0.20     # Prometheus 指标
opentelemetry-sdk>=1.25     # OTel 追踪
opentelemetry-instrumentation-fastapi>=0.46b0
opentelemetry-exporter-otlp>=1.25

# ---------- 测试（持续） ----------
aiosqlite>=0.20             # SQLite 异步驱动（演示模式 / 单测）
```

> 备注：`cryptography` 是 MySQL 8.0 默认认证插件 `caching_sha2_password` 的必要依赖，缺失时 PyMySQL/aiomysql 连接会报 `RuntimeError: 'cryptography' package is required for sha256_password or caching_sha2_password auth methods`。

---

> 本方案基于当前代码基线（305 单测全绿，7,959 行核心代码）制定，所有改动遵循「向后兼容、安全侧默认、演示不受影响」三大原则。每个 Phase 结束后均可独立验收，不影响后续阶段推进。
