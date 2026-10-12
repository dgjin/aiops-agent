# AIOps 自动运维智能体前端管理应用设计方案

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.0 |
| 日期 | 2026-10-01 |
| 状态 | 待评审 |
| 应用定位 | 运维控制台（流程看板 + 审批中心 + 发布窗口 + 审计回看） |
| 技术栈 | React 18 + Vite + TypeScript + Tailwind CSS；BFF：FastAPI（Python 3.12） |
| 关联文档 | 《系统设计说明书》、《用户操作手册》、《部署手册》 |

---

## 1 背景与目标

### 1.1 背景

AIOps 智能体后端链路已完整落地（五层架构 + 三道闸门 + 真实沙箱/发布/通知，123 单测与 E2E 验证通过）。当前人机交互入口有三类，但均不足以支撑日常值班运维：

| 现有入口 | 能力 | 局限 |
| --- | --- | --- |
| `demo_cli.py` | 全操作（启动/审批/窗口/查询/结果） | 命令行，非技术人员难用；无全局视图 |
| IM 卡片（飞书/钉钉） | 审批、公告的轻交互 | 无流程列表与历史；受保护目录二级审批链路不直观；卡片易被消息流淹没 |
| Temporal Web UI（:8233） | 通用工作流调试 | 无业务语义（不呈现闸门/阶段/根因/diff/测试报告） |

**缺口**：值班人员需要「一屏看全所有流程 → 点开看证据链 → 就地完成审批与窗口操作 → 事后可回查审计」的业务化控制台。

### 1.2 目标

1. **全局可视**：进行中/已完成流程一屏聚合，实时展示阶段（stage）、闸门状态与关键指标；
2. **证据链完整**：流程详情呈现根因、补丁 diff、测试报告、gate_events 时间线、审批记录、发布结果；
3. **操作闭环**：审批（一级/二级）、发布窗口（deploy-now / cancel / queue-patch）在控制台内完成，**与 IM 卡片共用同一套 Temporal 信号**，双通道天然一致；
4. **审计可回看**：历史流程审计记录归档查询；所有控制台操作落操作审计；
5. **高质量深色主题**：面向 7×24 值班场景的深色控制台设计（降低夜间视觉疲劳）。

### 1.3 非目标

- 不替代 IM 卡片：审批保持「IM / Web 双通道」，最终收敛到同一信号；
- 不做策略热编辑：策略仍为启动快照，控制台**只读展示**当前策略；变更走 YAML 评审 + 重启；
- 不做代码开发/合并工作台：MR 相关仍交给 GitLab/GitHub；
- 不直连 Temporal Web 端口：浏览器不接触 Temporal 裸接口（由 BFF 聚合）。

---

## 2 用户与角色

### 2.1 角色定义

| 角色 | 人员 | 核心诉求 | 控制台权限 |
| --- | --- | --- | --- |
| 操作员 operator | 运维值班 | 全局态势、窗口操作、关注异常流程 | 全部读 + 窗口操作 + 审批 |
| 审批人 approver | 运维主管 / 二级审批人（auth/payment/crypto 责任岗） | 快速审完待办、看懂证据 | 全部读 + 审批（一级/二级） |
| 观察者 viewer | 研发 / 管理者 / 演示观众 | 看现状与历史，不承担操作 | 只读 |

> 演示环境（单机）默认单用户免登录；生产环境接入 SSO 并绑定角色（见 5.5）。

### 2.2 核心场景走查

**场景 A（值班值守）**：交接班时打开看板 → 一眼看到 3 个进行中流程（1 个在 NOTIFYING 倒计时、1 个在 WAIT_APPROVAL 待审、1 个金丝雀观测中）→ 对 NOTIFYING 流程评估后点击「立即发布」或「取消」。

**场景 B（审批）**：审批中心显示 2 条待办（含 1 条受保护目录二级审批，倒计时 12:47）→ 点开详情核对根因与 diff → 点击「批准」→ 页面即时刷新为已审批。

**场景 C（事后复盘）**：搜索 alert_id 或服务名 → 打开历史流程 → 查看 gate_events 时间线（含 attempt0 失败 → attempt1 通过的回炉过程与金丝雀实测数字）→ 导出审计 JSON。

**场景 D（答辩/演示）**：投屏控制台，用另一个终端执行 `demo_cli.py start --description "test-fail 演示"` → 大屏实时呈现 `FIXING → TESTING` 阶段流转与 attempt 变化。

---

## 3 功能需求

功能围绕五个模块（M1–M5）与一个辅助模块（M6）：

### 3.1 M1 流程看板（Dashboard）

