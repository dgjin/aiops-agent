# AIOps 自动运维智能体系统用户操作手册

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.6 |
| 日期 | 2026-10-01（v1.6 修订 2026-10-14） |
| 适用对象 | 运维值班人员、审批人（含二级审批人）、演示人员、联调开发 |
| 关联文档 | 《系统设计说明书》、《部署手册》 |
| 修订记录 | v1.6（2026-10-14）：飞书客户端确认交互落地（长连接模式）——审批卡片改由企业自建应用机器人发送、长连接网关（`feishu_gateway.py`，watchdog 托管）接收回调，点击「批准 / 驳回」在飞书客户端内直接生效；未配置应用凭证时回落群机器人 webhook 仅留痕；配置与自检步骤见 7.1；v1.5（2026-10-14）：飞书审批卡片按钮直达审批落地（点击→内置回调网关→生效→卡片变回执）——操作者需映射为 operator（`AIOPS_IM_USERS`），成功/失败均有 toast 与卡片回执，防重复点击；钉钉按钮仍需外置网关转发（第 7 章）；v1.1：控制台四级导航（总览/跨系统待办/被监控系统/平台）与局域网访问；一键启动与组件守护指引（demo-up.sh）。v1.2（2026-10-13）：production 档收敛 loopback、经本机 Caddy HTTPS（8643）进入（见 9.1/9.2/9.3/9.5；产物未运行验收）；v1.3（2026-10-13）：IM 流程通知补齐（接收/需求接收/进入修复状态消息，卡片共七类）；生产档切换全链路执行（`shadow_mode: false`，MR → 审批 → 公告 → 金丝雀）；v1.4（2026-10-13）：影子建议续接——影子档（SHADOWED）建议归档评估后可续接执行完整链路（复用原补丁、不重新生成；`POST /api/requirements/analyses/{id}/resume-shadow`，见 5.4） |

---

## 1 手册说明

### 1.1 手册定位

本手册面向**使用者**（非部署/开发角色），说明如何操作 AIOps 自动运维智能体：

- 如何启动一次自动修复流程（手工 CLI 或由 Alertmanager 自动触发）；
- 如何查看流程状态、如何审批、如何操作发布窗口、如何查看结果；
- 通知卡片上各按钮的含义与回调行为；
- 完整演示剧本（含各异常分支的触发方式与预期结果）；
- 常见问题与操作速查。

系统的部署、启动与配置维护见《部署手册》；原理与实现细节见《系统设计说明书》。

### 1.2 前置条件

使用前请确认（由部署人员保障）：

1. Temporal 服务端已启动（默认 `localhost:7233`）；
2. AIOps worker 已运行（`python -m aiops_agent.worker`），终端可见「Worker 已启动」日志；
3. 使用演示加速策略时，操作终端与 worker 使用**同一份**策略文件（见 2.2 注意事项）；
4. （可选）Web 控制台：BFF 已启动——推荐直接用 `bash scripts/demo-up.sh` 一键含控制台（见部署手册 3.1），浏览器访问 **http://127.0.0.1:8600**（局域网设备 **http://<本机IP>:8600**）。

### 1.3 术语速览

| 术语 | 含义 |
| --- | --- |
| workflow / 流程实例 | 一次完整的告警修复流程，ID 形如 `aiops-fix-{service}-{alert_id}` |
| 闸门 1 / 2 / 3 | 置信度闸门 / 运维审批闸门 / 用户公告闸门 |
| attempt | 补丁回炉轮次（0 开始，默认最多 3 轮：0/1/2） |
| 窗口 | 公告后的延迟发布倒计时窗口（演示 30s / 生产 15m） |
| ESCALATED | 转人工终态（需人工接手） |
| CANCELLED | 发布被取消（队列保留） |

---

## 2 快速上手（5 分钟演示）

### 2.1 最小链路（三个终端）

**终端 A —— Temporal 服务端**（服务常驻）：

```bash
temporal server start-dev
```

**终端 B —— Worker**（先加载演示加速策略再启动）：

```bash
cd aiops-agent
export AIOPS_POLICY_PATH=./demo-policy.yaml
python -m aiops_agent.worker
```

**终端 C —— 操作端**（同样加载演示策略）：

```bash
cd aiops-agent
export AIOPS_POLICY_PATH=./demo-policy.yaml

# 1. 启动修复流程
python demo_cli.py start --service order --alert-id a-1001

# 2. 查看状态（应看到 stage 演进到 WAIT_APPROVAL）
python demo_cli.py status --wf-id aiops-fix-order-a-1001

# 3. 审批通过
python demo_cli.py approve --wf-id aiops-fix-order-a-1001 --decision approve

# 4. 演示中加速发布（跳过剩余倒计时）
python demo_cli.py deploy-now --wf-id aiops-fix-order-a-1001

# 5. 等待结束并查看审计记录
python demo_cli.py result --wf-id aiops-fix-order-a-1001
```

预期：终端 B 依次输出 `[triage]` → `[rag]` → `[fix]` → `[sandbox]` → 审批卡片 → `[notify]` → `[canary]` → `[release]` 日志；`result` 显示 `"stage": "DONE"`。

> 图形界面替代：上述全流程也可在 Web 控制台完成——`bash scripts/demo-up.sh` 一键含控制台（见部署手册 3.1），浏览器访问 http://127.0.0.1:8600；「审批中心」对应本文的 approve，「发布窗口」对应 deploy-now / cancel（详见 9.2）。

### 2.2 注意事项（重要）

1. **策略一致性**：`start` 时的策略快照与 worker 必须一致。`start` 命令会打印 `策略快照：./demo-policy.yaml（...）`；若提示「当前未使用演示加速策略」，说明当前终端未 export `AIOPS_POLICY_PATH`，此时审批超时为 30 分钟、倒计时 15 分钟、观测 5 分钟（生产参数）。
2. **幂等**：重复执行同一条 `start`（相同 service+alert_id）不会重复开流程，第二个实例直接报「已存在」失败提示，属正常防重行为。
3. 流程一旦启动，`approve`/`deploy-now` 等信号**必须发给对应 wf-id**；wf-id 公式为 `aiops-fix-{service}-{alert-id}`。

---

## 3 角色与操作

| 角色 | 可执行操作 | 对应信号 |
| --- | --- | --- |
| 运维值班（一级审批人） | 审批（放行/驳回）、窗口加速、窗口取消、补丁排队 | `submit_approval`、`submit_deploy_command`、`submit_patch` |
| 二级审批人 | 受保护目录补丁的二级审批（放行/驳回） | `submit_second_approval` |
| 观察者 | 查询状态、查看结果 | `status` / `result`（只读） |

**控制台角色映射**（Bearer 令牌，`/api` 全量鉴权；未配置令牌时全部接口 503 fail-closed）：

