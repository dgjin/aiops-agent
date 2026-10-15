# AIOps 自动运维智能体系统设计说明书

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.7 |
| 日期 | 2026-10-01（v1.7 修订 2026-10-14） |
| 状态 | 已实现系统（与 aiops-agent/ 工程代码逐项对齐） |
| 关联文档 | 《AIOps 自动运维智能体系统设计方案》v1.1、《AIOps 自动运维智能体系统实施计划》v1.0、《用户操作手册》、《部署手册》 |
| 适用范围 | 应用服务器异常自愈、AI 辅助代码修复与自动化发布（含演示环境全链路） |
| 修订记录 | v1.7（2026-10-14）：飞书客户端确认交互落地（长连接模式）——审批卡片发送侧切换为企业自建应用机器人（新增 `aiops_agent/feishu_app.py`：tenant_access_token 缓存、`im/v1/messages` 发卡、`im/v1/chats` 列群；三要素齐全优先于 webhook，失败降级留痕不回落），接收侧新增长连接网关 `feishu_gateway.py`（WSS 免公网订阅 `card.action.trigger` → 注入共享凭证转发 BFF 回调端点 → 回写 toast/结果卡；3 秒预算内转发超时 2.5s；含 CARD 帧改写补丁规避 lark-oapi ≤1.7.4 issue #126）；watchdog 应用凭证齐全时自动加挂（默认仍 6 组件）；`AIOPS_FEISHU_VERIFY_TOKEN` 语义调整为网关↔BFF 共享凭证（长连接无需与飞书后台一致）；测试基线 1364 用例；v1.6（2026-10-14）：飞书卡片回调网关落地（审批交互闭环）——`POST /api/im/card-callback` 接收新版 `card.action.trigger` 回调（Verification Token 验签 via `AIOPS_FEISHU_VERIFY_TOKEN`、挑战握手、加密载荷拒绝），复用角色判定（`AIOPS_IM_USERS`，operator 才可审批）/写守卫/kill switch 转发 Temporal 信号，响应 200 + toast + 处置结果卡（`render_approval_result_card`，防重复点击）；按钮载荷 `wf_id` 改由活动层注入真实流程 ID（`activity.info().workflow_id`，需求/重跑流程与公式值不一致）；middleware 回调通道豁免 Bearer（fail-closed：未配置 503 / 凭证不符 401）；测试基线 1331 用例；v1.5（2026-10-13）：影子建议续接——影子档 SHADOWED 建议归档评估后可续接执行完整链路（`resume_patch` 快照键复用原补丁、首轮跳过生成直入完整沙箱验证；端点 `POST /api/requirements/analyses/{id}/resume-shadow`，续接流程 id 固定 `-resume1` 幂等防重、会话 `approved.wf_id` 重指向并溯源 `resumed_from`）；发布层门禁按策略档位判定（影子档禁止真实发布 / 全链路放行，策略不可确认 fail-closed 防旁路）；测试基线 1305 用例；v1.4（2026-10-13）：生产档切换全链路执行——`release-gate-policy.yaml` 显式声明 `shadow_mode: false`；`validate_runtime_policy` 由「强制影子档」调整为「production 必须显式声明 shadow_mode（true=影子档 / false=全链路），缺失即拒绝启动」防静默漂移；readiness 配置校验标签 `valid_full_chain`；v1.3（2026-10-13）：IM 流程通知补齐——新增接收/需求接收/进入修复三类流程卡片（4.6 共 7 类卡片），工作流调用以 patched 标记守护（flow-intake-notify-g1 / requirement-intake-notify-g1 / fix-start-notify-g1）；activity 13→16；v1.2：测试基线 836 用例；运维治理加固：组件守护单实例锁与档位锁定+一致性校验、开机自启（launchd）、备份与恢复演练、数据清理与日志轮转、doctor 磁盘水位检查、启动档位横幅；v1.1：测试基线 832 用例；默认模型切换 qwen3.8:27b-mlx；新增 Qoder 修复引擎（4.3.3）、需求分析闭环（4.9）、可用性巡检/日志采集/组件守护；BFF 路由扩至 11 域 |

---

## 1 概述

### 1.1 编写目的

本文档描述 AIOps 自动运维智能体**已落地实现**的系统设计：五层架构、Temporal 工作流编排、三道闸门、各模块真实实现细节、接口契约与容错机制。与《设计方案》（顶层设计）和《实施计划》（工作包拆解）配套，供开发维护、评审验收与生产化改造参照。

本文所有接口名、配置键、环境变量、数据字段均与 `aiops-agent/` 工程代码一一对应，行文中的行为描述已通过单元测试（1364 个用例，2026-10-14 基线）与端到端实测验证。

### 1.2 系统定位

系统以「监控 → 分析 → 修复 → 测试 → 审批 → 延迟发布」全链路自动化为目标，将常规线上异常的处理从小时级压缩到分钟级，同时以三道闸门确保变更可控。核心原则：

1. **默认安全侧**——所有不确定路径（超时、接口异常、LLM 输出非法、证据不足）一律走向驳回或转人工，绝不自动放行；
2. **策略即配置**——闸门行为由 `release-gate-policy.yaml` 启动快照决定，锁定项由 pydantic 校验器强制，改配置绕过锁定会拒绝启动；
3. **全程可审计**——每阶段留痕（日志、`gate_events`、通知留痕文件、ArgoCD manifest 留痕、沙箱工作区），工作流结束时输出完整审计记录。

### 1.3 建设现状总览（真实化对照）

| 链路环节 | 实现状态 | 真实实现说明 |
| --- | --- | --- |
| 告警接入 | ✅ 真实 | Alertmanager v4 webhook → 标准化 `Alert` → 幂等启动 workflow（`alert_webhook.py`） |
| 日志采集/查询 | ✅ 真实 | Loki push/query API（`logs.py`），演示与生产同一查询接口 |
| 日志聚类降噪 | ✅ 真实 | Drain3 模板聚类（depth=4，相似度 0.5，trace_id/HEX/NUM 掩码） |
| 日志突增检测 | ✅ 真实 | Loki `count_over_time` 环比基线 → Alertmanager API 注入告警（`log_surge_detector.py`） |
| LLM 根因分析 | ✅ 真实 | Ollama `/api/chat` JSON 模式 + pydantic schema 校验 + 安全侧降级（`triage.py`） |
| Code RAG | ✅ 真实 | AST 语义切块 + bge-m3 嵌入 + FAISS/Python 双后端索引（`code_rag.py`） |
| 补丁生成 | ✅ 真实 | LLM 生成 unified diff → 宽容解析 → 应用 → 编译校验（`fix_agent.py`） |
| 测试沙箱 | ✅ 真实 | Docker 容器隔离执行 unittest；K8s Job + gVisor manifest 已就绪（`sandbox.py`） |
| 自动创建 MR | ✅ 真实 | GitLab/GitHub/Gitee 三平台真实创建 + 标签/描述内嵌 diff 与测试报告；未配置/失败降级 recorded/degraded 不中断（`git_integration.py`，见 4.8） |
| 审批卡片 | ✅ 真实 | 飞书/钉钉交互卡片渲染 + 真实 POST（加签）+ 留痕（`notify.py`） |
| 用户公告 | ✅ 真实 | 公告卡片投递 + 留痕（`notify.py`）；WebSocket 推送为生产接线点 |
| 金丝雀发布 | ✅ 真实 | ArgoCD Application 渲染/提交 + 本机 Docker 真实滚动 + 真实 HTTP 探活（`release.py`） |
| 自动回滚 | ✅ 真实 | 劣化时回收金丝雀、稳定版本保持不变（留痕文案区自动/等待人工） |
| 生产持久化 | ✅ 真实 | 双模运行（`mode.py`）：production=MySQL 5 表 + fail-fast；demo 文件后端零迁移（第 10 章） |
| 安全加固 | ✅ 真实 | 令牌指纹注册表 + 滑动窗口限速（Redis/进程内）+ 安全头 + kill switch（`kill_switch.py`） |
| 可观测 | ✅ 真实 | Prometheus 指标（BFF `127.0.0.1:8600`、worker production `:9091` / demo `:9090`，均仅 loopback）+ 6 告警 + Grafana 9 面板 + OTel 可选 |
| 数据生命周期 | ✅ 真实 | TTL 清理（`cleanup.py` + K8s CronJob）；Temporal retention 720h（部署侧） |
| 可用性巡检 | ✅ 真实 | 主动巡检「被监控应用」清单 → 连续失败注入告警 → 自动修复（`app_prober.py`）；页面级默认通用白屏判据（`page_health.py`） |
| 日志实时采集 | ✅ 真实 | 清单模式增量采集应用日志 → Loki，扮演 Filebeat 角色（`ship_app_logs.py`，位点持久化重启不重推） |
| 控制台 | ✅ 真实 | BFF（FastAPI :8600）+ React SPA：看板/审批/发布窗口/审计/被监控应用/需求基线/转人工待办/用户管理；demo 支持局域网访问，production 收敛 loopback 并经本机 Caddy HTTPS（8643）进入 |
| 需求基线闭环 | ✅ 真实 | 需求导出接口拉取 → LLM 主动分析 → 控制台审批 → 驱动修复（`analyze_requirements.py` 等，见 4.9） |
| 组件守护 | ✅ 真实 | 6 常驻组件崩溃自动拉起（`scripts/aiops-watchdog.py`，崩溃风暴冷却）；`demo-up.sh` 一键编排 |

> 说明：演示环境不存在的后端（Git 平台、K8s 集群、ArgoCD 服务、IM webhook）以「桩/留痕模式」对齐同一接口语义，配置对应环境变量即可切换为 live 模式；MR 创建三平台与失败降级链已完整落地。

---

## 2 总体架构

### 2.1 分层架构

