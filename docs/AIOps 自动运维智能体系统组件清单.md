# AIOps 自动运维智能体系统组件清单

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-10-14 | 首版：按运行边界八层盘点全部组件（常驻进程 / 核心引擎 / BFF / 前端 / 基础设施 / 外部依赖 / 被监控系统 / 工具链） |

> **口径说明**：本清单按「运行边界」分层，与《AIOps 自动运维智能体系统部署手册》§1.1「运行时组件清单」互为补充（手册侧重安装与启动顺序，本文侧重全量罗列与职责边界）。信息来源：部署手册 §1.1、根级 `docker-compose.yml` / `docker-compose.production.yml`、`deploy/` 目录、工程目录实测（2026-10-14）。
>
> **规模小结**：7 个自建常驻进程（6 组件 + 1 守护）、33 个核心引擎模块、14 个 BFF 功能模块 + 14 个路由模块、14 个控制台页面 + 23 个组件 + 3 个图表、8 类基础设施容器、6 类外部依赖。

---

## 1 系统常驻进程（6 组件 + 1 守护）

由 `./start.sh`（唯一启动入口）或 `bash scripts/demo-up.sh`（兼容薄壳）一键编排，启动顺序：worker → BFF → 告警接入 → 可用性巡检 → 日志采集 → 突增检测 → 组件守护。

| 组件 | 入口 | 默认端口 | 职责 | 关键依赖 |
| --- | --- | --- | --- | --- |
| Worker | `python -m aiops_agent.worker` | 指标 :9090 | Temporal Worker：承接 `AIOpsFixWorkflow` / `AIOpsRequirementWorkflow` 及全部 activity（triage、修复、沙箱、测试、发布、通知） | Temporal、Ollama、Docker、MySQL（production）/ data 目录（demo） |
| BFF 控制台 | `uvicorn bff.app:app` | :8600 | 控制台 API（会话+静态令牌双通道鉴权）、SPA 静态托管、IM 命令入口 `/api/im/command`、飞书卡片回调网关 `/api/im/card-callback` | Temporal、MySQL / data 产物、`web/dist` |
| 告警接入 | `python alert_webhook.py` | :8099 | 接收 Alertmanager webhook，按故障域聚合（production 走 `alert_store` 三表租约；demo 走进程内窗口），启动修复工作流 | Temporal、MySQL（production） |
| 可用性巡检 | `python app_prober.py` | — | 按被监控应用清单定时探测健康端点（支持 `probe_keyword` 页面内容校验） | 应用清单（热加载） |
| 日志采集 | `python ship_app_logs.py --follow` | — | 被监控应用日志增量采集，推送 Loki | Loki |
| 突增检测 | `python log_surge_detector.py` | — | 错误日志突增检测（Drain3 模板聚类 + 基线），命中后触发修复流程 | Loki、应用清单（热加载） |
| 组件守护 | `python scripts/aiops-watchdog.py` | — | 每 30 秒巡检 6 个常驻组件，崩溃自动拉起；单实例锁、档位锁定、RSS 内存水位守护；可经 launchd 托管登录自启 | 上述 6 组件 |

**按需进程（不常驻）**：

| 进程 | 入口 | 用途 |
| --- | --- | --- |
| MCP Server | `python mcp_server.py`（stdio 主模式 / `--http` 受限模式） | 对外暴露 MCP 工具面（查询/信号），production 拒绝无认证 HTTP |
| 演示 CLI | `python demo_cli.py` | 启停流程、审批、排队补丁、doctor 自检（7 项） |
| 需求分析 | `python analyze_requirements.py` | 需求拉取与 LLM 分析，产出修复请求 |
| 演示日志播种 | `python demo_log_generator.py` | demo 档演示日志生成 |

---

## 2 核心引擎模块（`aiops_agent/`，33 个功能模块）