| 控制台角色 | 可执行操作 | 对应 CLI 侧角色 |
| --- | --- | --- |
| `viewer` | 看板 / 流程详情 / 审计查询（只读） | 观察者 |
| `operator` | viewer + 审批（含二级）/ 发布指令 / 排队补丁 | 值班 + 二级审批人 |
| `admin` | operator + 被监控应用维护 / 令牌状态查看与轮换 / kill switch | —（运维管理员） |

---

## 4 核心操作详解

### 4.1 启动修复流程

#### 方式一：手工 CLI（演示/联调）

```bash
python demo_cli.py start --service order --alert-id a-1001 \
  [--severity critical] [--description "描述文本，可带演示关键词"]
```

| 参数 | 说明 |
| --- | --- |
| `--service` | 服务名（进入 workflow id） |
| `--alert-id` | 告警号（进入 workflow id；同一 service+alert-id 幂等） |
| `--severity` | 严重级别，默认 `critical` |
| `--description` | 告警描述；携带演示关键词可触发分支（见第 6 章） |

#### 方式二：Alertmanager 自动触发（真实链路）

告警经 Alertmanager 子路由（`aiops_demo="true"` 标签）转发到告警接入服务 `http://<host>:8099/webhook`，自动完成「标准化 → 幂等启动」。观察 `alert_webhook.py` 终端日志：

```text
已启动 workflow: aiops-fix-order-<alert_id>
重复告警（幂等拒绝，不重复开流程）: aiops-fix-...
```

### 4.2 查看流程状态

```bash
python demo_cli.py status --wf-id aiops-fix-order-a-1001
```

输出字段解读：

```json
{
  "stage": "WAIT_APPROVAL",          // 当前阶段（字典见附录 B）
  "approval": null,                  // 一级审批结果：null=未响应 / "approve" / "reject"
  "second_approval": null,           // 二级审批结果（仅受保护目录使用）
  "deploy_command": null,            // 窗口指令：null / "deploy_now" / "cancel"
  "queued_patches": []               // 已排队的新补丁 [{workflow_id, alert_id}]
}
```

阶段流转顺序：`TRIAGING → FIXING → TESTING → WAIT_APPROVAL → NOTIFYING → CANARY → ROLLING_OUT → DONE`；异常终态为 `ESCALATED`（转人工）或 `CANCELLED`（取消）。

> 提示：`status` 是即时快照；若正在 `TRIAGING/FIXING/TESTING`，稍等数秒再查。

### 4.3 审批操作（闸门 2）

#### 4.3.1 常规审批

```bash
# 放行（意见可选，但建议写明依据）
python demo_cli.py approve --wf-id aiops-fix-order-a-1001 --decision approve
# 驳回（意见必填，将写入审计记录）
python demo_cli.py approve --wf-id aiops-fix-order-a-1001 --decision reject --comment "缺少回滚验证"
```

- **审批证据绑定（approval-evidence-v1）**：每次审批都必须能回答「哪一次变更」。控制台、ChatOps、飞书卡片与 CLI 均从当前流程 `status.evidence_binding` 派生案件 ID / 补丁 ID / diff 哈希 / 测试报告哈希 / 策略哈希 / run ID；页面回传的 `expected_*` 仅用于拒绝过期页面（旧 diff / 旧 run 的审批会得到 409「审批未生效」并提示刷新）。
- **意见必填**：驳回必须给出非空意见（≤2000 字）；飞书卡片按钮无法输入文本，点击「驳回」会提示改用控制台或群内「驳回 <ID前缀> -- 意见」。批准意见可选但全程留痕。
- **职责分离**：流程发起人不得审批自己的变更；二级审批人不得与一级审批人相同。控制台、ChatOps、飞书回调与直接 Temporal 信号都会拦截。
- **送达语义**：返回 `delivery=accepted` 表示工作流已受理并记录；`submitted` 仅表示命令已提交，业务结果以流程状态为准；`rejected` 不回执生效，卡片保留按钮便于修正后重试；`pending` 表示命令已登记但 Temporal 暂不可达，系统会按 2/5/15/45/120 秒退避自动重试（最多 5 次），**不要重复提交**，用 `/api/commands/<command_id>` 查询进度（见 9.2）。
- 收到**飞书**审批卡片后，直接点击卡片上的「批准 / 驳回」按钮即可完成审批（2026-10-14 起真实链路打通：应用机器人发卡 + 长连接接收回调，配置与自检见 7.1）：操作者需在 `AIOPS_IM_USERS` 中映射为 operator，成功后卡片就地替换为处置结果（toast + 绿/红回执卡，防重复点击）；权限不足 / 阶段不符 / 重复点击均会在 toast 中提示原因；钉钉按钮需外置网关转发（见第 7 章）；
- **超时策略（锁定）**：规定时间（演示 300s / 生产 30m）内无响应 → **自动驳回**并升级值班负责人（`gate2:timeout->reject`）；驳回即关闭流程并留痕。

#### 4.3.2 二级审批（受保护目录）

当补丁命中 `auth/**`、`payment/**`、`crypto/**` 时，需要**两级审批**：

1. 一级审批通过后，日志出现 `!! 命中受保护目录，需二级审批（submit_second_approval）`；
2. 二级审批人执行（驳回同样必填意见）：

```bash
python demo_cli.py second-approve --wf-id aiops-fix-order-a-1001 --decision approve
python demo_cli.py second-approve --wf-id aiops-fix-order-a-1001 --decision reject --comment "待补充影响面评估"
```

二级审批同样有超时自动驳回（`gate2b:timeout->reject`）。两级都通过才进入公告窗口；二级审批人与一级审批人、发起人不得相同。

### 4.4 发布窗口操作（闸门 3）

审批通过后进入 `NOTIFYING`：向用户推送公告卡片并开始倒计时（演示 30s / 生产 15m）。窗口内可执行以下操作：

| 操作 | 命令 | 语义 |
| --- | --- | --- |
| 加速发布 | `python demo_cli.py deploy-now --wf-id <wf>` | 跳过剩余等待，立即进入金丝雀 |
| 取消发布 | `python demo_cli.py cancel --wf-id <wf>` | 取消本次发布；**已排队补丁保留**，队首补丁自动开启新周期 |
| 新补丁排队 | `python demo_cli.py queue-patch --wf-id <wf> --new-wf-id <next_wf> --service order --alert-id a-1002` | 仅 FIFO 入队：**不重置倒计时、不合并、数量不设上限** |
| 到期（无操作） | — | 倒计时结束自动进入金丝雀（默认出口） |

> 要点：
> - `queue-patch` 的 `--new-wf-id` 是**未来**新周期的 workflow id（如 `aiops-fix-order-a-1002`）；当前发布结束/取消后系统自动以该 id 启动子流程；
> - 当前流程处于 `NOTIFYING` 时若再执行一次 `start`（同 service+alert_id 不同告警），建议直接对该流程 `queue-patch`，不要另开并行发布（并发底线：同一时刻全局仅一个发布执行）。

### 4.5 查看最终结果（审计记录）

```bash
python demo_cli.py result --wf-id aiops-fix-order-a-1001
```

关键字段解读：