| # | 功能点 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| M1-1 | 进行中流程卡片墙 | 每个流程一张卡片：服务名、alert_id、stage 徽标、审批状态、耗时、最新事件 | Temporal `list_workflows` + 每家 `status` query |
| M1-2 | 关键计数概览 | 进行中 / 待审批 / 倒计时中 / 今日完成 / 今日转人工 | 列表聚合 |
| M1-3 | 终态分布（近 7 日） | DONE / ESCALATED / CANCELLED 占比条 | 历史结果聚合（BFF 缓存） |
| M1-4 | 快速操作 | 卡片内直接对待审批/倒计时流程弹操作（复用 M3/M4 操作组件） | 同上 |

### 3.2 M2 流程详情（Flow Detail）

| # | 功能点 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| M2-1 | 概览头 | 服务、alert_id、wf-id、stage 徽标、总耗时、当前闸门状态 | `status` query |
| M2-2 | 根因卡片 | error_type、confidence（阈值对比可视化）、summary、suspect_files | `result`（终态）或运行中不可得→ 显示"进行中，完成后回填"（见 8.1） |
| M2-3 | 补丁卡片 | patch_id、模型版本、风险等级、文件列表、**diff 查看器**（+/- 行高亮） | `result.patch_id` → BFF 读 `data/sandbox/<patch_id>/` 与工作流结果聚合（见 8.1） |
| M2-4 | 测试报告卡片 | 单测/回归/SAST 三项 + 失败用例名；attempt 轮次标注 | 同上 |
| M2-5 | gate_events 时间线 | 全部闸门事件按序展示（`gate1:blocked:*`、`tests:failed:attempt=n`、`gate2:approved`、`gate3:announced:vX`、`canary:*` 等），带时间轴样式；终态展示完整事件流（来自 result），运行中展示当前 stage 与留痕文件线索（见 8.3） | `result.gate_events`（终态） |
| M2-6 | 审批记录 | `approvals[]` 流水（含 timeout-reject） | `result` |
| M2-7 | 发布结果卡片 | version、rolled_back、reason、金丝雀实测（error_rate / p99） | `result.deploy_result` / `canary` |
| M2-8 | 操作区 | 按当前 stage 条件渲染可用操作（审批 / 窗口指令 / 排队）；终态显示审计导出 | 同 M3/M4 |

### 3.3 M3 审批中心（Approvals）

| # | 功能点 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| M3-1 | 待办列表 | 筛选 `stage=WAIT_APPROVAL`：区分一级待审 / 一级已批待二级；**审批倒计时**（服务端截止时间），最后 5 分钟变红 | `status` query + 截止时间（见 8.1 扩展） |
| M3-2 | 审批详情 | 复用 M2 详情（定位到补丁/测试卡片），突出「批准 / 驳回 / 转人工」决策区 | 同上 |
| M3-3 | 审批操作 | 一级 `submit_approval`、二级 `submit_second_approval`；操作前二次确认（展示决策摘要）；成功后防重复点击 | Temporal signal |
| M3-4 | 超时提示 | 倒计时归零后提示「已自动驳回（gate2:timeout->reject），流程已转人工」 | status 刷新 |

### 3.4 M4 发布窗口控制台（Release Window）

| # | 功能点 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| M4-1 | 窗口列表 | `stage=NOTIFYING` 的流程：大号倒计时（分:秒，服务端校准）、公告版本号、队列摘要 | `status` + 截止时间（见 8.1） |
| M4-2 | 部署加速 | 「立即发布」→ `submit_deploy_command("deploy_now")`；二次确认 | signal |
| M4-3 | 取消发布 | 「取消本次发布」→ `submit_deploy_command("cancel")`；**红色二次确认**（提示：排队补丁保留，队首将自动开启新周期） | signal |
| M4-4 | 补丁排队 | 表单：new_wf_id（默认按 `aiops-fix-{service}-{new_alert_id}` 自动生成）、service、alert_id、description → `submit_patch`；提示「不重置倒计时、不合并、数量不设上限」 | signal |
| M4-5 | 队列展示 | `queued_patches[]` 列表 + 说明「当前流程结束后队首自动承接」 | `status` |

### 3.5 M5 审计回看（Audit）

| # | 功能点 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| M5-1 | 历史流程检索 | 按 alert_id / service / 终态 / 时间范围过滤（近 30 日） | Temporal list（终态）+ BFF 归档缓存 |
| M5-2 | 审计详情 | 完整审计记录（result 全字段）+ 导出 JSON | `result` + `data/` 留痕 |
| M5-3 | 留痕关联 | 关联展示 `data/notify/*.json`（公告/审批卡片）、`data/argocd/*.json`（发布 manifest） | BFF 读 `data/` |
| M5-4 | 操作审计 | 控制台每个写操作的留痕（操作人、时间、参数、结果），独立可查 | BFF 操作审计日志（见 6.4） |