| 分组 | 模块 | 职责 |
| --- | --- | --- |
| 编排 | `workflows` | `AIOpsFixWorkflow` 主流程状态机（TRIAGING→…→CANARY/ESCALATED）、三道闸门等待与信号处理；需求驱动 `AIOpsRequirementWorkflow` |
| 编排 | `activities` | 全部 activity 实现（triage、修复、沙箱、测试、发布、通知等，注册表与 `_ACTIVITY_LIST` 同步约束） |
| 编排 | `worker` | Worker 启动、任务队列注册、指标服务（:9090） |
| 配置 | `config` | 启动时加载 `.env` 与策略 YAML 只读快照（pydantic 校验） |
| 配置 | `mode` | 双档判定唯一来源（`AIOPS_MODE` → demo / production，缺省 production） |
| 模型 | `models` | Alert / RootCause / Patch / TestReport / RunResult 等数据模型 |
| 感知 | `triage` | LLM 根因分析（置信度闸门 < 0.8 转人工） |
| 感知 | `logs` | Loki 日志查询与新鲜度判定 |
| 感知 | `code_rag` | 代码库检索（FAISS / 纯 Python 双后端） |
| 感知 | `topology` | 服务拓扑解析（依赖关系上下文） |
| 感知 | `page_health` | 页面健康与 `probe_keyword` 校验 |
| 修复 | `fix_agent` | 修复 Agent 编排（模型选择、最小 diff 生成、评估） |
| 修复 | `qoder_fix` | Qoder CLI 修复引擎接入（逐字节一致性校验、credits 计量） |
| 修复 | `qoder_guard` | Qoder 调用守卫（挂死防护、超时终止） |
| 修复 | `sandbox` | Docker 沙箱工作区（补丁应用、隔离测试） |
| 修复 | `git_integration` | 分支/提交/MR 集成 |
| 修复 | `changes` | 变更关联（发布与需求/告警的追溯） |
| 发布 | `release` | 发布执行（demo 留痕 / production 真实滚动、金丝雀与回滚） |
| 发布 | `release_journal` | 发布日志与协调路径（`coordination_paths()` 唯一来源） |
| 发布 | `backup` | 数据备份（持锁 + `assert_idle_locked()` 空闲校验） |
| 发布 | `cleanup` | 沙箱与临时产物清理 |
| 发布 | `kill_switch` | 全局降级开关（一键停自动修复/发布，保留手动通道） |
| 通知 | `notify` | 飞书/钉钉卡片渲染与投递（shadow 卡、fix-start 卡、审批卡、广播卡，应答码判定） |
| 通知 | `chatops` | ChatOps 命令解析与执行（IM 入口后端） |
| 注册 | `app_registry` | 被监控应用清单（production=MySQL 表 / demo=JSON 文件） |
| 注册 | `app_manifest` | 应用清单数据结构与校验 |
| 注册 | `readiness` | 就绪状态判定（发布前置条件） |
| 需求 | `requirement_agent` | 需求驱动修复 Agent（从需求分析到修复请求） |
| 需求 | `requirements_client` | 被监控系统需求接口客户端 |
| 存储 | `db` | MySQL 5 表（system_kv / console_tokens / monitored_apps / audit_events / workflow_runs）与 schema 幂等维护 |
| 存储 | `alert_store` | 告警聚合三表（alert_domains / alert_events / alert_members，租约与跨实例去重） |
| 可观测 | `metrics` | Prometheus 指标（旁路，异常吞掉降级） |
| 可观测 | `tracing` | OpenTelemetry 追踪（旁路，延迟导入不变量） |

---

## 3 BFF 后端（`bff/`，14 个功能模块 + 14 个路由）