| 字段 | 说明 |
| --- | --- |
| `stage` | 终态：`DONE`（成功）/ `ESCALATED`（转人工）/ `CANCELLED`（取消） |
| `root_cause` / `confidence` | 根因与置信度 |
| `patch_id` / `model_version` | 补丁编号（`p-{alert_id}-r{attempt}`）与生成模型 |
| `approvals` | 审批流水：`[{gate, decision}]`，含 timeout-reject |
| `gate_events` | 全事件流（字典见附录 C） |
| `deploy_result` | 发布终态：`{version, rolled_back, reason}` |
| `duration_seconds` | 全流程耗时 |
| `queued_patches` | 结束时仍滞留队列的补丁 |

### 4.6 IM 群指令（ChatOps：查询与审批）

值班人也可不打开控制台，直接在飞书/钉钉工作群里发消息完成查询与审批（操作入口前移）。前置：管理员已完成 IM 机器人网桥接入与通道配置（见部署手册 §3.8）。

命令一览（除中文外兼容英文别名，如 `pending` / `approve`；发送「帮助」可随时查看）：

| 命令 | 说明 | 最低角色 |
| --- | --- | --- |
| `帮助` | 命令说明 | viewer |
| `待办` | 列出待审批流程（受保护目录流程会标注二级审批进度） | viewer |
| `状态 <流程ID前缀>` | 单流程状态：阶段/根因/补丁/审批进度/截止时间 | viewer |
| `批准 <ID前缀> [-- 意见]` | 一级审批放行（意见可选） | operator |
| `驳回 <ID前缀> -- <意见>` | 一级审批驳回（意见必填） | operator |
| `二级批准 <ID前缀> [-- 意见]` | 二级审批放行（受保护目录流程） | operator |
| `二级驳回 <ID前缀> -- <意见>` | 二级审批驳回（意见必填） | operator |

一段典型的群内对话：

```text
[值班人]    @AIOps助手 待办
[AIOps助手] 待审批流程（2 个）：
              1. aiops-fix-order-a-1001（order）· 补丁 p-a-1001-r1（受保护目录，需二级审批）
              2. aiops-fix-coupon-a-1002（coupon）· 补丁 p-a-1002-r1
              发送「批准 <ID前缀>」或「驳回 <ID前缀> -- 意见」处置。

[值班人]    @AIOps助手 批准 aiops-fix-order-a-1001
[AIOps助手] ✓ aiops-fix-order-a-1001：一级审批命令已提交（操作者 ou_xxxx；业务结果以流程状态为准）
```

> 要点：
> - 流程 ID 支持**唯一前缀**；前缀命中多条时机器人回复候选列表，补充字符后再发即可；
> - 发送者未在 `AIOPS_IM_USERS` 中映射时按 **viewer** 处理（仅可查询）；审批类命令要求 operator 及以上；
> - 审批安全语义与控制台完全一致：复用同款写守卫（仅运行中 + 等待审批 + 防重复）、kill switch 与操作审计（审计 actor 记为 `im:<发送者>`）；驳回意见必填，发起人不得自审，二级审批人不得与一级相同；
> - 命令级失败（无权限 / 流程已结束 / 前缀不唯一 / 审批未生效等）会以文字直接回复原因，不会静默失败。

---

## 5 通知卡片说明

系统向 IM（飞书/钉钉）推送七类卡片；未配置 webhook 时自动改为**留痕模式**（保存到 `data/notify/*.json`，仅用于演示审计）。流程进行中会持续推送状态消息：**接收**（流程启动）→ **进入修复**（分析完成）→ 审批/公告 → 终态（影子档建议或转人工）。

### 5.1 审批卡片（橙色）

内容：根因（含置信度）、补丁（编号/风险/文件）、完整 diff、测试报告（单测/回归/SAST）、回滚预案；受保护目录追加「需二级审批」提示。

按钮：**批准**（`decision=approve`）、**驳回**（`decision=reject`，需在控制台或群内补充意见）。按钮载荷契约：

```json
{"wf_id": "aiops-fix-order-a-1001", "gate": "approval", "decision": "approve", "case_id": "a-1001", "diff_hash": "<sha256>", "run_id": "<temporal run id>"}
```

卡片上的 `case_id/diff_hash/run_id` 仅用于服务端拒绝**过期卡片**（点击时与当前 `status.evidence_binding` 比对）；可信绑定与信封始终由 BFF 从流程状态重新派生。

### 5.2 用户公告卡片（蓝色）

内容：版本号（`v1.0.X`）+ 影响提示 + 服务/补丁标识。无操作按钮；用户反馈通道不触发自动取消（是否取消由运维通过 `cancel` 决定）。

### 5.3 升级卡片（红色）

转人工时推送（超时驳回、测试耗尽、连续失败、金丝雀劣化等），内容含转人工原因、严重级别与处置动作（联系值班负责人人工处理）。

### 5.4 影子档建议卡片（青色）

影子档（`shadow_mode=true`）时，沙箱测试通过后推送修复建议：根因、建议补丁与完整 diff、沙箱测试结果，并注明「未创建 MR、未进入审批与发布，仅生成建议供人工参考」；流程到此为终态（`SHADOWED`，控制台显示「已建议（影子档）」）。这是渐进信任策略；当前生产档为**全链路**（`shadow_mode=false`，2026-10-13 起）：沙箱测试通过后创建 MR → 审批 → 公告倒计时 → 金丝雀发布；演示档 `demo-policy.yaml` 同为全链路。归档评估后决定采纳时，可对影子建议执行**续接**（API 入口：`POST /api/requirements/analyses/{analysis_id}/resume-shadow`，admin 角色；控制台 UI 入口后续接入）：不重新分析、不重新生成补丁，以原建议补丁续接执行完整链路（完整沙箱验证 → MR → 审批 → 公告 → 金丝雀发布）；续接流程 id 固定 `aiops-req-{analysis_id}-v{version}-resume1`（防重复续接），已发布成功（终态）的需求不可续接，未批准会话不可续接（均 409 精确拒绝）。留痕前缀 `shadow-`。

### 5.5 接收卡片（浅蓝色）

告警流程启动（监控到告警/日志）即推送：服务/告警标识、描述，以及处理流程说明（① 日志取证 → ② 聚类降噪 → ③ 根因分析 → ④ 闸门1 置信度判定；通过后进入修复流程）。留痕前缀 `intake-`。

### 5.6 需求接收卡片（浅蓝色）

需求实现任务（管理员批准后进入修复）启动时推送：需求标题、来源分析（版本·批准人）、需求原文摘要、验收要点，及处理流程说明（已批准，跳过日志诊断 → 直接进入修复流程）。留痕前缀 `req-intake-`。

### 5.7 进入修复卡片（靛蓝色）

告警/需求流程进入修复阶段时推送：分析结论（含置信度）、疑似文件、修复链路（生成补丁 → 沙箱验证 → 审批/发布），并标注当前模式——「影子档（仅建议不执行）」或「全链路（MR → 审批 → 公告倒计时 → 金丝雀发布）」。留痕前缀 `fix-start-`。