### 3.6 M6 系统状态（System）

| # | 功能点 | 说明 | 数据来源 |
| --- | --- | --- | --- |
| M6-1 | 服务健康 | Temporal 连通性、worker 心跳（`aiops-tasks` 队列有活跃 poller）、接入服务端口 | BFF 主动探测 |
| M6-2 | 策略快照 | 当前生效策略（含锁定项标识）；展示 policy_source 与关键时长 | BFF 加载的同一策略文件（只读） |
| M6-3 | 索引与模型 | 索引 stats（files/chunks/tickets/dim/model/backend）、FAISS 可用性 | `data/code_index.json` |
| M6-4 | 发布状态 | 稳定容器版本与健康（`/health`）、最近发布记录 | BFF 探测 stable（:18080）+ `data/argocd/` |

> 演示策略下的关键时长会在策略卡片中显著标注（如「审批 300s / 倒计时 30s / 观测 10s——演示加速」），避免值班误判。

---

## 4 信息架构

### 4.1 页面树

```text
运维控制台
├── /                看板 Dashboard（M1：概览计数 + 进行中卡片墙 + 终态分布）
├── /flows           流程列表（全部/进行中/终态；过滤：service、stage、终态类型、时间）
│   └── /flows/:wfId 流程详情（M2；Tabs：概览 | 时间线 | 补丁与测试 | 审批记录）
├── /approvals       审批中心（M3：待办 + 倒计时；含二级审批筛选）
├── /window          发布窗口（M4：倒计时大卡 + 三操作 + 队列）
├── /audit           审计回看（M5：搜索 + 记录列表 + 导出）
└── /system          系统状态（M6：健康 / 策略 / 索引 / 发布）
```

### 4.2 全局框架

```text
┌──────────────────────────────────────────────────────────────────┐
│ 顶栏：产品名 │ 全局态势（进行中 n · 待审 n · 倒计时 n） │ ⏱服务端时间 │ 用户 │
├────────────┬─────────────────────────────────────────────────────┤
│ 侧导航      │  主内容区                                            │
│  看板       │   （路由页面；所有写操作经操作确认层）                  │
│  流程列表    │                                                     │
│  审批中心 ●2 │                                                     │
│  发布窗口 ●1 │                                                     │
│  审计回看    │                                                     │
│  系统状态    │                                                     │
└────────────┴─────────────────────────────────────────────────────┘
```

- 侧导航徽标（●n）实时提示待办数（待审 + 倒计时），点击直达；
- 顶栏「服务端时间」与倒计时同源（BFF 返回 `server_time` 校准前端偏移），消除客户端时钟偏差。

### 4.3 与现有入口的操作对应

| 操作 | demo_cli 对应 | IM 卡片对应 | 控制台对应 |
| --- | --- | --- | --- |
| 启动流程 | `start` | —（由告警触发） | 本期不做（生产由告警自动触发；演示可用 CLI/接入服务） |
| 查状态 | `status` | — | M1/M2 实时展示 |
| 审批 | `approve` / `second-approve` | 卡片按钮 | M3 按钮（同信号） |
| 窗口指令 | `deploy-now` / `cancel` | — | M4 按钮（同信号） |
| 排队 | `queue-patch` | — | M4 表单（同信号） |
| 结果 | `result` | — | M2/M5 审计视图 |

---

## 5 技术架构

### 5.1 总体架构

```text
┌──────────────────────────── 浏览器 ────────────────────────────┐
│  React 18 + Vite + TS + Tailwind SPA（深色主题）                 │
│  TanStack Query（轮询）· React Router · 自绘组件（无重型 UI 库）    │
└───────────────▲────────────────────────────────────────────────┘
                │ HTTPS/JSON（REST）
┌───────────────┴──────────────── BFF（FastAPI）──────────────────┐
│  · Temporal Client 封装：list_workflows / query status / signal │
│  · data/ 产物只读聚合：notify、argocd、code_index、评测报告        │
│  · 服务探测：stable /health、worker poller、Temporal 连通性        │
│  · 操作审计：写操作留痕 data/web-audit/*.jsonl                    │
└──────▲──────────────────────▲──────────────────────▲────────────┘
       │ gRPC（temporalio）    │ 文件读取               │ HTTP 探测
┌──────┴──────┐        ┌──────┴──────┐        ┌──────┴──────┐
│  Temporal   │        │  data/ 产物  │        │ stable 容器  │
│  :7233      │        │  留痕/索引    │        │ :18080      │
└─────────────┘        └─────────────┘        └─────────────┘
```