| 分组 | 模块 | 职责 |
| --- | --- | --- |
| 网关核心 | `app` | FastAPI 应用组装、路由挂载、SPA 静态托管 |
| 网关核心 | `middleware` | 鉴权守卫（会话优先→静态令牌回落）、IM 通道独立判定、卡片回调豁免集、安全头、可信代理判定 |
| 网关核心 | `auth` | 令牌注册表（demo=明文文件 / production=sha256 指纹）与轮换 |
| 网关核心 | `deps` | 依赖注入（当前用户、角色） |
| 网关核心 | `ratelimit` | 限速（按可信客户端 IP 分桶） |
| 数据网关 | `temporal_gateway` | Temporal list / status query / signal / result |
| 数据网关 | `aggregator` | data 产物聚合（argocd 留痕、notify 广播、sandbox 目录、code_index stats） |
| 数据网关 | `runs_store` | workflow_runs 运行记录读写 |
| 域服务 | `audit` | 操作审计（demo=JSONL 文件 / production=audit_events 表） |
| 域服务 | `config_items` | 系统配置项读写（system_kv） |
| 域服务 | `monitored_apps` | 被监控应用管理（双后端） |
| 域服务 | `escalations` | 升级人工队列 |
| 域服务 | `ops_metrics` | 运营度量面板数据 |
| 域服务 | `requirement_analyses` | 需求分析记录（demo=JSON 文件 / production=MySQL） |

**路由模块（`bff/routes/`，14 个）**：`flows` / `approvals` / `audit` / `chatops` / `im_callback` / `requirements` / `escalations` / `monitored_apps` / `system` / `systems` / `tokens` / `users` / `ops_metrics` / `health`

---

## 4 前端控制台（`web/`，Vite + React + TypeScript）

### 4.1 页面（14）

| 页面 | 说明 |
| --- | --- |
| `Login` | 令牌登录（localStorage 键 `aiops.token`） |
| `Flows` | 流程列表（分页、行内菜单、状态筛选） |
| `FlowDetail` | 流程详情（阶段条、倒计时、Diff 视图、事件时间线、审批操作） |
| `Approvals` | 审批中心（待办过滤语义：仅展示可写阶段） |
| `WindowPage` | 发布窗口/倒计时管理 |
| `Requirements` | 需求基线（状态徽标、智能分析入口） |
| `Escalations` | 升级人工队列 |
| `Audit` | 审计回看（流程审计 Tab + 操作审计） |
| `MonitoredApps` | 被监控应用管理 |
| `Users` | 用户与令牌管理 |
| `SystemOverview` | 系统概览（运行概况、图表） |
| `SystemWorkbench` | 系统工作台（配置、策略快照） |
| `SystemPage` | 系统设置（档位信息、kill switch） |
| `Help` | 使用帮助 |

### 4.2 组件（23）

`AppShell`（侧栏布局，收缩/权限门控）、`FlowCard`、`FlowStageStrip`、`StageBadge`、`CountdownText`、`DiffViewer`、`EventTimeline`、`QueuePatchDialog`、`RequirementAnalysisModal`、`MonitoredAppStatus`、`GlobalSituation`、`PermissionGate`、`ActionButton`、`ConfirmDialog`、`CopyableId`、`EmptyState`、`JsonBlock`、`Logo`、`Modal`、`Pagination`、`ThemeMenu`、`Toast`、`UserMenu`

### 4.3 图表（3）与工具库（7）

- 图表：`Donut` / `FlowStateMachine`（流程状态机）/ `ReleasePipeline`（发布管线）
- `lib/`：`api` / `actions` / `permission`（同源权限规则）/ `theme`（token 双层结构）/ `hooks` / `format` / `types`

---

## 5 基础设施容器

### 5.1 默认栈（`docker-compose.yml`）

| 容器 | 镜像 | 默认端口 | 必需性 | 说明 |
| --- | --- | --- | --- | --- |
| `aiops-temporal` | temporalio/temporal:latest | 7233（gRPC）/ 8233（UI） | 必需 | Temporal dev server，SQLite 持久化（命名卷），retention 校准 168h |
| `aiops-loki` | grafana/loki:latest | 3101 | 可选（真实日志链路） | 日志存储，命名卷持久化 |

### 5.2 profile 栈