---

## 6 演示剧本（全场景矩阵）

> 通用前置：终端 B worker 与终端 C 操作端均 `export AIOPS_POLICY_PATH=./demo-policy.yaml`。
> 每次演示建议使用新的 `--alert-id`（避免与前一次幂等冲突）。

### 6.1 场景 1：正常全链路（推荐主剧本）

```bash
python demo_cli.py start --service order --alert-id demo-001
python demo_cli.py status --wf-id aiops-fix-order-demo-001       # 等到 WAIT_APPROVAL
python demo_cli.py approve --wf-id aiops-fix-order-demo-001      # 批准
python demo_cli.py deploy-now --wf-id aiops-fix-order-demo-001   # 加速（或等待 30s 到期）
python demo_cli.py result --wf-id aiops-fix-order-demo-001
```

预期：`[fix] validated=True` → `[sandbox] PASS 6/6` → 审批卡片 → `[notify] 已推送公告` → `[canary] 实测错误率 0.00% / P99 低值` → `[release] 全量发布` → `stage=DONE`。同时可验证稳定容器版本：

```bash
docker exec aiops-stable-order python -c "import os;print(os.environ.get('APP_VERSION'))"
```

### 6.2 场景 2：闸门 1 低置信度转人工

```bash
python demo_cli.py start --service order --alert-id demo-low --description "low-conf 演示"
python demo_cli.py result --wf-id aiops-fix-order-demo-low
```

预期：`stage=ESCALATED`，`gate_events` 含 `gate1:blocked:confidence=0.55<0.8`，且**不产生** patch。

### 6.3 场景 3：受保护目录二级审批

```bash
python demo_cli.py start --service order --alert-id demo-protected --description "protected 演示"
python demo_cli.py status --wf-id aiops-fix-order-demo-protected   # 等到 WAIT_APPROVAL
python demo_cli.py approve --wf-id aiops-fix-order-demo-protected  # 一级
python demo_cli.py status --wf-id aiops-fix-order-demo-protected   # 仍在 WAIT_APPROVAL，等二级
python demo_cli.py second-approve --wf-id aiops-fix-order-demo-protected
python demo_cli.py deploy-now --wf-id aiops-fix-order-demo-protected
python demo_cli.py result --wf-id aiops-fix-order-demo-protected
```

预期：`gate_events` 含 `gate2b:approved:protected-dir`。

### 6.4 场景 4：测试回炉后通过

```bash
python demo_cli.py start --service order --alert-id demo-retry --description "test-fail 演示"
python demo_cli.py result --wf-id aiops-fix-order-demo-retry
```

预期：`tests:failed:attempt=0` → `tests:passed:attempt=1` → 继续审批链路。

### 6.5 场景 5：重试耗尽转人工

```bash
python demo_cli.py start --service order --alert-id demo-exhaust --description "test-always-fail 演示"
python demo_cli.py result --wf-id aiops-fix-order-demo-exhaust
```

预期：`tests:failed:attempt=0/1/2` 后 `stage=ESCALATED`（不进入审批）。

### 6.6 场景 6：金丝雀劣化自动回滚

```bash
python demo_cli.py start --service order --alert-id demo-bad --description "canary-bad 演示"
python demo_cli.py approve --wf-id aiops-fix-order-demo-bad
python demo_cli.py deploy-now --wf-id aiops-fix-order-demo-bad
python demo_cli.py result --wf-id aiops-fix-order-demo-bad
```

预期：`[canary]` 日志显示错误率约 50%（注入劣化）→ `gate_events` 含 `canary:degraded->rollback` → `stage=ESCALATED`，`deploy_result.rolled_back=true`；稳定容器保持旧版本。

### 6.7 场景 7：窗口内补丁排队（FIFO，不重置倒计时）

```bash
# 流程进入 NOTIFYING 后：
python demo_cli.py queue-patch --wf-id aiops-fix-order-demo-001 \
  --new-wf-id aiops-fix-order-demo-002 --service order --alert-id demo-002
python demo_cli.py status --wf-id aiops-fix-order-demo-001   # queued_patches 出现 demo-002
python demo_cli.py deploy-now --wf-id aiops-fix-order-demo-001
# 当前流程结束后，系统自动以 aiops-fix-order-demo-002 启动新周期：
python demo_cli.py status --wf-id aiops-fix-order-demo-002
```

### 6.8 场景 8：窗口内取消（队列保留、队首自动承接）

```bash
python demo_cli.py queue-patch --wf-id aiops-fix-order-demo-001 \
  --new-wf-id aiops-fix-order-demo-003 --service order --alert-id demo-003
python demo_cli.py cancel --wf-id aiops-fix-order-demo-001
python demo_cli.py status --wf-id aiops-fix-order-demo-003      # 新周期已自动启动
```

预期：demo-001 `stage=CANCELLED`（`gate3:cancelled`），demo-003 进入 TRIAGING 开始新链路。

### 6.9 场景 9：审批超时自动驳回（演示 300s）

```bash
python demo_cli.py start --service order --alert-id demo-timeout
# 等到 WAIT_APPROVAL 后什么都不做，等待 300 秒
python demo_cli.py result --wf-id aiops-fix-order-demo-timeout
```

预期：~300s 后自动推进，`gate_events` 含 `gate2:timeout->reject`，`approvals` 含 `{"gate":"approval","decision":"timeout-reject"}`。

### 6.10 场景 10：告警自动触发链路（WP1/WP2 演示）

```bash
# 模拟错误日志突增 → 检测器注入告警 → Alertmanager → 接入服务 → workflow
python demo_log_generator.py --service nl2sql --mode surge --count 800
python log_surge_detector.py --services nl2sql --once
# 观察 alert_webhook.py 终端：已启动 workflow: aiops-fix-nl2sql-logsurge-XXXXXX
python demo_cli.py status --wf-id aiops-fix-nl2sql-logsurge-<HHMMSS>
```

> 注：该链路依赖本机 Loki/Alertmanager 栈可用（见《部署手册》第 5 章）；若不可用可用场景 1 代替演示。

---

## 7 审批卡片的回调说明（对接方）

### 7.1 飞书（应用机器人 + 长连接，无需自建服务）

审批卡片由**企业自建应用机器人**发送（群自定义机器人 webhook 的卡片按钮不支持回传，点击无反应）；卡片上的「批准 / 驳回」点击后，由 `feishu_gateway.py` 长连接网关接收回调并转发本系统 BFF 内置端点 `POST /api/im/card-callback`（免公网域名；应用创建、三要素配置与自检步骤见《部署手册》3.9）。点击后立即：