**为什么必须引入 BFF（而非前端直连 Temporal）**：

1. Temporal gRPC/Web 端口不应暴露给浏览器（无细粒度鉴权、非 HTTP/JSON 语义）；
2. 业务聚合需要跨接口合成——列表页需拉取全部 workflow 再并行 `status` query，详情页需拼合 `status` + `result` + `data/` 留痕；
3. 操作需要统一审计、二次确认语义与权限校验的落点；
4. BFF 与 backend 同属 Python 生态，可直接复用 `aiops_agent` 的 `models`/`config`，保证数据结构零漂移。

### 5.2 前端技术栈

| 项 | 选型 | 说明 |
| --- | --- | --- |
| 框架 | React 18 + TypeScript | 组件生态成熟、类型贯通 BFF 契约 |
| 构建 | Vite | 秒级冷启动 / HMR |
| 样式 | Tailwind CSS v4（自定义主题 token） | 深色主题以 design token 落地（见 7.1） |
| 路由 | React Router v7 | 6 个一级路由 + 详情嵌套 |
| 数据层 | TanStack Query v5 | 轮询（`refetchInterval`）、缓存、乐观更新 |
| 图标 | Lucide React | 线性图标，贴合运维工具气质 |
| diff 渲染 | `diff`（jsdiff）+ 自绘行级高亮 | 轻量，避免引入重型编辑器 |
| 图表 | 无（P1）；终态分布用纯 CSS 条 | 控制台图表需求量小，避免过早引入图表库 |

### 5.3 BFF 技术栈与模块

| 项 | 选型 |
| --- | --- |
| 运行时 | Python 3.12（与主工程一致，复用 `.venv`，新增 `fastapi`、`uvicorn`） |
| Web 框架 | FastAPI（OpenAPI 自动文档：`/docs`） |
| Temporal 访问 | `temporalio.Client`（复用 `aiops_agent.workflows.AIOpsFixWorkflow` 信号/查询定义） |
| 配置 | 环境变量（`TEMPORAL_ADDRESS`、`AIOPS_POLICY_PATH`、`AIOPS_STABLE_PORT` 等，与主工程同源）；**仅加载策略文件用于展示，不做热修改** |
| 操作审计 | 追加写 `data/web-audit/audit-YYYYMMDD.jsonl`（每行一条：ts、actor、action、wf_id、params、result） |
| 静态托管 | 生产：BFF 挂载前端构建产物（`web/dist`）同域提供；开发：Vite dev server 代理 `/api` |

### 5.4 实时性方案（分期）

| 阶段 | 方案 | 说明 |
| --- | --- | --- |
| P1/P2 | **轮询** | 列表 5s、详情 3s、审批/窗口页 2s（TanStack Query `refetchInterval`）；倒计时本地秒级插值、每 30s 与服务端校准 | 
| P3（可选） | SSE 推送 | BFF 提供 `/api/events`（Server-Sent Events）：轮询 Temporal 侧状态变化后只推「有变化的 wf」增量事件；前端切换为「SSE 优先 + 轮询兜底」 |

> 选择轮询优先的依据：流程状态变化频率低（分钟级）、并发用户数小（值班团队）、实现与排障成本最低；SSE 作为可选增强不影响架构。

### 5.5 认证与安全

| 项 | 演示环境 | 生产建议 |
| --- | --- | --- |
| 认证 | 免登录（仅监听 127.0.0.1） | OIDC/SSO（对接企业 IdP），BFF 校验 JWT |
| 角色 | 单用户全权限 | operator / approver / viewer（服务端 RBAC 校验每个写接口） |
| 网络 | 本机回环 | 内网 + 反代（TLS），禁止公网直达 |
| 写操作防护 | 二次确认 + 防重复提交 | 同上 + 操作审计（必留 actor）；高危操作（cancel）要求输入 wf-id 尾号确认 |
| 敏感面 | 控制台不展示任何密钥；策略仅展示非敏感键 | 与演示一致 |

### 5.6 部署形态

```text
演示（单机）：
  cd aiops-agent && .venv/bin/uvicorn bff.app:app --host 127.0.0.1 --port 8600
  浏览器打开 http://127.0.0.1:8600 （BFF 托管 web/dist 构建产物）
  # 依赖：Temporal(:7233)、worker、stable 容器；data/ 与主工程同目录

生产：BFF 独立容器（同镜像挂载 data/ 只读 + 与 Temporal 同网络），
      前端静态产物由 BFF 提供或 CDN；反代统一 TLS 与 SSO。
```

端口规划：BFF `8600`（避开已用端口 7233/8233/8099/11434/18080/3101/9093）。

---