| 容器 | profile | 镜像 | 默认端口 | 说明 |
| --- | --- | --- | --- | --- |
| `aiops-mysql` | `prod` | mysql:8.0 | 3316 | production 档持久化（5 表）；避开宿主 3306 |
| `aiops-redis` | `prod` | redis:7-alpine | 6380 | 缓存/限速；避开宿主 6379 |
| `aiops-prometheus` | `obs` | prom/prometheus:latest | 9095 | 采集 BFF :8600/metrics 与 worker :9090/metrics |
| `aiops-grafana` | `obs` | grafana/grafana:latest | 3200 | 自动加载 aiops-overview 面板 |

### 5.3 生产编排（`docker-compose.production.yml`）

`temporal-prod`（生产模式 Temporal：MySQL 后端）+ `temporal-schema` / `temporal-namespace`（maintenance profile 初始化）；MySQL / Redis 为默认常启；Loki / Prometheus / Grafana 归入 `optional-observability`。

### 5.4 入口与编排辅助

| 组件 | 位置 | 端口 | 说明 |
| --- | --- | --- | --- |
| Caddy | `deploy/caddy/Caddyfile` | 8643（HTTPS） | 生产 HTTPS 入口，launchd 托管；`real_ip_trusted_addresses` 指向 BFF |
| K8s 清单 | `deploy/k8s/` | — | 可选生产部署路径：worker / bff / mysql / redis / temporal / webhook / cronjob / networkpolicy / servicemonitor / secret.example |
| launchd 托管 | `deploy/launchd/` | — | 三项 plist：watchdog / backup / caddy（登录自启） |

---

## 6 外部依赖组件

| 组件 | 版本要求 | 默认端口 | 说明 |
| --- | --- | --- | --- |
| Python | 3.12（实测 3.12.0） | — | 全部自建进程与工具链运行时（项目内 `.venv`） |
| Ollama | 最新稳定版 | 11434 | LLM 链路：生成默认 `qwen3.8:27b-mlx`（可切 `qwen3:8b`），嵌入 `bge-m3:latest` |
| Docker Engine / Desktop（Colima） | 支持 `build/run/port` | — | 沙箱、发布、demo-app 容器；基础镜像 python:3.12-slim 本地化 |
| Temporal SDK | temporalio 1.33.0 | — | Python Worker / 客户端 |
| Alertmanager | 任意 0.2x | 9093 | 告警源（复用 nl2sql-monitoring 栈，`apply_alertmanager_route.py` 配置子路由） |
| 飞书 / 钉钉机器人 | — | — | 通知出口（飞书为真实投递；钉钉可选）；飞书卡片回调经 BFF 网关（`AIOPS_FEISHU_VERIFY_TOKEN` 验签） |
| ArgoCD | — | — | 发布留痕（`data/argocd/*.json`）；配置 `AIOPS_ARGOCD_URL` 可直连真实 API |
| Qoder CLI | — | — | 代码修复引擎（`qoder_fix` 接入，实测单次约 50.5s / 0.752 credits） |

---

## 7 被监控 / 被演示系统

| 系统 | 位置 | 端口 | 说明 |
| --- | --- | --- | --- |
| demo-app | `demo-app/`（FastAPI） | 稳定容器 :18080 | 演示被修复应用；沙箱内为已应用补丁的副本；auth/ 为受保护目录演示用例 |
| nl2sql 智能问数据分析系统 | 外部（经标准接口接入） | — | 生产被监控应用（日志、需求反馈、发布闭环实测对象） |

---

## 8 运维工具链（`scripts/` 及根级）

### 8.1 起停与治理

| 工具 | 用途 |
| --- | --- |
| `start.sh`（根级） | 唯一启动入口（无隐式动作：不灌日志/不起流程/不开浏览器）；子命令 back/up/web/restart/down/stop/status/env/preflight/maintenance/resume/demo/install/uninstall/down-all/sync-ip |
| `scripts/demo-up.sh` | 兼容薄壳（13 行，转发 `scripts/aiops-watchdog.py`）；底层命令唯一实现来源为 watchdog：up/down/stop/restart/status/env/preflight/demo/maintenance/resume/install/uninstall/down-all |
| `scripts/aiops-watchdog.py` | 组件守护（同 §1） |
| `scripts/backup-data.py` | 数据备份（与 release_journal 协调路径逐字一致） |
| `scripts/cleanup-sandbox.sh` | 沙箱产物清理（独立卷 `AIOPS_SANDBOX_ROOT`） |
| `scripts/rotate-logs.sh` | 日志轮转 |
| `scripts/sync_lan_ip.py` | 局域网地址同步（start.sh sync-ip） |
| `scripts/launchd-install.sh` | launchd 托管安装（watchdog/backup/caddy） |