1. 网关收到 `card.action.trigger`（`feishu_gateway.log` 输出 `收到卡片回调 operator=ou_...`）并注入共享凭证转发 BFF；
2. 验签 → 解析按钮载荷（`wf_id/gate/decision`）与操作者身份（`event.operator`）；
3. 按 `AIOPS_IM_USERS` 判定操作者角色（未映射＝viewer，仅 operator 可审批）；
4. 写守卫（阶段允许 / 防重复）+ kill switch 检查 → 转发 Temporal 审批信号；
5. 响应 toast 提示并在卡片上就地替换为**处置结果卡**（绿=已批准 / 红=已驳回，无按钮，防重复点击）。

> 点击无反应的排查顺序：① 网关进程是否在运行（`./start.sh status`；需 `.env` 已配置 `AIOPS_FEISHU_APP_ID` / `AIOPS_FEISHU_APP_SECRET`，watchdog 才会加挂该组件）；② `feishu_gateway.log` 是否有「收到卡片回调」行——有则后续问题在 BFF（查 `bff.log` 的 401/503）；没有则检查开放平台「订阅方式」是否为长连接、`card.action.trigger` 是否已订阅、应用版本是否已发布并经管理员审批。

### 7.2 钉钉（外部网关转运）

钉钉 actionURL 以查询参数携带同一契约，需外置网关转发。网关接法：

1. 调用 `aiops_agent.notify.parse_callback(payload)` 解析；
2. 返回非 `None` 时得到 `(wf_id, gate, decision)`，校验操作人权限后转发对应信号：

| gate | decision | 转发信号 |
| --- | --- | --- |
| `approval` | `approve`/`reject` | `submit_approval(decision)` |
| `second_approval` | `approve`/`reject` | `submit_second_approval(decision)` |

3. 解析失败（返回 `None`）表示载荷缺失或取值非法，网关应直接拒绝。

---

## 8 常见问题（FAQ）

| 现象 | 原因与处理 |
| --- | --- |
| `start` 报「执行失败: ...」并提示确认 Temporal/worker | Temporal 未启动（`temporal server start-dev`）或 worker 未运行；按提示检查两个终端 |
| `start` 提示「当前未使用演示加速策略」 | 操作终端未 export `AIOPS_POLICY_PATH=./demo-policy.yaml`；本次流程将使用生产时长（30m/15m/5m） |
| `start` 报工作流已存在 | 幂等防重：同 service+alert-id 已有实例；换新 `--alert-id` |
| `status` 长时间停在 TRIAGING | 本地 LLM 推理慢或 Ollama 未启动；观察 worker 日志 `[triage]` 是否 degraded（degraded 会转人工） |
| 审批后一直停在 WAIT_APPROVAL | 受保护目录补丁需要二级审批；或信号发给了错误的 wf-id |
| 发布后没有金丝雀日志 | 流程可能处于 NOTIFYING 倒计时中，等待到期或执行 `deploy-now` |
| `[sandbox] FAIL` 且原因为「沙箱运行时不可用」 | Docker 未启动；启动 Docker 后重试（系统会自动回炉重试） |
| 通知只有日志没有真实 IM 消息 | 未配置 webhook（留痕模式，`delivery=recorded`）；真实投递需配置环境变量（见《部署手册》4.3） |
| 金丝雀劣化误报（P99 偏大） | 观测取窗口内近似 P99；本机首次构建/冷启动可能偏高；生产由 SLO 阈值与窗口时长控制 |
| 重复演示同一 alert-id 没有产生新流程 | 幂等设计；使用新 alert-id（如 `demo-001b`） |
| 审批/发布指令返回 `delivery=pending` | 命令已登记，Temporal 暂不可达；后台自动退避重试，用 `/api/commands/<command_id>` 查看进度，不要重复提交 |

---

## 9 生产环境与控制台运维操作

### 9.1 双模环境切换

同一套代码两种运行档位；切换只需改环境变量并重启进程（详见《部署手册》第 5 章）：

| 档位 | 声明方式 | 数据面 | 适用 |
| --- | --- | --- | --- |
| `demo`（演示） | `export AIOPS_MODE=demo`（`scripts/demo-up.sh` 自动） | `data/` 文件后端，零外部依赖 | 演示、联调、评测 |
| `production`（默认） | 不设或 `export AIOPS_MODE=production` | MySQL 5 表 + `data/prod/`；**未配置 `AIOPS_DATABASE_URL` 拒绝启动** | 生产运行 |

> 两档数据互不影响：demo 的历史演示数据原地保留，production 使用全新 MySQL 库；测试永远强制 demo，不会误触生产库。

> 单机生产入口（E/F 产物，2026-10-13；**已于 2026-10-13 维护窗口投用**）：production 档全部组件仅监听 loopback，控制台经本机 Caddy 以 **https://<本机IP>:8643**（或 https://127.0.0.1:8643）进入；直连 `127.0.0.1:8600` 仅限本机受控操作。局域网/IM 网桥等外部访问一律走 8643；监控/凭证接线已完成（见《部署手册》第 9 章与 10.6），外部设备访问仍需先完成 CA 证书分发。

### 9.2 控制台日常运维

控制台访问方式分两档：

- **demo 档**：BFF 监听 `0.0.0.0`——本机 **http://127.0.0.1:8600**，局域网设备 **http://<本机IP>:8600**（IP 以 `bash scripts/demo-up.sh status` 显示或 `ipconfig getifaddr en0` 为准）。
- **production 档**（见《部署手册》第 9 章）：BFF 收敛为仅 loopback，须经本机 Caddy 入口 **https://<本机IP>:8643** 访问（直连 `127.0.0.1:8600` 仅限本机）。

**登录方式（用户名密码与静态令牌两种通道并存）**

- **用户名 + 密码（日常主通道）**：首启自动播种管理员 `admin`，初始密码 `admin123`（部署时可用 `AIOPS_BOOTSTRAP_ADMIN_PASSWORD` 覆盖）。**首次登录后请立即在右上角用户菜单 →「修改密码」改密**（需原密码；成功后该账号全部会话吊销，需用新密码重新登录）。会话默认 12 小时、活跃自动续期；生产档用户与会话存于 MySQL `console_users` / `console_sessions`（仅存密码哈希与会话指纹），demo 档落在 `data/console_users.json` / `data/console_sessions.json`。
- **静态访问令牌（自动化 / 应急通道）**：右上角用户菜单 →「访问令牌（静态通道）」填入 Bearer 令牌，适合脚本、CLI 与登录页不可用时的应急排障；`.env` 的 `AIOPS_CONSOLE_AUTH_TOKENS` 只在首启播种，运行期以注册表为准（请勿把令牌贴进工单或聊天）。

两种身份权限一致（viewer / operator / admin 角色不同），日常人工操作建议走用户名密码，避免长期令牌外泄。用户增删、角色分配、会话吊销与密码重置在「用户管理」（admin）。

导航为四级分组（「总览 / 跨系统待办 / 被监控系统 / 平台」，2026-10 控制台重构后）：