## 6 接口设计（BFF REST API）

### 6.1 接口清单

| # | 方法 | 路径 | 功能 | Temporal / 数据映射 |
| --- | --- | --- | --- | --- |
| 1 | GET | `/api/overview` | 看板聚合：计数 + 进行中卡片 + 终态分布 | `list_workflows` + 并发 `status` |
| 2 | GET | `/api/flows` | 流程列表（过滤：status=open/closed、service、stage、终态类型、时间窗） | `list_workflows`（分页）+ `status` 聚合 |
| 3 | GET | `/api/flows/{wf_id}` | 流程详情聚合：status + （终态时）result + data/ 留痕索引 | `query status` / `result` + 文件读取 |
| 4 | GET | `/api/flows/{wf_id}/result` | 完整审计记录（仅终态；运行中返回 409 + 当前 stage） | `handle.result()`（BFF 先检查 closed 状态避免挂起） |
| 5 | POST | `/api/flows/{wf_id}/approval` | 一级审批 `{decision: approve\|reject}` | `signal submit_approval` |
| 6 | POST | `/api/flows/{wf_id}/second-approval` | 二级审批 `{decision}` | `signal submit_second_approval` |
| 7 | POST | `/api/flows/{wf_id}/deploy-command` | 窗口指令 `{command: deploy_now\|cancel}` | `signal submit_deploy_command` |
| 8 | POST | `/api/flows/{wf_id}/queue-patch` | 补丁排队 `{new_wf_id, service, alert_id, description}` | `signal submit_patch` |
| 9 | GET | `/api/approvals` | 审批中心聚合（WAIT_APPROVAL 列表 + 缺口时间） | `status` 过滤 + 截止时间 |
| 10 | GET | `/api/window` | 发布窗口聚合（NOTIFYING 列表 + 截止时间 + 队列） | `status` 过滤 + 截止时间 |
| 11 | GET | `/api/audit` | 历史审计检索（分页） | 终态 `result` + BFF 归档缓存 |
| 12 | GET | `/api/system` | 系统状态（策略/索引/健康/发布） | 环境 + 文件 + 探测 |
| 13 | GET | `/api/events`（P3） | SSE 增量事件流 | 状态差分推送 |
| 14 | GET | `/api/health` | BFF 自身健康（探活用） | — |

### 6.2 关键响应契约（示例）

**GET `/api/flows`**（列表项）：

```json
{
  "server_time": "2026-10-01T22:10:00+08:00",
  "items": [
    {
      "wf_id": "aiops-fix-order-a-1001",
      "status": "RUNNING",
      "service": "order",
      "alert_id": "a-1001",
      "start_time": "2026-10-01T22:05:00+08:00",
      "stage": "WAIT_APPROVAL",
      "approval": null,
      "second_approval": null,
      "deploy_command": null,
      "queued_patches": [],
      "deadline": { "kind": "approval", "at": "2026-10-01T22:10:00+08:00" }
    }
  ]
}
```

**GET `/api/flows/{wf_id}`**（详情聚合，节选）：

```json
{
  "wf_id": "aiops-fix-order-a-1001",
  "status": "RUNNING",
  "stage_state": { "stage": "WAIT_APPROVAL", "approval": null, "...": "..." },
  "result": null,
  "artifacts": {
    "notify_cards": ["data/notify/approval-p-a-1001-r1.json"],
    "argocd_manifest": null,
    "sandbox_workspace": "data/sandbox/p-a-1001-r1"
  }
}
```

**POST 写操作统一响应**：

```json
{ "ok": true, "wf_id": "aiops-fix-order-a-1001", "action": "approval:approve", "audited": true }
```

### 6.3 状态码与错误约定

| 码 | 场景 |
| --- | --- |
| 200 | 成功 |
| 400 | 参数非法（decision/command 取值、new_wf_id 格式 `aiops-fix-*`） |
| 404 | wf_id 不存在 |
| 409 | 状态冲突（对终态流程发审批信号、非 WAIT_APPROVAL 阶段调审批接口、`/result` 未终态） |
| 502 | Temporal 不可达（前端提示「编排服务不可用」并保留只读数据展示） |

**前置状态校验（BFF 侧，避免幽灵操作）**：写接口先 `status` query 校验当前 stage 允许该操作（审批=WAIT_APPROVAL；窗口指令/排队=NOTIFYING），不满足返回 409 并附说明。该校验与后端工作流内部闸门形成双重防护。

### 6.4 操作审计

每个写操作成功后追加一行 JSONL（BFF 执行、前端不可绕过）：

```json
{"ts":"2026-10-01T22:07:31+08:00","actor":"operator@demo","action":"deploy_command:cancel","wf_id":"aiops-fix-order-a-1001","params":{},"result":"signaled"}
```