```text
┌────────────────────────────────────────────────────────────────────┐
│ 执行层  release.py       ArgoCD Application 渲染/提交 + Docker 真实滚动   │
│                          + 真实探活观测（SLO 判定）→ 全量 / 回滚          │
├────────────────────────────────────────────────────────────────────┤
│ 通知层  notify.py        飞书/钉钉审批卡片、公告卡片、升级卡片 + 真实投递   │
│                          + 回调契约解析 + 回调网关（4.6.1）              │
├────────────────────────────────────────────────────────────────────┤
│ 验证层  sandbox.py       SAST 静态扫描 → 工作区准备 → 隔离容器执行单测      │
│                          （Docker 本机形态 / K8s Job+gVisor 生产形态）     │
├────────────────────────────────────────────────────────────────────┤
│ 决策层  code_rag.py      AST 切块 + 向量索引（FAISS/纯 Python 双后端）      │
│         fix_agent.py     LLM 生成最小 diff → 应用校验 → 兜底补丁           │
├────────────────────────────────────────────────────────────────────┤
│ 分析层  triage.py        Ollama LLM 根因判定 + 置信度评分（闸门 1）         │
│         logs.py          Drain3 模板聚类降噪                             │
├────────────────────────────────────────────────────────────────────┤
│ 感知层  alert_webhook.py Alertmanager webhook → 标准化 Alert → 幂等启动    │
│         log_surge_detector.py  错误日志突增检测 → 注入 Alertmanager        │
├────────────────────────────────────────────────────────────────────┤
│ 编排    workflows.py / activities.py / config.py / worker.py            │
│         Temporal：状态机 + 信号 + 查询 + 定时器 + 重试 + 审计             │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 组件与代码映射

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| 工作流编排 | `aiops_agent/workflows.py` | `AIOpsFixWorkflow` 状态机、信号/查询、三道闸门、FIFO 队列、审计记录 |
| 活动集合 | `aiops_agent/activities.py` | 16 个 activity：感知/分析/修复/沙箱/审批/公告/流程通知（接收·需求接收·进入修复）/金丝雀与发布终态 |
| 策略配置 | `aiops_agent/config.py` | YAML + pydantic 强校验、锁定项校验器、启动快照 |
| 数据模型 | `aiops_agent/models.py` | Alert / RootCause / Patch / TestReport / CanaryResult / ReleaseResult |
| Worker | `aiops_agent/worker.py` | 注册工作流与 activity、加载策略快照、连接 Temporal |
| 告警接入 | `alert_webhook.py` | HTTP 服务：Alertmanager v4 载荷 → 幂等启动 workflow |
| 日志管道 | `aiops_agent/logs.py` | Loki push/query + Drain3 聚类 + 突增判定纯函数 |
| 突增检测 | `log_surge_detector.py` | 周期检测 + 冷却防抖 + Alertmanager 告警注入 |
| 根因分析 | `aiops_agent/triage.py` | triage prompt、Ollama 调用、schema 校验、安全侧降级 |
| Code RAG | `aiops_agent/code_rag.py` | AST 切块、嵌入、FAISS/纯 Python 双后端索引与检索 |
| 修复 Agent | `aiops_agent/fix_agent.py` | 目标定位、修复 prompt、diff 应用管线、编译校验、兜底补丁 |
| 沙箱 | `aiops_agent/sandbox.py` | SAST、工作区、Docker/K8s 隔离执行、unittest 解析、TestReport |
| 通知 | `aiops_agent/notify.py` + `aiops_agent/feishu_app.py` | 七类卡片渲染（审批/公告/升级/影子建议/接收/需求接收/进入修复）+ 处置结果卡（批准/驳回回执）、投递器（recorded/live/failed；飞书应用三要素齐全优先应用机器人发卡＝`live/channel=feishu-app`，失败降级不回落 webhook）、回调解析（新旧版兼容）+ 操作者提取 |
| 回调网关 | `bff/routes/im_callback.py` | 飞书卡片回调端点（验签/角色/写守卫/kill switch/signal/审计/处置结果卡响应），见 4.6.1 |
| 长连接网关 | `feishu_gateway.py` | 飞书长连接（WSS）接收 `card.action.trigger` → 注入共享凭证转发 BFF 回调端点 → 回写 toast/结果卡；`--list-chats` / `--send-test-card` 辅助工具；含 CARD 帧改写补丁（lark-oapi issue #126）；watchdog 条件托管（应用凭证齐全才注册），见 4.6.1 |
| 发布 | `aiops_agent/release.py` | ArgoCD 渲染/提交、镜像构建、金丝雀容器、探活观测、晋级/回收 |
| 演示 CLI | `demo_cli.py` | 启动流程、审批/加速/取消/排队、状态查询、结果获取 |
| 索引工具 | `index_codebase.py` | 建库 / 检索 CLI（代码块 + 历史工单） |
| 评测工具 | `eval_triage.py` / `eval_fix.py` | 根因 / 修复离线评测与报告 |
| 演示数据 | `demo_log_generator.py` | 模拟应用日志推送 Loki（normal / surge 两模式） |
| 监控接入 | `monitoring/apply_alertmanager_route.py`、`prometheus/aiops-demo-rules.yml` | Alertmanager 路由注入、告警规则样例 |
| 运行模式 | `aiops_agent/mode.py` | `production`（默认，fail-fast）/ `demo` 双模判定、数据根目录 |
| 关系库层 | `aiops_agent/db.py` | SQLAlchemy 引擎/连接池/建表（5 表）、`now_iso/now_dt` 时间口径 |
| 可观测 | `aiops_agent/metrics.py`、`deploy/prometheus/*`、`deploy/grafana/*` | Prometheus 指标（BFF/workflow/activity）、告警规则 6 条、Grafana 9 面板 |
| 紧急停止 | `aiops_agent/kill_switch.py` | 全局冻结开关（BFF 写操作 + webhook 新流程），fail-closed |
| 数据清理 | `aiops_agent/cleanup.py` | TTL 清理（审计/沙箱/通知/Qoder/ArgoCD/执行记录），CronJob 入口 |
| MR 创建 | `aiops_agent/git_integration.py` | GitLab/GitHub/Gitee 三平台真实创建 + recorded/degraded 降级 |
| 可用性巡检 | `app_prober.py` | 清单巡检 → 连续失败 → Alertmanager 注入 `AppUnreachable` → 自动修复流程 |
| 日志采集 | `ship_app_logs.py` | 清单模式增量采集应用日志 → Loki（service/level 标签 + `trace_id` 注入） |
| 页面健康判据 | `aiops_agent/page_health.py` | 通用白屏检测（响应非空 + HTML 完整闭合 + body 可渲染容器）；配置关键字时精确匹配 |
| 需求基线 | `analyze_requirements.py`、`aiops_agent/requirement_agent.py`、`requirements_client.py` | 需求导出接口拉取 → LLM 主动分析 → 控制台审批 → 驱动修复流程 |
| 应用注册表 | `aiops_agent/app_registry.py`、`app_manifest.py` | 清单/Manifest → 修复契约（关键字/健康路径/专属索引/仓库定位） |
| 变更关联 | `aiops_agent/changes.py` | 最近发布/配置变更注入 triage 上下文（双通道变更源：外部 feed + ArgoCD 留痕） |
| 控制台 | `bff/` + `web/` | FastAPI BFF（14 域路由，:8600）+ React SPA（同源托管，局域网可访问） |
| 组件守护与编排 | `scripts/aiops-watchdog.py`、`scripts/demo-up.sh` | 常驻组件巡检自动拉起（默认 6 个；飞书应用凭证齐全时含长连接网关共 7 个；单实例锁 + 档位锁定/一致性校验，可交 launchd 托管）/ 一键启动停止与状态（含档位横幅） |
| 生产交付物 | `deploy/` | Dockerfile、K8s 清单 12 文件（38 资源）、Prometheus、Grafana |

### 2.3 技术栈

| 模块 | 选型 | 版本/参数（实测环境） |
| --- | --- | --- |
| 工作流编排 | Temporal + temporalio Python SDK | `temporalio>=1.7.0`；task queue `aiops-tasks` |
| 策略校验 | pydantic v2 + PyYAML | `pydantic>=2.5.0` |
| 日志聚类 | Drain3 | `drain3>=0.9.11`；depth=4，sim_threshold=0.5 |
| 日志存储 | Loki（本机栈 3101 端口） | push/query API |
| LLM 推理 | Ollama（本机 11434） | triage/修复默认 `qwen3.8:27b-mlx`（可切轻量 `qwen3:8b`）；嵌入 `bge-m3:latest`（1024 维） |
| 修复引擎 | 本地 LLM（Ollama，默认）/ Qoder CLI 无头（可选） | `AIOPS_FIX_PROVIDER=ollama/qoder`；Qoder 默认模型 `DeepSeek-Flash`（见 4.3.3） |
| 向量检索 | FAISS（faiss-cpu）+ 纯 Python 回退 | `IndexFlatIP` + L2 归一化（内积=余弦） |
| 沙箱 | Docker（本机）/ K8s Job + gVisor（生产） | 镜像 `python:3.12-slim`；禁网、只读、限额 |
| 通知 | 飞书（企业自建应用机器人 / 自定义机器人）/ 钉钉自定义机器人 | 交互卡片；应用机器人卡片支持按钮回传（回调经 lark-oapi 长连接，见 4.6.1）；支持加签 |
| 发布 | ArgoCD API + Docker 滚动 | Application manifest；SLO：错误率 ≤5%、P99 ≤500ms |
| 生产持久化 | MySQL 8.0 + PyMySQL/SQLAlchemy 2.0 | InnoDB/utf8mb4；`pool_recycle=3600` + `pool_pre_ping`；BIGINT 自增/JSON/ENUM |
| 多副本限速 | Redis 7（可选） | 滑动窗口存 `aiops:rl:*`；未配置退化为进程内窗口 |
| 生产编排 | Kubernetes（deploy/k8s/） | 2 副本 + PDB + HPA；Temporal auto-setup（MySQL 后端，retention 720h） |
| 指标/看板 | Prometheus + Grafana | BFF `127.0.0.1:8600/metrics`、worker production `127.0.0.1:9091`（demo 9090）；6 告警 + 9 面板 |
| 追踪 | OpenTelemetry（可选） | OTLP HTTP（`AIOPS_OTEL_ENDPOINT`）；未配置完全 no-op |
| Git 集成 | stdlib urllib（零新依赖） | GitLab MR / GitHub PR / Gitee PR；失败降级不中断 |
| 运行时 | Python | 3.12 |

### 2.4 三道闸门

| 闸门 | 触发点 | 类型 | 实现机制 |
| --- | --- | --- | --- |
| 闸门 1 | 根因分析后 | 策略闸门 | `confidence < 0.8` → `ESCALATED` 转人工，不进入自动修复 |
| 闸门 2 | 测试通过后 | 人工闸门 | 审批卡片 + `submit_approval` 信号；超时 30 分钟自动驳回；受保护目录（`auth/**`、`payment/**`、`crypto/**`）追加 **二级审批**（`submit_second_approval`） |
| 闸门 3 | 审批通过后 | 用户闸门 | 公告卡片 + 15 分钟倒计时（`NOTIFYING`）；到期自动部署、可 `deploy_now` 加速、可 `cancel` 取消（队列保留）；窗口内新补丁仅 FIFO 入队 |

异常路径收敛：测试回炉重试耗尽、审批驳回/超时、金丝雀劣化 → 全部进入 `ESCALATED` 并投递升级卡片。

---

## 3 端到端流程设计

### 3.1 状态机

```text
DETECTED → TRIAGING → FIXING ⇄ TESTING → WAIT_APPROVAL → NOTIFYING → CANARY → ROLLING_OUT → DONE
              │           │                  │  │            │           │
              ▼           ▼                  ▼  ▼            ▼           ▼
        置信度<0.8   重试耗尽转人工        驳回/超时     二级审批驳回   cancel   劣化自愈
              │           │                  │  │            │           │
              └───────────┴──────────────────┴──┴────────────┴───────────┘
                                      ▼
                           ESCALATED / CANCELLED（全程审计留痕）
```

FIXING ⇄ TESTING 为回炉重试环：每次失败后 `attempt+1` 重新生成补丁，默认重试上限 `max_fix_retries=2`（共 3 次尝试），耗尽进入 `ESCALATED`。

测试通过后按策略运行档执行：`shadow_mode=true`（影子档）进入 `SHADOWED` 终态——仅推送影子档建议卡片（`notify_shadow_suggestion`），不创建 MR、不进入审批与发布；`false`（全链路，当前生产档）继续创建 MR → 闸门 2 审批 → 公告 → 金丝雀发布。production 档必须在策略中**显式声明** `shadow_mode`（缺失即拒绝启动，防静默漂移）；演示档 `demo-policy.yaml` 同为 `false`。影子档建议经归档评估决定采纳时走**影子建议续接**（`POST /api/requirements/analyses/{analysis_id}/resume-shadow`）：不重新分析、不重新生成补丁，以原 SHADOWED 结果的补丁经策略快照 `resume_patch` 注入续接流程（`workflows._patch_from_resume`，字段防御性兜底、非法输入返回 `None` 回落正常链路），首轮跳过 `generate_patch` 直入完整沙箱验证（`gate_events` 记 `resume:patch:{patch_id}`），验证失败回炉后仍走正常生成链路；续接流程 id 固定 `aiops-req-{analysis_id}-v{version}-resume1`（幂等防重），会话 `approved.wf_id` 重指向续接流程并记录 `resumed_from` 溯源。发布层另有按档位判定的最终防御（`release.shadow_release_only`）：影子档禁止真实发布（防绕过工作流 shadow 分支的旁路调用）、全链路档放行，策略不可确认时 fail-closed 按影子档处理；`deploy_canary` 对已注册直连型应用（含 url）放行全链路发布、容器型交付仍拒绝。

### 3.2 阶段说明

| 阶段 | 动作 | 对应 activity | 关键产出 |
| --- | --- | --- | --- |
| DETECTED | 告警事件生成（webhook / CLI / 突增检测）；**接收卡片**（处理流程说明） | `notify_flow_intake` | `Alert` |
| TRIAGING | 日志取证（Loki 近 10 分钟）+ Drain3 聚类 + LLM 根因 | `collect_evidence`、`cluster_logs`、`analyze_root_cause` | `RootCause` |
| FIXING | RAG 检索 + LLM 生成最小 diff + 应用/编译校验；**进入修复卡片**（结论/链路/模式） | `retrieve_similar_fixes`、`generate_patch`、`notify_fix_started` | `Patch` |
| TESTING | SAST + 隔离沙箱执行单测 | `run_tests_in_sandbox` | `TestReport` |
| WAIT_APPROVAL | 创建 MR（桩）+ 审批卡片 + 等待信号（超时 30m 自动驳回） | `create_merge_request`、`notify_approvers` | 审批结果 + `gate_events` |
| NOTIFYING | 公告卡片 + 倒计时（默认 15m）+ 窗口事件处理 | `notify_users` | 发布决策（expiry / deploy_now / cancel） |
| CANARY | ArgoCD 提交 + 镜像构建 + 金丝雀容器真实探活 | `deploy_canary` | `CanaryResult` |
| ROLLING_OUT | 达标全量滚动 / 劣化回收 | `finalize_release` | `ReleaseResult` |
| DONE / SHADOWED / ESCALATED / CANCELLED | 审计记录 + 队列承接；SHADOWED 推送修复建议后收口 | `escalate_to_human`、`notify_shadow_suggestion`、子工作流 | 审计 dict |

### 3.3 一次成功流程的数据流（实测链路）

```text
Alertmanager/webhook ──► alert_webhook ──► workflow 启动（幂等 id）
  │
  ├─ notify_flow_intake：接收卡片（流程启动即推送处理流程说明）
  ├─ collect_evidence：Loki 拉取近 10m 日志 → 提取 trace_id（最多 10 个）
  ├─ cluster_logs：Drain3 聚类 → 模板 + 计数（压缩比下降）
  ├─ analyze_root_cause：Ollama qwen3.8:27b-mlx → RootCause{error_type, suspect_files, confidence, summary}
  ├─ gate1：confidence ≥ 0.8 放行
  ├─ notify_fix_started：进入修复卡片（分析结论 + 修复链路 + 影子档/全链路模式）
  ├─ retrieve_similar_fixes：向量检索 top-5（代码块 + 历史工单）
  ├─ generate_patch：LLM diff → 应用 → py_compile 校验 → Patch
  ├─ run_tests_in_sandbox：SAST → docker run（禁网/只读/限额）→ unittest
  ├─ create_merge_request（桩）+ notify_approvers（审批卡片）
  ├─ gate2：submit_approval=approve（受保护目录加 gate2b）
  ├─ notify_users：公告卡片（版本号 v1.0.X）
  ├─ gate3：到期 / deploy_now / cancel
  ├─ deploy_canary：ArgoCD 提交 + docker build + 金丝雀容器 + observe 实测
  └─ finalize_release：达标 → 停旧稳定 → 起新稳定 → 就绪校验 → 回收金丝雀
```

### 3.4 异常路径与转人工规则

| 异常 | 检测点 | 处理 | 留痕事件 |
| --- | --- | --- | --- |
| 置信度不足 | 闸门 1 | 转人工，不修复 | `gate1:blocked:confidence=x<0.8` |
| 补丁无法生成/校验失败 | FIXING | 兜底补丁（degraded）继续沙箱 → 大概率被测试拦截 | `[fix] degraded=True` 日志 |
| 测试失败 | TESTING | attempt+1 回炉；耗尽转人工 | `tests:failed:attempt=n` |
| 审批驳回/超时 | 闸门 2 | 转人工 | `gate2:reject` / `gate2:timeout->reject` |
| 二级审批驳回/超时 | 闸门 2b | 转人工 | `gate2b:reject` / `gate2b:timeout->reject` |
| 窗口取消 | 闸门 3 | `CANCELLED`，队列保留并承接 | `gate3:cancelled`、`queue:next-cycle-started:*` |
| 金丝雀劣化 | CANARY | 回收金丝雀，稳定版本不动，转人工 | `canary:degraded->rollback` |

---

## 4 模块详细设计

### 4.1 感知层

#### 4.1.1 告警接入服务（`alert_webhook.py`）

- **接入形态**：HTTP POST `/webhook`（默认 `0.0.0.0:8099`），适配 Alertmanager v4 webhook 载荷；
- **标准化**：仅处理 `status == "firing"` 的告警（`resolved` 计数忽略）；字段映射：

| Alertmanager 字段 | Alert 字段 | 规则 |
| --- | --- | --- |
| `labels.alert_id` | `alert_id` | 优先取业务标签 |
| `fingerprint` 前 12 位 | `alert_id` | 无业务标签时的稳定兜底（labelset 哈希） |
| `labels.service` / `labels.job` | `service` | 依次回退，缺省 `unknown` |
| `labels.severity` | `severity` | 缺省 `critical` |
| `annotations.description` / `summary` / `alertname` | `description` | 依次回退 |

- **幂等**：workflow id = `aiops-fix-{service}-{alert_id}`，`id_reuse_policy=REJECT_DUPLICATE` + `id_conflict_policy=FAIL`；重复投递仅计入 `duplicates`，不产生第二个流程实例；
- **响应契约**：`200 {"started": [...], "duplicates": [...], "ignored_resolved": n}`；启动失败返回 `5xx` 交由 Alertmanager 重试；
- **策略快照**：服务启动时加载一次（与 worker 语义一致，运行期不热修改）。

#### 4.1.2 日志管道与聚类降噪（`logs.py`）

- **数据源**：Loki（默认 `http://localhost:3101`，`AIOPS_LOKI_URL` 可配）；
- **写入**：`push_lines(service, level, lines)` 以 `service`/`level` 流标签推送（演示环境由 `demo_log_generator.py` 扮演 Filebeat 角色，生产替换为 Filebeat / Fluent Bit，查询接口不变）；
- **查询**：`query_lines`（近 N 分钟、时间升序、最多 3000 行）；`query_metric`（instant 查询求和，供突增检测）；
- **字段规范**：日志行内携带 `trace_id=<id>`，`collect_evidence` 据此提取调用链关联 ID（生产对接 OTel Collector / Jaeger）；
- **聚类**：Drain3（`depth=4`、`sim_threshold=0.5`），掩码顺序固定为 `trace_id` → 十六进制长串（`HEX`）→ 纯数字（`NUM`），输出 `{templates:[{id,pattern,count}], compression_ratio, total_lines}`，按 count 降序供 LLM 消费（最多取前 10 个模板）；
- **突增判定（纯函数）**：`is_surge(recent, baseline, min_lines, factor)`——绝对门槛防低流量误报；基线为 0 时过门槛即算突增；基线 >0 时要求 `recent > factor * baseline`。

#### 4.1.3 日志突增检测器（`log_surge_detector.py`）

- 默认参数：轮询间隔 15s、统计窗口 1m、绝对门槛 100 行、环比倍数 3.0、告警冷却 300s；
- 检测：`recent = count_over_time({service="X", level="error"}[1m])`，`baseline = 同一查询 at (now-1h)`；
- 命中后向 Alertmanager `/api/v2/alerts` 注入 `LogErrorSurge` 告警（标签含 `aiops_demo: "true"`、`alert_id: logsurge-<svc>-<HHMMSS>`），经 Alertmanager 子路由自动转发至告警接入服务；
- 冷却期内同服务不重复告警（防抖）。

#### 4.1.4 监控规则与 Alertmanager 路由

- `prometheus/aiops-demo-rules.yml`：告警规则样例（Node CPU、MySQL 连接、Redis 内存、nl2sql 错误率环比突增），统一携带 `aiops_demo: "true"` 标签；默认不挂载，接入步骤见文件头注释；
- `monitoring/apply_alertmanager_route.py`：对复用监控栈的 Alertmanager 模板做**最小侵入**修改（新增子路由 `matchers: ['aiops_demo = "true"']` → receiver `aiops-agent`；receiver 指向 `http://host.docker.internal:8099/webhook`）；安全设计：修改前自动备份、幂等跳过、锚点校验失败即拒绝修改。

### 4.2 分析层（`triage.py`）

- **流程**：`build_triage_prompt` → `call_ollama`（`/api/chat`，`format=json`、`think=false`、`temperature=0.2`、默认模型 `qwen3.8:27b-mlx`、超时 45s）→ `strip_code_fence`（剥 markdown 围栏）→ `TriageOutput.model_validate_json` → `RootCause`；
- **输出 schema**：`error_type`（非空）、`suspect_files`（最多取前 3）、`confidence`（0~1，越界即校验失败）、`summary`（非空）；额外字段忽略（`extra="ignore"`）；
- **置信度校准**：prompt 内置校准规则——证据明确指向异常类型时 `>0.8`；证据不足必须 `<0.8`（配合闸门 1 拦截）；
- **安全侧降级**：任何异常（超时、连接失败、输出非法）→ `fallback_root_cause`（`confidence=0.0`）→ 闸门 1 自动转人工；
- **meta 留痕**：`{model, degraded, reason, elapsed_seconds, confidence, error_type}`；
- **历史工单**：`load_historical_tickets` 读取 `data/historical_tickets.json`（6 条样例工单），作为 WP4 RAG 语料。

### 4.3 决策层

#### 4.3.1 Code RAG（`code_rag.py`）

- **切块**：AST 语义切块（`chunk_python_source`）——函数/类级独立块，附 `file`/`qualname`/`start_line` 元数据，优于定长文本切块；
- **嵌入**：Ollama embeddings（默认 `bge-m3:latest`，1024 维）；
- **双后端索引**：

| 项 | FAISS 后端（默认 auto 优先） | 纯 Python 回退 |
| --- | --- | --- |
| 索引结构 | `IndexFlatIP` + L2 归一化（内积=余弦，精确检索） | JSON 内嵌向量 + 暴力余弦排序 |
| 持久化 | `data/code_index.faiss` + `data/code_index.json` | `data/code_index.json` |
| 触发条件 | `AIOPS_VECTOR_BACKEND=auto/faiss` 且依赖可用 | 强制 `python`、依赖缺失、索引陈旧（维度/行数不匹配）、读取异常 |

- **一致性保证**：FAISS `normalize_L2` 后内积与纯 Python `cosine` 同口径，实测两后端检索结果逐位一致（0.7441/0.5898/0.5481）；
- **检索接口**：`search_index(query, top_k=5)` 返回与纯 Python 版同构的结果（去向量、`similarity` 保留 4 位小数）；索引不可用时 `retrieve_similar_fixes` 降级为空引用，不阻断主流程；
- **内容**：代码块（demo-app 全部 .py）+ 历史工单（症状/根因/处置方案），统一进入同一个向量空间。

#### 4.3.2 修复 Agent（`fix_agent.py`）

- **目标定位**：`locate_target_file` 按嫌疑文件的路径 token 匹配（容忍 `src/` 前缀差异），定位失败 → 兜底补丁（degraded）；
- **修复 prompt**：根因 + RAG 引用 + 目标文件**带行号**全文 + 修复要求（最小化 diff、上下文行逐字符一致、空指针防护位置规则、禁止顺手重构、`a/` `b/` 文件头）+ 重试强化提示（attempt>0 时注入）；
- **模型选择链**：显式 `model` 参数 > `AIOPS_FIX_MODEL`（`DEFAULT_FIX_MODEL`）> triage 默认模型（`AIOPS_TRIAGE_MODEL`）；`timeout` 默认 45s，大模型评估可上调；
- **diff 应用管线（容错设计）**：
  1. `_parse_hunks`：宽容解析 hunk 头（容忍 LLM 常见的行号/计数误差）；
  2. `_apply_hunk` + `_match_context`：上下文行严格相等优先；随后容忍**行内空白**差异（`" ".join(split())` 折叠后相等），但**前导缩进必须逐字符一致**（缩进是 Python 语义）；匹配成功的上下文行**保留源文件原文**输出，避免污染未修改行；
  3. 应用后 `validate_source`（`py_compile` 编译校验）双重把关；
- **兜底补丁（degraded）**：未定位目标文件/DLL 输出非法/diff 不可应用/编译失败时，生成确定性兜底补丁（`_replace_lines_diff` / `_insert_after_diff` 变体），保证流程可继续到沙箱环节（大概率被真实测试拦截，安全）；
- **演示分支**：`_DEMO_STUB_MARKERS`（`low-conf`/`protected`/`test-fail`/`test-always-fail`/`canary-bad`）走确定性桩，保证演示矩阵回归不依赖 LLM；
- **补丁标签**：`patch_id=p-{alert_id}-r{attempt}`、`files`、`model_version`、`confidence` 全量进入 `Patch` 模型（可追溯）。

#### 4.3.3 Qoder 修复引擎（`qoder_fix.py`，可选提供者）

- **启用**：`AIOPS_FIX_PROVIDER=qoder`（默认 `ollama`）；Qoder CLI 无头调用（`AIOPS_QODER_BIN`，默认 `qoder`；鉴权 `AIOPS_QODER_TOKEN` / `QODER_PERSONAL_ACCESS_TOKEN`），在隔离工作区（`AIOPS_QODER_ROOT`，默认 `./data/qoder`）内自主读改代码；
- **安全参数**：`--permission-mode accept_edits`（非 bypass_permissions）+ 工具白/黑名单（`AIOPS_QODER_ALLOWED_TOOLS`）+ `--max-turns`（默认 100）+ 子进程超时（`AIOPS_QODER_TIMEOUT`，默认 720s）+ `--no-session-persistence`（无状态可复现）；
- **模型**：默认 `DeepSeek-Flash`——模型 ID **区分大小写**，写错不报错且静默回退 auto，接入层写入 `meta.model_warning` 留痕；
- **超时约束**：CLI 超时 + 快速重试预算（180s）必须小于修复活动 Temporal 超时（960s，`_FIX_ACTIVITY_TIMEOUT`），否则被硬取消、无法降级；
- **降级链**：未安装/鉴权失败/调用失败 → 安全侧兜底（沿用 `fix_agent` 降级路径），流程不中断；目标文件未定位时走「仓库级自主定位」模式（不直接降级）；
- **留痕**：隔离工作区、CLI 输出与 `meta`（provider/model/elapsed/degraded）进入流程审计。

### 4.4 验证层（`sandbox.py`）

- **编排顺序**：`scan_patch_diff`（SAST）→ `prepare_workspace`（复制 demo-app 到 `data/sandbox/<patch_id>/` 并应用补丁 diff）→ 隔离执行 → `TestReport`；
- **SAST（演示实现）**：扫描 diff **新增行**中的 8 类高危模式——`eval()`、`exec()`、`os.system()`、`subprocess`、`socket`、`requests/urllib.request`、`rm -rf`、硬编码敏感信息（password/api_key/secret/token 赋值）；命中即拦截（`passed=False`，测试不执行）；
- **隔离语义（生产 vs 本机对照）**：

| 隔离维度 | K8s Job + gVisor（生产） | Docker 容器（本机演示） |
| --- | --- | --- |
| 内核隔离 | `runtimeClassName: gvisor`（用户态内核） | 容器命名空间 |
| 网络 | default-deny NetworkPolicy（无出网权限） | `--network none` |
| 文件系统 | `readOnlyRootFilesystem` + emptyDir 挂载 | `--read-only` + `--tmpfs /tmp:size=32m` |
| 权限 | runAsNonRoot/65534、drop ALL capabilities、seccomp RuntimeDefault | 只读挂载 `-v <workspace>:/work:ro` |
| 限额 | 256Mi/1CPU/1Gi，`activeDeadlineSeconds=90` | `--memory 256m --cpus 1 --pids-limit 128` |
| 生命周期 | `backoffLimit=0` + `ttlSecondsAfterFinished=600`（即用即毁） | `--rm`（即用即毁） |

- **执行**：容器内 `python -m unittest discover -s tests -v`（工作区只读挂载、CWD=/work）；
- **结果解析**：`parse_unittest_output` 提取用例数、失败用例名、首个错误摘要；`passed = 退出码 0 且有用例且未超时`；「退出码 0 但 0 用例」视为失败（防静默通过）；
- **报告**：`TestReport{unit_tests, regression_tests, sast, details}`；失败详情格式 `失败用例：<name>、<name>；<error>（attempt=n）`（供评测口径解析）；
- **运行时就绪性**：Docker 不可用时返回 `exit_code=-127`（"沙箱运行时不可用"）→ 测试失败 → 回炉重试；持续失败转人工（安全侧）。

### 4.5 编排引擎与闸门（`workflows.py` / `activities.py` / `config.py`）

- **工作流定义**：`AIOpsFixWorkflow`（单类承载状态机）；信号 4 个、查询 1 个（见 5.2）；
- **Activity 调用策略**：`start_to_close_timeout=60s`（金丝雀/发布终态放宽至 600s，覆盖镜像构建与观测窗口）；`RetryPolicy(maximum_attempts=3, initial_interval=1s, backoff=2.0)`；重依赖（logs/triage/code_rag/fix_agent/sandbox/release/notify）在 activity 函数内**延迟导入**（避免 Temporal 沙箱验证 workflow 定义时触发 RestrictedWorkflowAccessError）；
- **闸门 1**：`root_cause.confidence < threshold` → `ESCALATED` + `escalate_to_human`；
- **测试回炉**：`for attempt in range(max_retries + 1)`；通过即 `break`；`else`（耗尽）→ 转人工；
- **闸门 2**：清空历史审批信号（防幽灵审批）→ `wait_condition(timeout=approval_timeout)`（SDK 语义：超时抛 `asyncio.TimeoutError`）→ 超时自动驳回留痕；`needs_second` 由 `_is_protected`（`auth/**` 前缀匹配）判定，二级审批超时同样自动驳回；
- **闸门 3**：倒计时以**服务端时间**为准（`workflow.now() + countdown`），循环等待 `deploy_now`/`cancel` 信号（不重置计时）；未知指令忽略并继续等待；`cancel` → `CANCELLED` + `_start_next_cycle`；
- **队列承接**：`_start_next_cycle` 取队首补丁以 `start_child_workflow(id=next_wf_id, parent_close_policy=ABANDON)` 启动新周期（父流程结束不影响子流程）；
- **审计记录**（`_final`）：`{workflow_id, alert_id, service, stage, root_cause, confidence, patch_id, model_version, approvals[], gate_events[], deploy_result, duration_seconds, queued_patches[]}`；
- **策略模型（`config.py`）**：`Policy → release_gate → {triage, approval, notify_window, canary}`；`extra="forbid"` 拒绝未知键；时长支持 `30s/15m/2h` 写法；`snapshot_for_workflow` 转纯 JSON dict（时长统一转秒）作为启动参数，工作流内**不读文件**。

**锁定项校验器**（改配置绕过即启动失败）：

| 锁定项 | 校验器 | 错误示例 |
| --- | --- | --- |
| 审批超时仅自动驳回 | `ApprovalConfig._lock_timeout_action` | `on_timeout` 非 `reject` 即抛错 |
| 窗口新补丁仅排队 | `NewPatchPolicy._lock_strategy/_lock_reset_timer/_lock_merge/_lock_queue_limit` | `reset_timer=true`、`queue_limit≠0` 均拒绝 |
| 用户反馈不熔断 | `FeedbackBreakerConfig._lock_enabled` | `enabled=true` 拒绝 |
| 并发底线 1 | `ConcurrencyConfig._lock_concurrency` | `max_active_deployments≠1` 拒绝 |
| 到期自动部署 | `NotifyWindowConfig._lock_deploy_on_expiry` | `deploy_on_expiry=false` 拒绝 |

### 4.6 通知层（`notify.py`）

- **七类卡片**（决策类：审批/公告/升级；状态类：影子建议 + 三类流程通知）：

| 卡片 | 渲染函数 | 内容要素 | 颜色主题 |
| --- | --- | --- | --- |
| 审批卡片 | `render_approval_card` | 根因（含置信度）+ 补丁（含风险/文件）+ 完整 diff + 测试报告 + 回滚预案 + 批准/驳回按钮；受保护目录追加二级提示 | 橙 |
| 用户公告卡片 | `render_broadcast_card` | 版本号 + 影响提示 + 服务/补丁标识 | 蓝 |
| 升级卡片 | `render_escalation_card` | 转人工原因 + 严重级别 + 处置动作 | 红 |
| 影子档建议卡片 | `render_shadow_card` | 根因 + 建议补丁（含完整 diff）+ 沙箱测试 + 「未创建 MR / 未审批 / 未发布」说明 | 青 |
| 接收卡片 | `render_intake_card` | 告警服务/描述 + 处理流程（日志取证 → 聚类降噪 → 根因分析 → 闸门1；通过后进入修复） | 浅蓝 |
| 需求接收卡片 | `render_requirement_intake_card` | 需求标题 + 来源分析（版本·批准人）+ 需求原文 + 验收要点 + 「已批准 → 直接进入修复」说明 | 浅蓝 |
| 进入修复卡片 | `render_fix_started_card` | 分析结论（含置信度）+ 疑似文件 + 修复链路 + 模式标注（影子档仅建议 / 全链路审批发布） | 靛蓝 |

- **流程通知挂载**（活动由工作流调用，投递同走 `NotificationSender` 留痕 `data/notify/`；`workflow.patched` 版本守护，旧运行中历史重放自动跳过）：告警流程启动即调 `notify_flow_intake`（标记 `flow-intake-notify-g1`，msg_id=`intake-<alert_id>`）；进入修复段（`_fix_to_release`，告警/需求共用）调 `notify_fix_started`（标记 `fix-start-notify-g1`，msg_id=`fix-start-<alert_id>`）；需求流程批准后调 `notify_requirement_intake`（标记 `requirement-intake-notify-g1`，msg_id=`req-intake-<entry_id>`）；

- **双 provider**：飞书（interactive card，按钮 `value` 携带回调契约）；钉钉（actionCard，按钮 `actionURL` 查询参数携带回调契约）；
- **投递器 `NotificationSender`**：三种模式——`live`（配置 webhook 真实 POST 且机器人应答码为 0，飞书加签秒级时间戳/钉钉加签毫秒级时间戳）、`recorded`（未配置 webhook，留痕 `data/notify/<msg_id>.json`）、`failed`（网络异常或机器人应答非零如加签不符，降级并携带原因，**通知失败不阻断修复主流程**）；无论何种模式均先留痕；
- **回调契约**：按钮载荷 `{"wf_id": "<运行中流程真实 ID>", "gate": "approval|second_approval", "decision": "approve|reject"}`——审批卡片在活动层（`notify_approvers`）渲染时经 `activity.info().workflow_id` 注入**运行中真实 workflow id**（需求流程 `aiops-req-…`、重跑/续接流程与公式值 `aiops-fix-<service>-<alert_id>` 不一致；公式值仅作为活动上下文外的回落）；`parse_callback` 同时支持飞书新版（`event.action.value`）/旧版（顶层 `action.value`）dict 结构与钉钉 actionURL 字符串，字段缺失或取值非法返回 `None`（服务端据此拒绝）；解析后的合法结果由内置回调网关转发 workflow signal（见 4.6.1，2026-10-14 落地）；

#### 4.6.1 飞书卡片回调网关（审批交互落地，`bff/routes/im_callback.py`）

飞书审批卡片上「批准 / 驳回」按钮此前仅有意愿、无服务端接线——本节点打通「按钮点击 → 飞书服务器回调 → 网关验签解析 → 权限判定 → Temporal 信号 → 卡片即时更新」全链路（2026-10-14 落地）：

- **协议**：接收新版 `card.action.trigger` 回调（Verification Token 在 `header.token`；`event.operator` 携带操作者身份、`event.action.value` 携带按钮载荷）；兼容旧版结构（`action` 在顶层）；配置回调地址时响应 `url_verification` 挑战握手（回显 `challenge`）；加密模式（Encrypt Key）不支持——携带 `encrypt` 字段的载荷明确拒绝（400，飞书后台需保持明文模式）；
- **响应契约**：除凭证/格式错误外一律 **HTTP 200 + toast**（飞书要求 3 秒内响应；5xx/超时会触发重试与客户端错误码）——权限不足 / 阶段不符 / 重复提交 / kill switch 等业务拒绝均在响应体 `toast.error` 中说明；审批成功后响应 `{toast: {type: success}, card: {type: raw, data: <处置结果卡>}}` 就地替换按钮区（`render_approval_result_card`：绿=批准 / 红=驳回，无按钮，防重复点击）；旧版回调响应 `card` 直接为卡片 JSON（无 `type/data` 包装）；
- **安全模型（fail-closed）**：middleware `_CALLBACK_ENDPOINTS` 精确路径豁免 Bearer（飞书无法携带控制台凭证），改以路由内 Verification Token 验签（恒时比较）——`AIOPS_FEISHU_VERIFY_TOKEN` 未配置＝通道关闭（503）、凭证不符＝认证失败（401），挑战握手与动作回调同一校验，不回落常规鉴权；操作者角色按 `AIOPS_IM_USERS` 映射判定（未映射＝viewer，仅 operator 可审批）；写操作复用 `_find_flow`（完整/唯一前缀匹配）+ `_write_guard`（阶段允许 + 防重复）与 kill switch（激活即拒绝）；
- **审计与可观测**：信号发出后 `invalidate_cache` 刷新缓存、`metrics.observe_gate_event` 计入闸门指标，操作审计 `actor=im:<operator>`、`action=card-callback:approval|second-approval:approve|reject`、`params.channel=feishu-card`；
- **与同类声明对齐**：操作者身份提取（`extract_callback_operator`）兼容 v2 `event.operator`（open_id/user_id/union_id）与 v1 顶层；响应层写入的 `wf_id` 即按钮回传值，网关内先经 `_find_flow` 解析为真实流程后再发信号。
- **客户端真实交互链路（2026-10-14 落地，长连接模式）**：发送侧新增应用机器人通道（`feishu_app.py`，三要素齐全时 `NotificationSender` 优先应用发卡——记录 `channel=feishu-app`，失败降级留痕且**不回落** webhook）；接收侧新增长连接网关 `feishu_gateway.py`（WSS 免公网订阅 `card.action.trigger`；`build_forward_payload` 以本进程 `AIOPS_FEISHU_VERIFY_TOKEN` 覆写 `header.token` 后转发 BFF 回调端点，3 秒时限内转发超时 2.5s，任何失败转 `toast.error` 不抛错；操作者 open_id 记入网关日志 `feishu_gateway.log`）；watchdog 条件托管（应用凭证齐全自动加挂为第 7 个组件，未配置默认仍 6 组件）；
- **上游缺陷规避（lark-oapi ≤1.7.4）**：官方长连接客户端 `_handle_data_frame` 丢弃 `MessageType.CARD` 帧（larksuite/oapi-sdk-python issue #126，表现为点击按钮报错 200340）——网关安装帧类型改写补丁（CARD 帧 `type` 头改写为 `event` 后交官方派发，路由依据 payload 内 `event_type="card.action.trigger"`，响应回写逻辑复用官方），进程内幂等安装，上游修复后可移除。

### 4.7 执行层（`release.py`）

- **发布编排 `run_canary`**：
  1. 复用沙箱工作区（已应用补丁的 demo-app 源码）作为构建上下文（不存在则重新 `prepare_workspace`）；
  2. `render_application`：渲染 ArgoCD Application manifest——labels 含 `patch-id/alert-id/service`，`spec.rollout` 含金丝雀 steps（`setWeight: 5 → pause: observe → setWeight: 100`）；
  3. `ArgoCDPublisher.apply`：配置 `AIOPS_ARGOCD_URL` 时真实 POST `/api/v1/applications`（Bearer token）；未配置留痕 `data/argocd/<app>.json`（recorded）；
  4. `docker build` 真实构建镜像（tag `aiops-demo-app:<patch_id>`，附 `aiops.version` label）；
  5. 启动金丝雀容器（随机端口、`BAD_CANARY=1` 可注入劣化）；
  6. 真实观测窗口：周期 POST `/order`（0.5s 间隔），实测错误率与 P99 延迟；
  7. SLO 判定：`error_rate ≤ 5%` 且 `p99 ≤ 500ms` → healthy；
- **发布终态 `run_finalize`**：
  - 达标：**真实滚动**——停旧稳定容器 → 以金丝雀镜像启动新稳定容器（`:18080`）→ 就绪校验（`/health` 200）→ 就绪前金丝雀保持运行（作为可用回退）→ 回收金丝雀；
  - 劣化：回收金丝雀容器，稳定版本保持不变（回滚语义）；`auto_rollback` 仅影响留痕文案（自动回滚 / 等待人工处理）；
- **版本号公式**：`release_version(patch_id) = v1.0.{stable_id(patch_id, 900) + 100}`，与用户公告（`notify_users`）使用**同一公式**，保证公告版本与实际发布版本一致；
- **生产对照**：manifest 由 ArgoCD 调谐到 K8s（Argo Rollouts 按 setWeight 分流）；本机以独立端口金丝雀容器 + 真实 HTTP 探测对齐同一语义。

### 4.8 MR 真实创建（`git_integration.py`）

`create_mr_for_patch(patch, test_report)` 按策略 `git:` 段（`repo_url` / `default_branch` / `mr_labels`）+ 环境变量 `AIOPS_GIT_TOKEN` 创建 MR/PR，三种返回语义（`mode` 字段）：

| mode | 触发条件 | 行为 |
| --- | --- | --- |
| `live` | 配置齐全且平台调用成功 | GitLab `POST /api/v4/projects/:id/merge_requests`（`PRIVATE-TOKEN`）；GitHub `POST /repos/:o/:r/pulls`（labels 经 issues API 补挂，失败不阻塞）；Gitee `POST /api/v5/repos/:o/:r/pulls`（access_token query） |
| `recorded` | 未配置 `repo_url` / token（演示默认） | 确定性桩结果，演示行为零变化 |
| `degraded` | 已配置但调用失败（HTTP/网络/非 JSON） | 桩 + `error` 原因；流程继续进入闸门 2（人工审批兜底） |

- 平台识别按 host 判定（含自建 GitLab/GitHub Enterprise，后者 API 根为 `/api/v3`）；源分支约定 `aiops/fix-<patch_id>`（本模块只建 MR，不推送代码）；
- 标题/描述由 `render_title` / `render_description` 生成：告警、补丁元数据（风险/置信度/模型）、改动文件、**完整 diff**、沙箱测试报告、回滚预案（与公告文案同源 `notify.ROLLBACK_PLAN`），审批者一站式查看；
- 标签 = 策略静态标签 + 动态三要素（`ai-fix:{alert_id}` / `model:{version}` / `confidence:{x}`）；
- 零新依赖（stdlib urllib），Temporal 沙箱内 activity 导入无额外负担。

### 4.9 需求分析闭环（`requirements_client.py` / `requirement_agent.py`）

被监控系统「需求收集与反馈」中经管理员评估纳入**需求基线**的条目，可开放给 AIOps 做主动分析（优先级研判 / 实现建议 / 排期参考），形成「分析结果查看 → 管理员反馈 → 再次分析 → 批准进入修复工作流」闭环：

- **拉取**（`requirements_client.fetch_requirements`）：`GET /api/requirements/export`（契约 v1.0，UTF-8 JSON，无 success 字段以 HTTP 状态码为准）；接口发现两种——服务清单 `requirements_path` 字段（推荐）或能力自描述；认证 `Authorization: Bearer <OPS_API_TOKEN>`（令牌经 `NL2SQL_OPS_TOKEN` 注入）；只做「拉取 + 校验」，失败（超时/非 JSON/401/结构不符）绝不抛异常；
- **分析**（`requirement_agent.analyze_requirement`）：需求原文 + 管理员历次反馈 + 上一版分析（迭代注入）+ Code RAG 引用 → 本地 LLM（JSON 模式，模型 `AIOPS_REQUIREMENT_MODEL`，缺省复用 triage 默认模型）→ pydantic 校验；失败降级为 degraded 分析（confidence=0），不抛异常；
- **安全侧原则**：分析结果只进入「展示 + 待批准」链路，LLM 不直接触发执行——能否进入修复工作流由管理员批准决定；
- **CLI**（`analyze_requirements.py`）：拉取 + 汇总（类型/优先级/版本分布、P0/P1 高优提示）→ Markdown 报告或 `--json` 输出；
- **控制台**：BFF `routes/requirements` 提供界面（应用选择/令牌指引/拉取透传），`bff/requirement_analyses.py` 双模存储分析闭环状态；数据源支持**跨系统聚合**（`app_id=all` 哨兵）——对全部启用应用 `asyncio.gather` 并行拉取、条目合并并以 `_source_app_id/_source_app_name/_source_service/_source_system` 标注来源（前端「来源系统」列 / 筛选 / 统计的口径），`sources` 逐系统状态 + `warnings` 中文提示实现**逐系统容错**（单系统失败不影响其余条目，全部失败才 `ok=false`）；聚合视图下智能分析 / 审批按条目来源应用归口（分析映射键 `app_id:entry_id`，防跨系统 id 撞车）。

---

## 5 数据与接口设计

### 5.1 数据模型（`models.py`）

| 模型 | 字段 | 说明 |
| --- | --- | --- |
| `Alert` | `alert_id, service, severity="critical", description=""` | 流程唯一入口 |
| `RootCause` | `error_type, suspect_files[], confidence, summary` | LLM 结构化根因 |
| `Patch` | `patch_id, alert_id, files[], diff, description, risk, model_version, confidence` | 最小补丁 + 可追溯元数据 |
| `TestReport` | `patch_id, passed, unit_tests, regression_tests, sast, details` | 沙箱测试报告 |
| `CanaryResult` | `patch_id, traffic_percent, healthy, error_rate, p99_latency_ms, observation` | 金丝雀观察实测 |
| `ReleaseResult` | `version, rolled_back, reason` | 发布终态 |

全部为 dataclass，保证 Temporal 序列化兼容（JSON 可编码）。

### 5.2 工作流信号与查询接口

| 接口 | 类型 | 参数 | 语义 |
| --- | --- | --- | --- |
| `submit_approval` | signal | `decision: approve/reject` | 一级审批；超时 30m 自动驳回 |
| `submit_second_approval` | signal | `decision: approve/reject` | 受保护目录二级审批 |
| `submit_deploy_command` | signal | `command: deploy_now/cancel` | 窗口内加速 / 取消 |
| `submit_patch` | signal | `workflow_id, alert: Alert` | 窗口内新补丁仅 FIFO 入队（不重置计时、不合并、无上限） |
| `status` | query | — | 返回 `{stage, approval, second_approval, deploy_command, queued_patches[{workflow_id, alert_id}]}` |

### 5.3 外部系统接口契约

| 外部系统 | 方向 | 接口 | 关键约定 |
| --- | --- | --- | --- |
| Alertmanager → 接入服务 | 入 | `POST /webhook`（v4 载荷） | 仅 firing；幂等 workflow id；5xx 触发重试 |
| 突增检测器 → Alertmanager | 出 | `POST /api/v2/alerts` | `LogErrorSurge` + `aiops_demo="true"` 标签路由 |
| 接入服务/突增检测器 → Loki | 出 | push / query_range / query API | 流标签 `service`/`level`；行内 `trace_id` |
| triage/fix → Ollama | 出 | `POST /api/chat`（JSON 模式） | `think=false`，`temperature=0.2`；超时 45s |
| code_rag → Ollama | 出 | embeddings API | `bge-m3:latest`，1024 维 |
| notify → 飞书/钉钉 | 出 | 群机器人 webhook POST | 加签：飞书秒级补签、钉钉毫秒级签名（query 参数） |
| 飞书卡片回调 → BFF 网关 | 入 | `POST /api/im/card-callback`（v2 `card.action.trigger`） | 验签 `AIOPS_FEISHU_VERIFY_TOKEN`（未配置 503 / 不符 401）；200 响应 `{toast, card}`（处置结果卡）；契约 `{wf_id, gate, decision}`；非法即拒；长连接模式下由 `feishu_gateway.py` 注入共享凭证转发；见 4.6.1 |
| release → ArgoCD | 出 | `POST /api/v1/applications` | Bearer token；失败抛错走 activity 重试 |
| release → 应用容器 | 出 | `GET /health`、`POST /order` | 探活与观测请求（真实 HTTP） |
| 沙箱 → 运行时 | 出 | Docker CLI / K8s API | 隔离参数见 4.4 |

### 5.4 数据资产（`data/` 目录）

| 产物 | 路径 | 说明 |
| --- | --- | --- |
| 代码索引 | `data/code_index.json` / `data/code_index.faiss` | 33 chunks + 6 tickets，1024 维（demo-app 实测） |
| 历史工单 | `data/historical_tickets.json` | 6 条样例工单（RAG 语料） |
| 通知留痕 | `data/notify/<msg_id>.json` | 每张卡片一份（approval/broadcast/escalation-*） |
| ArgoCD 留痕 | `data/argocd/demo-app-<patch_id>.json` | 每次渲染的 Application manifest |
| 沙箱工作区 | `data/sandbox/<patch_id>/` | 应用补丁后的源码（构建上下文复用） |
| 评测报告 | `data/triage_eval_report.json` / `data/fix_eval_report.json` | 离线评测结果 |
| 评测工作区 | `data/sandbox-eval/<patch_id>/` | 评测专用沙箱工作区（与主流程隔离） |

### 5.5 幂等键与命名约定

- workflow id：`aiops-fix-{service}-{alert_id}`（webhook 与 `demo_cli` 同一公式）；
- 补丁 id：`p-{alert_id}-r{attempt}`；金丝雀容器：`aiops-canary-{patch_id}`；稳定容器：`aiops-stable-order`（`:18080`）；镜像：`aiops-demo-app:{patch_id}`；通知留痕：`approval-{patch_id}` / `broadcast-{patch_id}` / `escalation-{alert_id}`；
- 版本号：`v1.0.{stable_id(patch_id,900)+100}`（公告与发布共用）。

---

## 6 关键设计决策与容错机制

### 6.1 安全侧降级链（全链路）

| 故障点 | 降级行为 | 最终效果 |
| --- | --- | --- |
| Loki 不可用 | `collect_evidence` 返回空证据（`loki_available=false`） | LLM 证据不足 → 低置信度 → 闸门 1 转人工 |
| LLM triage 异常 | `fallback_root_cause`（confidence=0.0） | 闸门 1 转人工 |
| 修复 LLM 异常 | 兜底补丁（degraded 留痕） | 进入沙箱大概率被拦截 → 回炉/转人工 |
| Code RAG 不可用 | 空引用（基于目标文件仍可生成） | 流程继续 |
| 沙箱运行时不可用 | `TestReport.passed=False` | 回炉重试 → 耗尽转人工 |
| 通知投递失败 | `failed` 模式 + 留痕兜底 | 修复主流程不中断 |
| ArgoCD 未配置/提交失败 | 留痕模式 / 抛错重试 | 本机演示语义一致；生产失败触发 activity 重试 |

**原则**：观测面、推理面故障一律降级并放大不确定性（转人工）；执行面故障（沙箱、发布）不得静默通过。

### 6.2 LLM 生成 unified diff 的应用管线容错

实测发现 LLM 复制上下文行时会改写**行内空白**（如注释前对齐空格 2→3 个），导致严格匹配失败、补丁被丢弃。管线设计（`_match_context`）：

1. 严格相等优先；
2. 不相等时：**前导缩进必须逐字符一致**（缩进参与 Python 语义，不放宽）；
3. 其余差异经 `" ".join(line.split())` 折叠后相等 → 视为匹配；
4. 匹配的上下文行输出**源文件原文**（而非 LLM 改写版），保证未修改行零污染；
5. 应用后仍须通过 `py_compile` 编译校验才进入沙箱。

实测效果：启用容错后 27b 模型对 F02 样本（畸形令牌）的 diff 应用成功率由 0 提升到 100%。

### 6.3 金丝雀发布安全顺序

`promote` 的真实滚动顺序为：**停旧稳定 → 起新稳定 → 就绪校验（/health 200）→ 回收金丝雀**；就绪校验通过前金丝雀保持运行（作为可用回退）；新稳定未就绪则清理新容器并抛错（交由 activity 重试）。劣化路径只回收金丝雀，绝不动稳定版本。

### 6.4 幂等与防重

| 场景 | 机制 |
| --- | --- |
| 重复告警投递 | workflow id 幂等（REJECT_DUPLICATE + FAIL），`duplicates` 返回不重开 |
| 重复启动同名容器 | `start_canary`/`promote` 前先 `stop` 同名容器（幂等防重） |
| 幽灵审批信号 | 进入 `WAIT_APPROVAL` 前清空历史审批/二级审批信号 |
| 未知窗口指令 | 忽略并继续等待剩余倒计时（不重置计时） |
| 同服务告警风暴 | 突增检测器 300s 冷却 + Alertmanager 分组（复用栈配置） |

### 6.5 FIFO 队列与周期承接

窗口内新补丁仅入队；当前流程结束（DONE/ESCALATED/CANCELLED）后由 `_start_next_cycle` 取队首补丁启动**子工作流**（`parent_close_policy=ABANDON`，父流程关闭不影响子流程执行），实现「队首补丁自动开启新周期」。

---

## 7 非功能性设计

| 维度 | 设计 |
| --- | --- |
| 可靠性 | Temporal 持久化（定时器/信号/重试）；策略启动快照；活动重试 3 次指数退避；队列任务不丢 |
| 可观测性 | `status` 查询实时阶段；`gate_events` 全事件流；各环节日志留痕（evidence/cluster/triage/rag/fix/sandbox/notify/canary/release）；data/ 全量审计产物 |
| 确定性 | 工作流内不读文件、不直接调用非确定性函数；策略经 `snapshot_for_workflow` 纯 JSON 传入；重依赖 activity 内延迟导入 |
| 性能 | 金丝雀活动超时放宽至 600s 覆盖构建+观测；LLM 调用带超时（45s/可调）；日志聚类采集侧完成降低 LLM 输入量；FAISS 精确检索满足演示规模 |
| 扩展性 | 新闸门=新状态+signal；新服务接入=配告警规则（零代码）；向量后端可切换（auto/faiss/python）；模型可换（AIOPS_*_MODEL） |
| 安全 | SAST 拦截高危 diff；沙箱禁网/只读/限额/非 root；受保护目录二级审批；补丁元数据全量留痕；AI 禁止顺手重构（prompt 约束） |

---

## 8 质量保障与评测体系

### 8.1 单元测试矩阵（2026-10-14 回归复核：49 个测试文件 / 1331 用例通过、跳过 6）

| 测试文件 | 覆盖范围 |
| --- | --- |
| `tests/test_policy.py` | 策略校验（锁定项拒绝、时长解析、快照结构） |
| `tests/test_webhook.py` | 载荷标准化、幂等键、resolved 忽略 |
| `tests/test_logs_triage.py` | Drain3 聚类、突增判定、triage schema/降级 |
| `tests/test_code_rag_fix.py` | AST 切块、检索排序、diff 解析/应用/容错/编译校验 |
| `tests/test_vector_backend.py` | FAISS 一致性、陈旧索引回退、依赖缺失回退、修复模型选择 |
| `tests/test_sandbox.py` | SAST 规则、工作区、unittest 解析、Runner 语义 |
| `tests/test_release.py` | manifest 渲染、观测判定、promote/回滚、版本号 |
| `tests/test_notify.py` | 卡片结构、加签、投递模式、回调解析（v2/旧版）、操作者提取、处置结果卡 |
| `tests/test_auth.py` / `test_bff.py` | 令牌鉴权/轮换/宽限、BFF 路由与阶段校验、审计读写、回调网关（豁免/三态/审批防重/OpenAPI） |
| `tests/test_mode_db.py` / `test_store_db.py` | 双模判定、fail-fast、DB 引擎与建表、存储分派双后端 |
| `tests/test_metrics.py` / `test_audit_report.py` | 指标口径（标签集/状态字符串化）、指标服务、审计聚合报表 |
| `tests/test_monitored_apps.py` | 应用清单 CRUD 双后端 |
| `tests/test_kill_switch.py` / `test_cleanup.py` | 紧急停止（fail-closed）、TTL 清理与 dry-run |
| `tests/test_git_integration.py` / `test_qoder_fix.py` | MR 三平台创建/降级、Qoder 修复接入 |
| `tests/test_ratelimit.py` / `test_ship_app_logs.py` | 限速窗口（Redis/进程内）、日志采集器（清单模式/位点） |
| `tests/test_watchdog.py` / `test_page_health.py` / `test_app_prober.py` | 组件守护（冷却/拉起/档位锁定/单实例锁）、通用白屏判据、可用性巡检（防抖/告警/状态落盘） |
| `tests/test_requirements.py` / `test_bff_requirements.py` / `test_requirement_analyses.py` / `test_requirement_agent.py` / `test_requirement_workflow.py` | 需求基线：客户端/域路由/分析闭环存储/分析器降级/工作流信号兼容 |
| `tests/test_escalations.py` / `test_auth_users.py` / `test_ops_metrics.py` | 转人工处置闭环（SLA 巡检）、用户与会话（登录/改密/吊销）、运营度量（MTTR/比率） |
| `tests/test_app_manifest.py` / `test_changes.py` / `test_release_direct.py` / `test_worker_registry.py` | Manifest 校验与探测、变更关联、直连发布、活动注册表防回归 |
| `tests/test_deploy_manifests.py` | K8s 清单 39 项静态校验（命名空间/镜像/副本/PDB/HPA/探针/PVC/策略/告警名集合对齐）+ compose profiles + Dockerfile |

> 2026-10-13 单机生产化整改后全量回归复核：**Ran 1290 tests — OK（skipped=6）**（49 个测试文件；发现式运行必须带 `-t .`）。整改新增套件包括：`test_production_safety` / `test_chatops` / `test_mcp_server` / `test_alert_store` / `test_release_safety` / `test_release_replay` / `test_backup` / `test_readiness` / `test_startup_mode` / `test_production_deploy` / `test_topology`。同日 IM 流程通知补齐（接收/需求接收/进入修复，activity 13→16）后同口径复核通过；`test_notify` / `test_worker_registry` / `test_release_replay` 覆盖新卡片、活动注册与重放守护。同日生产档切换全链路执行（`release-gate-policy.yaml` 显式 `shadow_mode: false`；统一启动门禁改为「档位必须显式声明」，缺失即拒绝启动）后再次全量复核通过；`test_startup_mode` / `test_readiness` 按放行/拦截双侧更新。同日影子建议续接落地（`resume_patch` 快照键 + `resume-shadow` 端点 + 发布层门禁按档位判定，见 3.1）后再次全量复核：**Ran 1305 tests — OK（skipped=6）**；`test_requirement_workflow`（续接纯函数）/ `test_requirement_analyses`（`apply_resumed`）/ `test_bff_requirements`（端点分支）/ `test_release_safety`（门禁双侧）覆盖。2026-10-14 飞书卡片回调网关落地（审批交互闭环）后全量复核：**Ran 1331 tests — OK（skipped=6）**；新增 26 用例分布于 `test_notify`（v2 回调解析 / 操作者提取 / 真实 wf_id 注入 / 处置结果卡渲染）与 `test_bff`（middleware 豁免守卫 / 端点三态响应 / 双级审批 / 防重复点击 / 旧版 v1 兼容 / OpenAPI 注册）。

### 8.2 triage 离线评测（`eval_triage.py`）

- 样本集：`data/triage_eval_samples.json`（6 条，含高证据/低证据样本）；
- 指标口径：schema 通过率（输出合法）、Top-1 命中率（首个嫌疑文件命中）、低置信拦截率（低证据样本 100% 转人工）、正确率（综合判定）；
- 实测（qwen3:8b）：schema 通过率 1.0、Top-1 命中率 1.0、低置信拦截率 1.0、综合正确率 1.0，平均单样本 1.18s。

### 8.3 修复离线评测（`eval_fix.py`，双模型对比）

- 样本集：`data/fix_eval_samples.json`（F01 空优惠券防护、F02 畸形令牌容错）；
- 评测链路：真实 `run_fix`（LLM 生成 → 应用 → 编译校验）+ 真实沙箱（Docker 隔离执行单测）；
- **判定口径**：`schema_pass` = 校验链全过且非 degraded；`sandbox_pass` = 目标用例转为通过 **且** 无新增失败（允许基线已知失败用例存在，避免单缺陷样本被其它注入缺陷连坐）；
- 实测对比：

| 模型 | schema 通过率 | 沙箱通过率 | 平均耗时 |
| --- | --- | --- | --- |
| qwen3:8b | 0.5 | 0.0 | 2.29s |
| qwen3.8:27b-mlx | 1.0 | 1.0 | 6.62s |

> 结论：大模型在 diff 语义正确率上提升显著（0.0 → 1.0），验证了 `AIOPS_FIX_MODEL` 模型切换通道的价值。2026-10-09 起 triage 与修复默认模型已切换为 `qwen3.8:27b-mlx`（上表 8b 行为历史基线记录）。

### 8.4 端到端验收证据（wp9-final-1 实测）

- 全链路耗时 71.59s（含 attempt0 拦截 → attempt1 通过的回炉过程）；
- `gate_events` 完整：`tests:failed:attempt=0` → `tests:passed:attempt=1` → `mr:created:!2314` → `gate2:approved` → `gate3:announced:v1.0.514` → `gate3:command:deploy_now` → `canary:healthy->full-rollout`；
- 金丝雀实测：20 次真实请求，错误率 0.00%、P99 10ms；stable 容器 `APP_VERSION=v1.0.514` 核验一致；
- 留痕：`data/notify/approval-p-wp9-final-1-r1.json`、`data/argocd/demo-app-p-wp9-final-1-r1.json`、`data/sandbox/p-wp9-final-1-r1/`。

---

## 9 已知边界与演进路线

| 项 | 现状 | 演进方向 |
| --- | --- | --- |
| MR 创建 | **已实现**三平台真实创建（GitLab/GitHub/Gitee）+ 降级链（见 4.8） | 按需扩展 Bitbucket 等平台 |
| K8s 沙箱 | manifest 渲染就绪，本机用 Docker 对齐 | 集群接线：apply Job → 轮询状态 → 读 pod 日志 |
| WebSocket 公告 | 卡片投递 + 留痕（语义对齐） | 接真实 WS 网关向在线会话推送 |
| 回调服务端 | **已落地**（2026-10-14）：飞书卡片回调网关 `POST /api/im/card-callback`（应用机器人发卡 + 长连接网关 `feishu_gateway.py` 免公网接收回调），含验签/角色/写守卫/kill switch/审计全链（见 4.6.1） | 钉钉按钮回调网关切换：actionURL 契约经外置转发器至等价校验后发信号 |
| 向量库 | FAISS（精确检索，演示规模充分） | 数据规模增长后升级 Milvus 等托管向量库 |
| 修复引擎 | 默认 Ollama（模型随 triage，`qwen3.8:27b-mlx`） | 可切 `AIOPS_FIX_PROVIDER=qoder`（CLI 无头）/ `AIOPS_FIX_MODEL` 换模 |
| 感知数据源 | 复用本机监控栈（Loki/Alertmanager） | 接生产 Prometheus/OTel/Filebeat，接口不变 |
| 生产持久化 | MySQL 5 表 + 双模运行已落地（见第 10 章） | 大规模场景评估读写分离 / 分区表 |
| 多副本部署 | K8s 2 副本 + PDB + HPA + 共享 Redis 限速 | 按负载调整 HPA 阈值与副本上下限 |
| 数据生命周期 | TTL 清理（CronJob 每日）+ Temporal retention 720h | 按合规要求调整保留期 |

---

## 10 双模架构与生产化设计（v2 增补）

### 10.1 设计目标与原则

| 原则 | 落地方式 |
| --- | --- |
| 单一代码库双模 | `AIOPS_MODE` 决定持久化后端；演示与生产共用全部业务代码与流程 |
| 历史数据零迁移 | demo 保持 `data/` 文件后端原样；production 用全新 MySQL 库 |
| 默认安全侧 | production 为**默认模式**且 DB 未配置即 fail-fast；不确定一律拒绝 |
| 演示不受影响 | demo 显式声明（`.env` / `demo-up.sh`）；测试 conftest 强制 demo |

### 10.2 模式层（`mode.py`）

- `mode()`：读 `AIOPS_MODE`，缺省 = `production`，非法值抛 `ModeConfigError`；
- `db_url()`：demo 恒为 `None`（演示永不触达数据库）；production 未配置 `AIOPS_DATABASE_URL` 时**抛错拒绝启动**（`worker.py` / `bff/app.py` / `demo_cli.py` 启动入口转 fail-fast），杜绝"以为落库、实际静默丢文件"；
- `data_root()`：`AIOPS_DATA_ROOT` 优先；否则 demo → `data/`、production → `data/prod/`（索引/沙箱/留痕等非结构化产物两档隔离）。

### 10.3 数据层（`db.py`，MySQL 8.0）

引擎：SQLAlchemy 2.0 + PyMySQL，`pool_recycle=3600` + `pool_pre_ping=True` + `pool_size=5`；首次连接自动 `metadata.create_all`（幂等）。五张表：

| 表 | 主键 | 关键列 | 用途 |
| --- | --- | --- | --- |
| `system_kv` | `kv_key` | `kv_value`(JSON 文本) / `updated_at` | 系统开关（kill switch 状态） |
| `console_tokens` | `id`(BIGINT 自增) | `token_hash`(sha256, unique) / `user_name` / `role` / `state`(active/previous/revoked) / `expires_at` / `rotated_from` | 令牌注册表（仅存指纹，不存明文） |
| `monitored_apps` | `id`(字符串) | `name` / `url` / `service` / `log_path` / `enabled` / `note` | 被监控应用清单 |
| `audit_events` | `id`(BIGINT 自增) | `ts` / `actor` / `action` / `workflow_id` / `target` / `detail`(JSON) / `result` / `mode` | 操作审计（索引 ts/wf/actor） |
| `workflow_runs` | `workflow_id` | `run_id` / `stage` / `alert`(JSON) / `confidence` / `patch_id` / `duration_seconds` / `result`(JSON) / `updated_at` | 流程执行速查（跨实例一致） |

时间语义统一：`now_dt()`（naive UTC DATETIME）与 `now_iso()`（带时区 ISO 串）双口径；MySQL 严格模式约束已知并适配（如 `TEXT` 列不给 `server_default`）。

### 10.4 双后端存储分派

同一接口，两档实现（调用方无感知）：

| 域 | 模块 | demo | production |
| --- | --- | --- | --- |
| 操作审计 | `bff/audit.py` | `data/web-audit/audit-*.jsonl` 追加 | `audit_events` 表（`ORDER BY id DESC` 读） |
| 令牌注册表 | `bff/auth.py` | `data/console_tokens.json`（明文键，文件优先于 env 播种） | `console_tokens` 表（**sha256 指纹**为键，鉴权二次匹配 `_token_key`） |
| 应用清单 | `bff/monitored_apps.py` | 清单 JSON 文件 | `monitored_apps` 表（写入恒显式赋值，含 `note` 空串） |
| 流程速查 | `bff/runs_store.py` | 不启用（直读 Temporal） | Temporal List → `workflow_runs` upsert（`sync_runs`），终态触发指标观测 |
| kill switch | `aiops_agent/kill_switch.py` | `data/kill_switch.json`（原子写 tmp→replace） | `system_kv`（跨副本一致） |

### 10.5 安全加固

- **令牌安全**：注册表仅存 sha256 指纹；轮换为平滑替换（旧令牌转 `previous` + 宽限期）；令牌值默认不回显（`AIOPS_TOKEN_ALLOW_REVEAL` 显式开启）。
- **限速**（`bff/ratelimit.py`）：token / IP 双维度滑动窗口——读 240/min、写 20/min、认证失败 10/min；Redis 可用时存 `aiops:rl:tok:<fp>:<kind>` / `aiops:rl:authfail:<ip>`（多副本共享配额），否则退化为进程内窗口；超限统一 429。
- **安全响应头**（middleware）：CSP / X-Frame-Options / nosniff 恒开；HSTS 仅在 HTTPS 或 `X-Forwarded-Proto: https`（TLS 终结代理）时下发。
- **kill switch 检查点**：BFF 集中式中间件拦截一切 `/api` 写操作（管理端点除外）+ `alert_webhook` 启动前检查（激活时 503）；状态不可读 fail-closed。**刻意不在 workflow 编排层检查**——避免 Temporal replay 不确定性。

### 10.6 可观测设计

- **指标口径（`aiops_agent/metrics.py`，契约稳定）**：计数标签一律字符串——`stage` 大写（DONE/ESCALATED/CANCELLED）、布尔小写（"true"/"false"）、BFF `status` 为 `str(int)`、闸门 `gate` ∈ approval / second_approval / deploy_command；直方图覆盖 workflow 时长、BFF 请求耗时、金丝雀观测。
- **暴露面**：BFF `GET /metrics`（`127.0.0.1:8600`，无鉴权但**仅接受 loopback 对端**）；worker 独立指标服务默认仅 loopback（production `:9091` / demo `:9090`，`AIOPS_METRICS_PORT=0` 关闭）；`/readyz`、`/livez` 同为仅回环（探针口径见 10.10）。
- **告警**：`deploy/prometheus/aiops-alerts.yml` 6 条（成功率、卡单、耗时、BFF 错误率/延迟、限速触发），与 K8s `PrometheusRule` 同步（测试校验名称集合一致）。
- **看板**：`deploy/grafana/aiops-overview.json` 9 面板，含 `mode` 模板变量区分两档。
- **追踪**：`AIOPS_OTEL_ENDPOINT` 配置后 BFF HTTP 与 worker workflow/activity 自动生成 span；未配置完全 no-op。

### 10.7 数据清理（TTL）

保留期（生产 / 演示）：审计与执行记录 365/30 天、沙箱与 Qoder 工作区 7/1 天、通知与 ArgoCD 留痕 90/7 天。安全约束：工作区删除仅限指定根目录直接子目录；文件域仅匹配固定模式；dry-run 只统计；表行删除仅在 production 执行；单域失败不阻塞其余域（错误进 `errors` 列表并反映为 CLI 非零退出码）。

### 10.8 BFF 分层

`bff/routes/`（按域拆分为 14 域：`health` / `flows` / `approvals` / `chatops` / `im_callback` / `escalations` / `ops_metrics` / `audit` / `tokens` / `system` / `systems` / `monitored_apps` / `requirements` / `users`）+ `services` 聚合逻辑（`aggregator` / `temporal_gateway` / `runs_store`）+ `middleware`（鉴权/限速/指标/安全头）+ `deps`（依赖注入）。写操作路由保持 stage 前置校验（不满足 409）与操作审计钩子；前端 SPA（`web/`）由 BFF 同源托管，`--host 0.0.0.0` 时局域网可访问。

### 10.9 部署面

`deploy/`：多阶段 `Dockerfile`（非 root / 无密钥）、`k8s/` 平铺 12 文件 38 资源（命名空间 / ConfigMap / Secret 样例 / MySQL / Redis / Temporal / worker / bff / webhook / CronJob / NetworkPolicy / ServiceMonitor+PrometheusRule）、`prometheus/`、`grafana/`；单机整改新增 `deploy/caddy/Caddyfile`（本机 HTTPS 反代）、`deploy/temporal/`（生产配置 / bootstrap.sql / schema.sh）、`deploy/launchd/`（watchdog / caddy / backup 三个 plist 模板）、`deploy/ci/compose.yml`（隔离集成环境）——均为 E/F/H/I 产物；**2026-10-13 维护窗口已投用**（Caddy/watchdog launchd 托管、8643 三向入口、监控接线、真实加密备份与 W1 冷备；备份调度 plist 已注册为禁用态待租约协调）；`docker-compose.yml` 以 profiles 增量交付（默认行为与改造前完全一致：仅 temporal + loki；`--profile prod` 加 MySQL/Redis；`--profile obs` 加 Prometheus/Grafana）。

### 10.10 单机生产化整改设计增量（2026-10-13，批次 A–I）

在 v2 双模基础上，按《单机生产化整改实施计划》完成单机受控生产化；批次状态与证据标记见《实施计划》第 13 节，部署操作见《部署手册》第 9/10 章。**维护窗口（2026-10-13）已执行切换与运行验收，状态与残余待办见《部署手册》10.6。**设计增量：

- **事件聚合（C）**：告警事件跨进程持久化与去重（`aiops_agent/alert_store.py`，故障域键 = `{mode}:{namespace}:{service}`，成员唯一约束防重复投递，条件 UPDATE + 租约认领支持崩溃接管，Temporal 调用一律在事务外）；新增 `alert_events` / `alert_event_members` 表与迁移。
- **ChatOps 信任边界（B）**：`POST /api/im/command` 走 middleware 专用通道认证（`AIOPS_IM_TOKEN`；未配置＝通道关闭、凭证不符＝认证失败，两者都不回落常规鉴权；production 未接可信网桥前整体关闭），路由内二次核对 `token_record.source == 'im'`；发送者角色按 `AIOPS_IM_USERS` 映射，未映射＝viewer。MCP production 仅支持 stdio 且逐调用最小权限凭证。`/metrics`、`/readyz`、`/livez` 收敛为仅 loopback 对端。
- **发布互斥与急停（G）**：`release_journal.py` 文件锁 + 持久化发布记录锁定真实目标资源；急停与失败恢复沿用 kill switch 与 journal 非终态记录处理。
- **统一启动（D）**：`config.startup_snapshot` 对 production 强制全组件 loopback、端口不得重复、venv/策略档位显式声明（`shadow_mode` 键必填）等 fail-fast 门禁；演示工具在 production 一律拒绝（如 `demo_log_generator`）。
- **HTTPS 与网络收敛（E/F）**：本机 Caddy（8643）反代 8600 / 8099 / 指标通道，清除客户端转发头、只用真实 TCP 对端重建身份；HSTS 仅在受信代理转发 https 时下发；正式 Temporal Server 使用独立业务库 / visibility 库与生产 namespace。
- **备份与恢复（H）**：AES-256-GCM 加密归档 + 定向隔离恢复校验；备份与发布共用同一 `release_lock` 并在持锁后执行 `assert_idle_locked()`（见《部署手册》10.2）。K8s 探针契约同步为 exec（I），清单仅静态校验、未集群实测。

---

## 附录 A 演示分支矩阵

触发方式：`Alert.description` 携带关键词（`demo_cli start --description "..."` 或 webhook 告警描述）。

| 关键词 | 期望链路 | 终态 |
| --- | --- | --- |
| （无） | 正常修复 → 审批 → 公告 → 金丝雀健康 → 全量 | DONE |
| `low-conf` | 置信度 0.55 → 闸门 1 拦截 | ESCALATED |
| `protected` | 嫌疑文件 `auth/token_service.py` → 二级审批 | DONE / ESCALATED |
| `test-fail` | attempt0 注入错误候选 → 拦截；attempt1 通过 | DONE |
| `test-always-fail` | 3 次尝试全失败 | ESCALATED |
| `canary-bad` | 金丝雀注入 50% 失败 + 500ms 延迟 → 劣化 | ESCALATED（回滚） |

## 附录 B gate_events 事件字典

| 事件 | 含义 |
| --- | --- |
| `gate1:blocked:confidence=x<y` | 闸门 1 拦截 |
| `tests:failed:attempt=n` / `tests:passed:attempt=n` | 沙箱测试结果（按尝试轮次） |
| `mr:created:!xxxx` | MR 已创建 |
| `gate2:approved` / `gate2:reject` / `gate2:timeout->reject` | 一级审批结果 |
| `gate2b:approved:protected-dir` / `gate2b:reject` / `gate2b:timeout->reject` | 二级审批结果 |
| `gate3:announced:vX.Y.Z` | 用户公告已推送（含版本号） |
| `gate3:command:deploy_now` / `gate3:command:cancel` / `gate3:cancelled` | 窗口指令与取消 |
| `gate3:patch-queued:<alert_id>` | 窗口内新补丁入队 |
| `queue:next-cycle-started:<alert_id>` | 队首补丁开启新周期 |
| `canary:healthy->full-rollout` / `canary:degraded->rollback` | 发布终态 |

## 附录 C 阶段（stage）字典

| stage | 含义 | 后续 |
| --- | --- | --- |
| DETECTED | 已接收告警 | TRIAGING |
| TRIAGING | 证据采集/聚类/根因 | FIXING 或 ESCALATED |
| FIXING | 补丁生成 | TESTING |
| TESTING | 沙箱验证 | WAIT_APPROVAL 或回炉/ESCALATED |
| WAIT_APPROVAL | 等待审批（含二级） | NOTIFYING 或 ESCALATED |
| NOTIFYING | 公告 + 倒计时 | CANARY 或 CANCELLED |
| CANARY / ROLLING_OUT | 金丝雀观测 / 发布终态 | DONE 或 ESCALATED |
| DONE / ESCALATED / CANCELLED | 终态（审计记录输出） | — |