| 分组 | 页面 | 操作 | 说明 |
| --- | --- | --- | --- |
| 总览 | 系统总览 | 运营度量看板 | 值班告警 / MTTR·MTTA / 自动完成率 / 人工干预率 / 分类型·分服务·分风险 / 证据与采集 / 沙箱与补丁 / 待办与知识（数据源：production 走 `workflow_runs` 等 SQL 聚合，demo 走 Temporal 读侧） |
| 总览 | 全部流程 | 查看流程列表与详情 | 点击行在其下端展开行内简要流程图（绿=已完成 / 强调色=当前进行中，待审批与公告期为黄 / 灰=未到达 / 红叉=转人工断点，终态按闸门事件链精确标注中断点与原因）；详情含根因 / 补丁 diff / 测试报告 / 发布结果 |
| 跨系统待办 | 审批中心 | 审批（闸门 2） | operator 及以上；受保护目录自动要求二级审批 |
| 跨系统待办 | 转人工待办 | 处置 ESCALATED 流程 | 登记处理结果 / 关闭待办（含 SLA 巡检提示）；状态页签：全部 / 待指派 / 已指派 / **重试跟踪** / 已关闭；表格按固定列宽铺满内容区，监控系统 / 状态 / 升级原因 / 告警·工作流 / 责任人 / 登记·SLA / 日志证据 / 操作全部一屏可见（长文本悬停看全文），点击「工作流」ID 进入流程详情；「日志证据」列可弹窗核对 TRIAGING 归档的原始日志与模板聚类（无归档显示「未归档」） |
| 跨系统待办 | 发布窗口 | 发布窗口指令（闸门 3） | 立即发布 / 取消 / 排队补丁；倒计时以服务端时间为准 |
| 被监控系统 | 系统清单管理 | 应用清单维护 | admin；增删改应用（URL / service / 日志路径 / 备注）；「从标准接口探测」一键回填 |
| 被监控系统 | （动态系统列表） | 系统工作台 `/systems/:appId` | 每个已登记系统的专属入口：探测状态 / 巡检结果 / 日志 / 修复历史 |
| 平台 | 需求基线 | 需求分析闭环 | 数据源支持按被监控系统切换与**「全部系统」聚合**（跨系统并行拉取合并展示，条目带来源系统列、可按来源筛选/统计，逐系统容错）；拉取需求 → 智能分析 → 反馈迭代 → 批准进入修复；影子档建议归档评估后可续接执行完整链路 |
| 平台 | 审计回看 | 审计查询与证据导出 | 流程表游标分页（每页 100，可加载更早）、数据源徽标（`SQL 数据面` / `Temporal 回退`）；操作审计独立分页，支持操作者 / 动作过滤；展开行看统一时间线并可下载证据包；admin 可校验审计哈希链 |
| 平台 | 系统状态 | 配置与开关 | 策略来源与配置项生效方式（hot/restart/snapshot）、限速与 kill switch 状态、并发配额（本地 LLM / Qoder / 沙箱 / 发布各自的并发上限与锁定项） |
| 平台 | 用户管理（admin） | 用户与会话 | 创建/停用用户、角色分配、会话吊销 |
| 平台 | 帮助中心 | 使用帮助 | 常见操作指引 |
| — | 右上角用户菜单 | 身份与令牌 | 查看当前身份（会话/静态令牌）、修改密码、登出；「访问令牌」弹窗查看令牌状态 / 清单 / 触发轮换（仅自动轮换开启时显示「立即轮换」） |

**转人工待办的重试跟踪与送达（M3，2026-11-08 起）**

- **重试不再立即关闭**：点「重试」后待办进入「重试跟踪」（暂停 SLA 计时，但仍计入开放统计），行内出现重试流程链接与终态（跟踪中 / 已成功 / 再次升级 / 执行失败 / 已取消 / 影子观察）。
  新流程 `DONE` 由后台自动结构化关闭（详情标注「后台重试跟踪自动关闭」，不自动进知识库候选）；再次升级 / 失败 / 取消 / 影子观察会重新打开原待办并重置通知周期；重试跟踪中不能再次点「重试」。
- **关闭必须结构化**：仅备注关闭已停用，旧客户端会收到 400 迁移提示；历史仅备注记录仍可查看但其详情不再作为新关闭范例。
- **并发冲突不覆盖**：所有写操作回传页面读取时的 `version`，页面停留过久后提交会返回 409（已被其他操作更新），刷新确认最新状态后再操作。
- **再升级以送达为准**：行内「已送达」才表示通知成功；「仅留档 / 投递失败」会显示次数与失败原因，后台按 1 / 5 / 15 / 30 / 60 分钟退避重试并保持「已超时（通知待送达）」，不会伪造已通知。

**审计回看、统一时间线与证据包（M4，2026-11-08 起）**

- **流程审计分页**：审计页流程表改为游标分页（每页 100 条，底部「加载更早的审计记录」），不再只显示最近一批；顶部徽标标明数据源（production `SQL 数据面` / demo `Temporal 回退`）。只有第一页参与 30s 轮询，翻页后不重复轮询。
- **操作审计独立分页**：切到「操作审计」页签后走 `/api/audit/ops`，可按操作者 / 动作过滤并继续向前翻页；展开任意流程行可看统一时间线（History / 闸门 / 命令投递 / 通知 / 人工待办 / 发布节点），无真实时间的旧节点标 `~`。
- **流程详情时间线**：详情页「时间线」页签读取 `GET /api/flows/{wf_id}/timeline`；运行中每 5s 刷新，头部显示 History 事件数、已完整 / 投影中 / 断档待对账与同步延迟。投影不可用时回退到闸门事件，不显示空白。
- **证据包下载**（operator 及以上）：详情页或审计展开行点「证据包」，下载自包含 JSON（身份、闸门与审批、Temporal 事件、日志证据、测试、通知、发布 / 回滚、人工处置、重试链）并附 manifest 与 `root_sha256`。导出动作自身写操作审计，提示中会显示审计 ID 与 root 前 16 位；History 未完整时明确提示「不得视为最终历史」。
- **完整性自检**（admin）：`GET /api/audit/integrity` 校验审计哈希链并返回归档清单；修改 / 插入 / 删除链中记录都会在校验结果中暴露（`ok=false` + `broken_at`）。该能力为 **tamper-evident（本机可发现篡改）**，不等同外部 WORM。
- **清理语义**：过期审计不再按 TTL 直接删除——先归档并校验链与 SHA-256，复核通过后才 purge 原始文件 / 表行；未归档记录不会被静默删除，归档副本保留在 `data/web-audit/archive/`（demo）或部署配置的归档目录（production）。

**运营度量看板读法**

- **先看数据源与 `degraded`**：标题右侧标明 `SQL 镜像` 或 `Temporal 读侧`，并显示镜像同步时间。
  出现「部分分量不可读」提示时，对应卡片按空窗口展示——先修数据面，不要据此下结论。
- **比率「暂无数据」= 分母为 0**，不是 0%。`0%` 只可能来自非零分母，是真实观测值。
- **值班告警置顶**：`danger` / `warn` / `unknown` 三档；`unknown` 表示数据不足、无法判断，
  不等于正常。点「Runbook」在右侧抽屉查看该 code 的处置步骤（来源 `docs/AIOps 值班 Runbook.md`）。