---

## 7 页面与视觉设计（深色主题）

### 7.1 设计语言

**气质**：安静、克制、数据优先的「夜间值班台」——深蓝黑基底、单一青色强调、状态用色语义化；等宽字体承载一切机器标识（wf-id / diff / 版本号）。

**设计 token（Tailwind 自定义主题）**：

| Token | 色值 | 用途 |
| --- | --- | --- |
| `bg-base` | `#0B0F14` | 全局底色 |
| `bg-panel` | `#10161D` | 卡片/面板 |
| `bg-elevated` | `#172029` | 悬浮层/选中行 |
| `border-subtle` | `#1F2933` | 分割线/卡片描边 |
| `text-primary` | `#E6EDF3` | 主文本 |
| `text-secondary` | `#8899A6` | 次要文本/标签 |
| `accent` | `#2DD4BF` | 主强调（链接、主按钮、聚焦环） |
| `status-ok` | `#34D399` | 成功/DONE/健康 |
| `status-warn` | `#FBBF24` | 等待/倒计时/CANARY |
| `status-danger` | `#F87171` | 失败/ESCALATED/劣化回滚 |
| `status-info` | `#60A5FA` | 进行中/信息 |
| `status-idle` | `#64748B` | 已取消/无状态 |

**字体**：UI 文本 `Inter / system-ui`；机器标识与 diff 用 `JetBrains Mono / SF Mono`（字号 12–13px、行高 1.6）。

**间距与圆角**：4px 基准网格；卡片圆角 10px、描边 1px、**无重阴影**（深色下用层级色差而非阴影表达层次）；动效仅用于状态变化（200ms 过渡），无装饰性动画。

### 7.2 状态色彩系统（stage → 视觉）

| stage | 色彩 | 徽标文案 |
| --- | --- | --- |
| TRIAGING / FIXING / TESTING | `status-info` 蓝 + 呼吸点 | 分析中 / 修复中 / 测试中（含 attempt=N） |
| WAIT_APPROVAL | `status-warn` 琥珀 | 待审批（一级/二级）· 倒计时 |
| NOTIFYING | `status-warn` 琥珀（倒计时数字放大） | 公告倒计时 mm:ss |
| CANARY / ROLLING_OUT | `status-info` 蓝 | 金丝雀观测 / 发布中 |
| DONE | `status-ok` 绿 | 已完成 |
| ESCALATED | `status-danger` 红 | 已转人工（附原因摘要） |
| CANCELLED | `status-idle` 灰 | 已取消 |

### 7.3 关键页面描述

**7.3.1 看板 `/`**
- 顶部四计数卡（进行中/待审批/倒计时/今日完成），点击下钻到对应筛选列表；
- 中部「进行中流程」卡片墙（自适应网格）：卡片=服务名+alert_id（等宽）、stage 徽标、耗时计时、最新 gate_event 一行摘要、行内操作按钮（审批/窗口操作直达）；
- 底部「近 7 日终态分布」横向占比条（绿/红/灰三色 + 数值）；
- 空状态：所有流程完成后显示「当前无进行中流程」+ 最近 3 条历史。

**7.3.2 流程详情 `/flows/:wfId`**
- 头部：wf-id（可复制）、stage 徽标、总耗时、右侧「审计导出」按钮（终态可用）；
- Tab 1 概览：根因卡（confidence 用进度环 + 阈值 0.8 刻度线对比）、补丁卡（风险徽标：低/中/高）、测试卡（三项 + 失败用例清单）、发布卡（版本/回滚状态/金丝雀实测）；
- Tab 2 时间线：gate_events 垂直时间轴，事件按类型着色（失败=红、通过=绿、闸门=琥珀、发布=蓝）；回炉重试的 attempt 分组折叠；
- Tab 3 补丁与测试：全屏 diff 查看器（双栏紧凑视图：行号 + 语法色保留灰度、`+` 绿底 / `-` 红底），测试输出折叠面板；
- Tab 4 审批记录：approvals 流水表格（gate/decision/结果），timeout-reject 灰色标注。

**7.3.3 审批中心 `/approvals`**
- 列表按倒计时升序（最紧急在上）：每行=服务/alert_id、审批层级（一级/二级）、**倒计时 mm:ss**（<5min 红、<1min 闪烁）；
- 行内「详情」「批准」「驳回」；点击「批准/驳回」弹确认层：展示根因摘要 + confidence + diff 首 10 行 + 测试结论（默认折叠），底部大按钮；
- 二级审批行有独立视觉标记（左侧琥珀竖条 + 「受保护目录」标签）。