### 8.2 生产切换与验证

`scripts/production_deploy.py`（受控部署）/ `production_tls.py`（HTTPS 证书）/ `production-smoke.py`（冒烟）/ `production_baseline.py`（基线校准）

### 8.3 CI 与评测

| 工具 | 用途 |
| --- | --- |
| `scripts/ci_dependencies.py` / `ci_integration.py` | 依赖与集成检查 |
| `scripts/e2e_auto_adapt.py` | 端到端自动适配验证 |
| `scripts/security-scan.sh` | Bandit 安全门禁（按 High 计数判定） |
| `eval_triage.py` / `eval_fix.py` / `gen_fix_eval_samples.py` | triage 与修复评测（报告 `data/triage_eval_report.json` / `fix_eval_report.json`） |
| `index_codebase.py` | Code RAG 索引构建（`data/code_index.*`） |
| `check_aiops_interface.py` | 被监控系统标准接口契约校验 |

### 8.4 演示剧本（`scripts/scenarios/01-08`）

`01-happy-path` / `02-low-confidence` / `03-protected-dir` / `04-test-retry` / `05-test-exhaust` / `06-canary-rollback` / `07-queue-patch` / `08-window-cancel`

### 8.5 监控与告警配置

| 工具 | 用途 |
| --- | --- |
| `monitoring/apply_alertmanager_route.py` | Alertmanager 子路由配置（指向告警接入 :8099） |
| `prometheus/aiops-demo-rules.yml` | 演示监控规则（指标 + 日志突增告警） |
| `deploy/prometheus/aiops-alerts.yml` | obs profile 告警规则 |

---

## 9 端口速查总表

| 端口 | 组件 | 档位 |
| --- | --- | --- |
| 7233 | Temporal gRPC | 默认 |
| 8233 | Temporal Web UI | 默认 |
| 8600 | BFF 控制台 | 默认 |
| 8099 | 告警接入 | 默认 |
| 9090 | Worker 指标 | 默认（常被宿主 ssh 占用，不影响主链路） |
| 3101 | Loki | 默认 |
| 3316 | MySQL | prod |
| 6380 | Redis | prod |
| 9095 | Prometheus | obs |
| 3200 | Grafana | obs |
| 8643 | Caddy HTTPS 入口 | production |
| 11434 | Ollama | 外部依赖 |
| 9093 | Alertmanager | 外部依赖 |
| 18080 | demo-app 稳定容器 | 演示 |
| 5273 | Vite dev 热更 | 开发（`./start.sh web`） |

---

## 10 关键依赖关系（文字说明）

- **触发链**：Alertmanager(9093) → 告警接入(8099) → Temporal(7233)；日志突增检测（Loki 3101）与可用性巡检同样经 Temporal 启动流程。
- **执行链**：Temporal → Worker → Ollama(11434)（triage/修复生成）→ Code RAG（本地索引）→ Docker 沙箱（补丁验证）→ 发布（demo 留痕 / production 真实滚动）→ 通知（飞书）。
- **展示链**：浏览器 / 飞书回调 → BFF(8600) → Temporal + data 产物；浏览器不直连 Temporal。
- **持久化**：demo 档 = 文件（`data/`）；production 档 = MySQL(3316) 5 表 + Redis(6380)；Temporal 与 Loki 均为命名卷持久化。
- **一键编排**：`./start.sh` → `scripts/demo-up.sh`（兼容薄壳）→ `scripts/aiops-watchdog.py` 执行编排（6 组件 + 守护）；watchdog 崩溃拉起，launchd 可选托管登录自启。