- **口径边界**：真实沙箱通过率只认 `UNIT_TEST`，`CONTRACT_TEST` / `STATIC_ONLY` 单列；
  观测面异常（Loki 不可达 / 无日志 / 标签未映射）不计入模型低置信；模型成本与知识采用情况
  在计量接入前显示「暂无数据」。
- 时间窗默认 7 天（接口上限 30 天），切换窗口后所有维度同步重算。

**写命令查询（`/api/commands`，2026-11-08 起）**

审批、发布窗口与排队补丁现在都是先登记「意图审计 + 待投递命令」，再尝试投递；Temporal 暂不可达不再直接报错丢失操作：

```bash
# 查看积压命令（可按状态 / 工作流过滤）
curl -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8600/api/commands?status=PENDING'
# 查看单条命令（SUBMITTED 会补查一次业务回执，不重发信号）
curl -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8600/api/commands/<command_id>'
```

- `PENDING`=已登记待重试；`SUBMITTED`=已送达 Temporal（业务结果以流程状态为准）；`ACCEPTED`=工作流已记录该命令；`FAILED`=重试耗尽或业务拒收（`last_error` 给出原因）。
- `GET /api/subsystems` 的 `commands` 子系统展示积压时长与 24h 失败数；积压超过 `AIOPS_COMMAND_BACKLOG_ALERT_SECONDS`（默认 300s）即降级提示。
- 审计中每次写操作都有两行：意图行 `result=pending` 与结果行 `signaled / rejected / failed`，两行均带同一个 `command_id`。

**确定性风险、发布窗口快照与上线硬门禁（M5，2026-11-08 起）**

- **风险等级不再看 LLM 措辞**：系统按补丁文件清单、影响服务、受保护目录、验证方式与降级标记做确定性判定（同一输入恒定同一等级与同一原因顺序）。受保护目录 / 敏感路径（认证、支付、权限、密钥、数据迁移）/ 降级补丁直接 HIGH；配置与发布基础设施、多服务、改动文件 ≥5、非真实测试累计 2 分一档。补丁卡片上的模型风险描述仅作背景参考，不参与闸门。
- **HIGH 的双重代价**：必须提供 `UNIT_TEST`（真实执行）验证结果，`STATIC_ONLY` / `CONTRACT_TEST` / `UNKNOWN` 一律转人工；同时触发双人审批（与受保护目录取「或」，不重复计数）。审批中心会显示命中的原因码（如 `protected-path` / `sensitive-path:auth` / `degraded-bypass`），审批人据此核对，不再依赖文字描述。
- **发布窗口指令看启动快照**：流程启动时固化当时的策略（含 `allow_deploy_now` / `allow_cancel`）。运行中修改 YAML **不会**放开已启动流程：控制台点「立即发布 / 取消」会返回 409 并说明被禁原因；即使绕过控制台直接发信号，工作流同样拒收并记入 `command_rejections`（`policy-disabled`），该次拒收不占用命令 ID。需要新策略时**重新发起流程**取得新快照，或先联系 admin 确认策略文件。
- **系统状态页多一项 tracing**：`/api/subsystems` 与 `readyz` 会给出追踪状态（`otlp_not_configured / otlp_configured_not_initialized / otlp_init_failed / otlp_initialized`）。production 若开启 `AIOPS_OTEL_REQUIRED=true` 却未配置 `AIOPS_OTEL_ENDPOINT`，服务启动即失败（不伪造就绪）；这一项只校验「已配置并初始化」，Collector 是否真正收到数据仍需在观测平台核对。
- **上线硬门禁新增三项**（`scripts/rollout_gate.py`，对所有阶段生效）：`audit-integrity` 审计哈希链校验通过、`command-backlog` 命令 0 积压 / 0 失败 / 最老等待 <300s、`notify-delivery` 升级通知 0 条未送达。任何一项数据读不到都记为 `unknown`（pending），**不要当作通过**；发布前先在审计页与系统状态页确认这三项为绿。
- **值班演练**：`scripts/governance_drill.py` 在本机复现审计篡改、归档后链续接、命令重放 / 冲突、过期审批（超时后迟到审批与旧 run 信封被拒）、通知送达失败、History 断档、250 条事件分页七类异常并断言系统能发现，退出码 0 为全通过；该脚本只在临时目录操作，不影响现有数据。

### 9.3 紧急停止（kill switch）

用途：一键冻结自动修复——**BFF 拒绝一切写操作、webhook 拒绝启动新流程**；已在跑的流程不受影响，可继续完成或人工处理。

```bash
# 激活（admin 令牌；控制台「系统状态」页亦有同款开关）
curl -X POST http://127.0.0.1:8600/api/system/kill-switch \
  -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  -d '{"active": true, "reason": "生产故障，暂停自动修复"}'

# 关闭（恢复正常）
curl -X POST http://127.0.0.1:8600/api/system/kill-switch \
  -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  -d '{"active": false, "reason": "故障已定位"}'
```

- 激活/关闭均落操作审计（actor + reason + 时间）；
- 局域网内操作（demo 档）：把上述 URL 的 `127.0.0.1` 换成本机 IP（控制台监听 `0.0.0.0`，见 9.2）；production 档改为 Caddy 入口 `https://<本机IP>:8643`；
- 状态不可读时**fail-closed**（宁可拦住，不可放行）；
- 生产档状态存 MySQL `system_kv`（多副本一致）；demo 档存 `data/kill_switch.json`。

### 9.4 数据清理（TTL）

```bash
# 生产：预演（只统计不删除），确认真实删除请去掉 --dry-run
.venv/bin/python -m aiops_agent.cleanup --dry-run

# 演示环境 / 手动
.venv/bin/python demo_cli.py cleanup --dry-run
```

保留期（生产）：审计与流程执行记录 365 天、通知与 ArgoCD 留痕 90 天、沙箱与 Qoder 工作区 7 天；演示档更短。K8s 部署内置 CronJob 每日 03:00 自动执行。

### 9.5 生产环境常见问题（与演示 FAQ 互补）