**7.3.4 发布窗口 `/window`**
- 单一大卡片（生产同时最多 1 个窗口）：巨型倒计时（mm:ss，等宽字体 48px）、公告版本、服务/补丁信息、队列列表；
- 三操作按钮横排：「立即发布」（主按钮）、「取消本次发布」（危险样式）、「新补丁排队」（打开表单）；
- 取消确认层红色主题，文案强调「排队补丁将保留，队首补丁自动开启新周期」；
- 倒计时归零瞬间：卡片切换为「正在进入金丝雀…」并自动刷新为 CANARY 视图。

**7.3.5 审计回看 `/audit`**
- 顶部搜索：alert_id / service / 终态 / 时间范围（近 7/30 日）；
- 结果表：wf-id、服务、终态徽标、置信度、模型、耗时、完成时间；行点击展开 M2 详情（只读）；
- 「导出 JSON」单条与批量（打包审计记录）。

**7.3.6 系统状态 `/system`**
- 健康卡组：Temporal 连通（延迟ms）、worker poller、接入服务 8099、stable 容器版本与延迟；
- 策略卡：policy_source + 关键键值（锁定项带锁形图标与原因 tooltip：如「超时自动驳回（锁定）」）；
- 索引卡：files/chunks/tickets/dim/model/backend（FAISS 已启用/回退标记）；
- 发布卡：最近 5 次发布（版本、结果、时间，数据源 `data/argocd/`）。

### 7.4 组件清单（自绘）

| 组件 | 用途 |
| --- | --- |
| `StageBadge` | 阶段徽标（7.2 色彩系统） |
| `CountdownRing` / `CountdownText` | 审批/窗口倒计时（服务端校准 + 本地插值） |
| `FlowCard` | 看板/列表流程卡 |
| `DiffViewer` | unified diff 行级渲染（折叠大文件） |
| `EventTimeline` | gate_events 时间轴 |
| `RiskBadge` | 补丁风险（低/中/高） |
| `ConfirmDialog` | 操作确认（常规/危险两级样式） |
| `MetricBar` | 终态分布条 |
| `CopyableId` | wf-id / alert_id 一键复制（等宽） |
| `EmptyState` | 空状态（含引导文案） |

---

## 8 后端配合改造点

本期方案对后端只提出**两处小改造**（均向后兼容、不改行为语义）：

### 8.1 status query 扩展截止时间字段（推荐，影响 P1 倒计时精度）

现状：`status` query 返回 `{stage, approval, second_approval, deploy_command, queued_patches}`，**不含**阶段截止时间，前端无法精确展示「审批剩余 12:47」与「倒计时 00:23」。

改造：`workflows.py` 的 `status` query 增加两个只读字段（进入对应阶段时记录截止点，使用 `workflow.now()` 派生）：

```json
{ "stage": "WAIT_APPROVAL",
  "deadline": { "kind": "approval|second_approval|countdown",
                "at": "2026-10-01T22:10:00+08:00" } }
```

兼容性：纯增量字段；旧调用方（demo_cli 打印）不受影响。若暂不改造，控制台 P1 降级方案 = 仅展示当前预览时刻起的相对示意（不承诺分钟级精确），P2 再启用精确倒计时。

### 8.2 （可选）Search Attributes 注册

现状：列表按 `WorkflowType="AIOpsFixWorkflow"` + `ExecutionStatus` 过滤，然后对每条并发 `status` query 聚合 stage（演示规模 <100 流程无压力）。

可选优化（生产规模时）：注册自定义 Search Attributes（`AioService`、`AioStage`），`list_workflows` 直接按 stage/service 服务端过滤，减少 N 次 query。默认 dev server 支持标准属性，自定义需 `temporal operator search-attribute create`。

### 8.3 运行中流程的证据展示说明

- 根因/补丁/测试报告字段在**工作流结果**（`result`）中随终态一次性返回；运行中前端展示 `status` 可得信息 + 日志侧留痕（`data/sandbox/`、`data/notify/`）；
- 备选（不推荐进本期）：BFF 读取 Temporal Event History 提取 activity 结果——实现复杂且耦合内部结构，仅在确有审计需要的生产场景评估。

---

## 9 实施计划

### 9.1 分期