| 现象 | 原因与处理 |
| --- | --- |
| 启动即报「production 模式必须配置 AIOPS_DATABASE_URL」 | fail-fast 保护；配置连接串或显式 `AIOPS_MODE=demo` |
| 所有 `/api` 返回 503 | 未配置令牌（fail-closed）；播种 `AIOPS_CONSOLE_AUTH_TOKENS` 并重启 |
| 登录页 401「用户名或密码错误」 | 首启管理员为 `admin` / `admin123`（或部署时设的 `AIOPS_BOOTSTRAP_ADMIN_PASSWORD`）；改密后用新密码。忘记密码由其他 admin 在「用户管理」重置，应急可用静态令牌登录 |
| 401「凭证无效或已过期」 | 令牌错误或被重置注册表吊销；读注册表取当前令牌（部署手册 4.1） |
| 局域网打不开 8643 / 证书域名不符 | 主机 LAN 地址被 DHCP 变更；执行 `./start.sh sync-ip` 同步 Caddy 站点与证书 SAN（排障见部署手册 10.7） |
| 写操作返回 429 | 触发限速（写 20/min）；稍后重试，或按容量调 `AIOPS_RATE_LIMIT_WRITE_PER_MIN` |
| 认证失败连续 429 | 按 IP 的防爆破限速（10/min）；等待窗口恢复 |
| 流程列表与 Temporal 不一致 | 生产档由 `workflow_runs` 同步任务周期补齐；检查 BFF 日志同步错误 |
| 直连 8600 / 9091 返回 404 或无响应 | 正常：production 档指标/健康端点仅接受 loopback 对端；从 Caddy 入口（8643）访问，或确认请求确发自本机 |
| 运行演示脚本被拒绝（ModeConfigError） | 演示工具在 production 档 fail-closed（如 `demo_cli.py` / `demo_log_generator.py`）；演示请显式 `AIOPS_MODE=demo` |
| IM 群指令无响应 | production 未配置 `AIOPS_IM_TOKEN` 或未接可信网桥时通道整体关闭（不回落常规鉴权）；如已配置，确认发送者在 `AIOPS_IM_USERS` 映射内 |

---

## 附录 A 命令速查卡

```bash
# 一键启动（受控）：档位横幅 + 控制台构建检查 + 依赖预检 + 常驻组件（默认 6 个，含 BFF 控制台；配飞书应用凭证后加挂长连接网关）+ 组件守护（不自动灌日志/起流程）
./start.sh                         # 无隐式动作；./start.sh web 加前端热更；底层即 demo-up.sh
./start.sh status                  # 查看组件与守护状态、生效档位快照
./start.sh build                   # 仅重建控制台静态产物（web/dist）
./start.sh doctor                  # 汇总自检：依赖预检 + 环境自检 + 组件状态 + 构建新鲜度
./start.sh logs bff                # 跟踪组件日志（默认 watchdog，all 看全部）
./start.sh sync-ip                 # 网络地址变化后同步生产部署（Caddyfile/settings/监控片段 + 重签 TLS、reload Caddy）

# 控制台：demo 档本机 http://127.0.0.1:8600、局域网 http://<本机IP>:8600；production 经 Caddy https://<本机IP>:8643
.venv/bin/python -m uvicorn bff.app:app --host 0.0.0.0 --port 8600

# 环境准备（每个操作终端）
cd aiops-agent && export AIOPS_POLICY_PATH=./demo-policy.yaml

# 启动 / 查询 / 结果
python demo_cli.py start --service order --alert-id <id> [--description "<关键词>"] 
python demo_cli.py status --wf-id aiops-fix-order-<id>
python demo_cli.py result --wf-id aiops-fix-order-<id>

# 审批
python demo_cli.py approve        --wf-id <wf> [--decision approve|reject]
python demo_cli.py second-approve --wf-id <wf> [--decision approve|reject]

# 发布窗口
python demo_cli.py deploy-now  --wf-id <wf>
python demo_cli.py cancel      --wf-id <wf>
python demo_cli.py queue-patch --wf-id <wf> --new-wf-id <next_wf> --service order --alert-id <id2>

# 感知侧（可选）
python demo_log_generator.py --service nl2sql --mode surge --count 800
python log_surge_detector.py --services nl2sql --once

# 生产模式（production；见第 9 章）
export AIOPS_MODE=production AIOPS_DATABASE_URL='mysql+pymysql://<user>:<pass>@<host>:3316/aiops?charset=utf8mb4'
export AIOPS_REDIS_URL='redis://127.0.0.1:6380/0' AIOPS_POLICY_PATH=./release-gate-policy.yaml
python scripts/production-smoke.py            # 生产冒烟 9 项
python -m aiops_agent.cleanup --dry-run       # 清理预演
bash scripts/security-scan.sh                 # SAST（Bandit，HIGH 必须为 0）

# 运维治理（P0-P2）
bash scripts/cleanup-sandbox.sh --status      # 数据清理：查看沙箱/Qoder 占用（--dry-run 预演 / --days N 保留期）
bash scripts/rotate-logs.sh                   # 日志轮转：>1MB 的 *.log 压缩轮转（copytruncate，进程免重启）
python scripts/backup-data.py                 # 数据备份（MySQL + 关键 JSON → backups/）；--restore-test 恢复演练
bash scripts/launchd-install.sh install       # 组件守护交 launchd 托管（登录自启/崩溃自动重启）；status/uninstall 管理

# 对外通道（可选；详见部署手册 §3.8 / §3.9）
curl -X POST http://127.0.0.1:8600/api/im/command -H "Authorization: Bearer $AIOPS_IM_TOKEN" \
  -H "Content-Type: application/json" -d '{"sender":"ou_xx","text":"待办"}'   # IM 群指令（ChatOps）手工验证
.venv/bin/python mcp_server.py               # MCP server（stdio；需装 requirements-mcp.txt 并设 AIOPS_MCP_TOKEN）
```

## 附录 B 阶段字典

| stage | 含义 | 运维动作 |
| --- | --- | --- |
| DETECTED / TRIAGING | 接收告警 / 根因分析中 | 观察 |
| FIXING / TESTING | 补丁生成 / 沙箱验证（可能回炉） | 观察；多次 `tests:failed` 说明修复困难 |
| WAIT_APPROVAL | 等待审批（含二级） | 审批；超时自动驳回 |
| NOTIFYING | 公告 + 倒计时 | 可 deploy-now / cancel / queue-patch |
| CANARY / ROLLING_OUT | 金丝雀观测 / 发布终态 | 观察 |
| DONE | 成功闭环 | 归档 |
| ESCALATED | 转人工 | 接手人工处理（见升级卡片原因） |
| CANCELLED | 已取消（队列保留） | 确认队列承接的新周期 |

## 附录 C gate_events 速查

| 事件 | 含义 |
| --- | --- |
| `gate1:blocked:confidence=x<y` | 置信度不足转人工 |
| `tests:failed:attempt=n` / `tests:passed:attempt=n` | 沙箱测试（按轮次） |
| `mr:created:!xxxx` | MR 创建（`live` 真实创建；未配置 `recorded` / 失败 `degraded` 桩——worker 日志 `[mr]` 行含 mode 与原因） |
| `gate2:approved` / `gate2:reject` / `gate2:timeout->reject` | 一级审批 |
| `gate2b:*` | 二级审批（protected-dir） |
| `gate3:announced:vX.Y.Z` | 公告版本 |
| `gate3:command:deploy_now` / `gate3:command:cancel` / `gate3:cancelled` | 窗口指令 |
| `gate3:patch-queued:<alert_id>` / `queue:next-cycle-started:<alert_id>` | 排队与承接 |
| `canary:healthy->full-rollout` / `canary:degraded->rollback` | 发布终态 |