| 阶段 | 范围 | 交付物 | 验收标准 |
| --- | --- | --- | --- |
| **P1 核心闭环** | BFF 骨架（FastAPI + Temporal 封装 + 操作审计）；看板 + 流程列表 + 详情（status/result）；审批操作；深色主题基础与组件库 | 可运行的本地控制台 | 用控制台完成：查看进行中流程 → 打开待审批 → 批准 → 跟踪到 DONE；demo_cli 场景 1/2 可在控制台还原 |
| **P2 窗口与审计** | 发布窗口控制台（倒计时/三操作/队列）；审批中心（倒计时/二级标识）；审计回看（检索/导出）；系统状态页 | 功能完整控制台 | 操作手册剧本 6–9（金丝雀回滚/排队/取消承接/审批超时）全程控制台可见可操作；审计导出与 wf result 一致 |
| **P3 增强** | SSE 实时推送；Search Attributes 过滤；终态分布趋势；操作审计页 | 体验增强版 | 状态变更 2s 内上屏（SSE）；100+ 流程列表过滤无卡顿 |

### 9.2 建议工程结构（实施时）

```text
aiops-agent/
├── bff/                      # FastAPI BFF
│   ├── app.py                # 入口（挂载 API + 静态产物 + 健康）
│   ├── temporal_gateway.py   # Temporal Client 封装（list/query/signal/result）
│   ├── aggregator.py         # data/ 产物聚合（notify/argocd/index）
│   ├── audit.py              # 操作审计 JSONL
│   └── requirements-bff.txt  # fastapi / uvicorn（追加依赖，不影响主工程）
├── web/                      # 前端工程
│   ├── src/{pages,components,lib,styles}/
│   ├── vite.config.ts        # dev 代理 /api → :8600
│   └── package.json
└── （既有后端零改动，除 8.1 可选扩展）
```

### 9.3 工作量估算（参考）

| 阶段 | 后端 BFF | 前端 | 合计 |
| --- | --- | --- | --- |
| P1 | 2–3 人日 | 4–5 人日 | 约 1.5 周 |
| P2 | 1–2 人日 | 3–4 人日 | 约 1 周 |
| P3 | 1–2 人日 | 2–3 人日 | 约 0.5–1 周 |

---

## 10 风险与边界

| 风险 | 应对 |
| --- | --- |
| 双通道审批（IM 与 Web 并发操作） | 两个通道写入同一 Temporal 信号（单值后到覆盖）；控制台在操作后立即刷新状态并禁用按钮，展示「已审批」；IM 侧无状态变更推送不影响安全（信号本身幂等无副作用） |
| 运行中流程证据不可见（结果随终态返回） | 运行中展示 status 可得信息 + 留痕文件；关键证据在终态完整呈现（见 8.3） |
| 轮询放大 Temporal 压力 | 间隔分级（2–5s）+ TanStack Query 缓存；规模增大转 SSE 增量推送 |
| 前端时钟漂移导致倒计时错误 | 所有截止时间以服务端时间为准（BFF 返回 `server_time`，前端校偏 + 本地插值） |
| 误操作（cancel 发布） | 危险操作红色二次确认 + 操作审计留痕；生产 RBAC 限制 operator 角色 |
| 安全暴露面 | 演示仅回环监听；生产 SSO + 内网 + TLS；控制台不展示任何密钥 |
| BFF 与主工程依赖耦合 | BFF 仅复用 `models`/`workflows` 定义与策略文件读取（只读）；依赖清单独立（requirements-bff.txt） |

---

## 附录 A 与《用户操作手册》剧本的覆盖矩阵

| 剧本 | 控制台覆盖 |
| --- | --- |
| 1 正常全链路 | ✅ 全程可视可操作（除 start 用 CLI/告警） |
| 2 闸门 1 转人工 | ✅ 时间线展示 `gate1:blocked` |
| 3 二级审批 | ✅ 审批中心二级标识 + 操作 |
| 4 回炉后通过 | ✅ 时间线 attempt 折叠展示 |
| 5 重试耗尽 | ✅ 终态 ESCALATED + 原因 |
| 6 金丝雀回滚 | ✅ 发布卡（rolled_back/reason） |
| 7 排队（FIFO） | ✅ 窗口页队列 + 表单 |
| 8 取消承接 | ✅ 取消操作 + 新周期自动出现在列表 |
| 9 审批超时 | ✅ 倒计时归零 → 状态刷新为超时驳回 |
| 10 告警自动触发 | ✅ 新流程自动出现在看板（推送/轮询） |

## 附录 B 页面清单速查

| 路由 | 页面 | 主要操作 |
| --- | --- | --- |
| `/` | 看板 | 下钻、行内审批/窗口操作 |
| `/flows` | 流程列表 | 过滤、进入详情 |
| `/flows/:wfId` | 流程详情 | 审批、窗口操作、审计导出 |
| `/approvals` | 审批中心 | 批准、驳回 |
| `/window` | 发布窗口 | 立即发布、取消、排队 |
| `/audit` | 审计回看 | 检索、导出 |
| `/system` | 系统状态 | 只读 |
