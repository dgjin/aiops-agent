# AIOps 自动运维智能体系统用户操作手册

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.3 |
| 日期 | 2026-10-01（v1.3 修订 2026-10-13） |
| 适用对象 | 运维值班人员、审批人（含二级审批人）、演示人员、联调开发 |
| 关联文档 | 《系统设计说明书》、《部署手册》 |
| 修订记录 | v1.1：控制台四级导航（总览/跨系统待办/被监控系统/平台）与局域网访问；一键启动与组件守护指引（demo-up.sh）。v1.2（2026-10-13）：production 档收敛 loopback、经本机 Caddy HTTPS（8643）进入（见 9.1/9.2/9.3/9.5；产物未运行验收）；v1.3（2026-10-13）：IM 流程通知补齐（接收/需求接收/进入修复状态消息，卡片共七类）；生产档切换全链路执行（`shadow_mode: false`，MR → 审批 → 公告 → 金丝雀）；v1.4（2026-10-13）：影子建议续接——影子档（SHADOWED）建议归档评估后可续接执行完整链路（复用原补丁、不重新生成；`POST /api/requirements/analyses/{id}/resume-shadow`，见 5.4） |

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
# 放行
python demo_cli.py approve --wf-id aiops-fix-order-a-1001 --decision approve
# 驳回
python demo_cli.py approve --wf-id aiops-fix-order-a-1001 --decision reject
```

- 收到审批卡片（飞书/钉钉）后，也可直接点击卡片上的「批准 / 驳回」按钮，由回调网关转发等价信号；
- **超时策略（锁定）**：规定时间（演示 300s / 生产 30m）内无响应 → **自动驳回**并升级值班负责人（`gate2:timeout->reject`）；驳回即关闭流程并留痕。

#### 4.3.2 二级审批（受保护目录）

当补丁命中 `auth/**`、`payment/**`、`crypto/**` 时，需要**两级审批**：

1. 一级审批通过后，日志出现 `!! 命中受保护目录，需二级审批（submit_second_approval）`；
2. 二级审批人执行：

```bash
python demo_cli.py second-approve --wf-id aiops-fix-order-a-1001 --decision approve
```

二级审批同样有超时自动驳回（`gate2b:timeout->reject`）。两级都通过才进入公告窗口。

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
| `批准` / `驳回 <流程ID前缀>` | 一级审批决策（闸门 2） | operator |
| `二级批准` / `二级驳回 <流程ID前缀>` | 二级审批决策（受保护目录流程） | operator |

一段典型的群内对话：

```text
[值班人]    @AIOps助手 待办
[AIOps助手] 待审批流程（2 个）：
              1. aiops-fix-order-a-1001（order）· 补丁 p-a-1001-r1（受保护目录，需二级审批）
              2. aiops-fix-coupon-a-1002（coupon）· 补丁 p-a-1002-r1
              发送「批准 <ID前缀>」或「驳回 <ID前缀>」处置。

[值班人]    @AIOps助手 批准 aiops-fix-order-a-1001
[AIOps助手] ✓ aiops-fix-order-a-1001：一级审批已批准（操作者 ou_xxxx）
```

> 要点：
> - 流程 ID 支持**唯一前缀**；前缀命中多条时机器人回复候选列表，补充字符后再发即可；
> - 发送者未在 `AIOPS_IM_USERS` 中映射时按 **viewer** 处理（仅可查询）；审批类命令要求 operator 及以上；
> - 审批安全语义与控制台完全一致：复用同款写守卫（仅运行中 + 等待审批 + 防重复）、kill switch 与操作审计（审计 actor 记为 `im:<发送者>`）；
> - 命令级失败（无权限 / 流程已结束 / 前缀不唯一等）会以文字直接回复原因，不会静默失败。

---

## 5 通知卡片说明

系统向 IM（飞书/钉钉）推送七类卡片；未配置 webhook 时自动改为**留痕模式**（保存到 `data/notify/*.json`，仅用于演示审计）。流程进行中会持续推送状态消息：**接收**（流程启动）→ **进入修复**（分析完成）→ 审批/公告 → 终态（影子档建议或转人工）。

### 5.1 审批卡片（橙色）

内容：根因（含置信度）、补丁（编号/风险/文件）、完整 diff、测试报告（单测/回归/SAST）、回滚预案；受保护目录追加「需二级审批」提示。

按钮：**批准**（`decision=approve`）、**驳回**（`decision=reject`）。按钮载荷契约：

```json
{"wf_id": "aiops-fix-order-a-1001", "gate": "approval", "decision": "approve"}
```

### 5.2 用户公告卡片（蓝色）

内容：版本号（`v1.0.X`）+ 影响提示 + 服务/补丁标识。无操作按钮；用户反馈通道不触发自动取消（是否取消由运维通过 `cancel` 决定）。

### 5.3 升级卡片（红色）

转人工时推送（超时驳回、测试耗尽、连续失败、金丝雀劣化等），内容含转人工原因、严重级别与处置动作（联系值班负责人人工处理）。

### 5.4 影子档建议卡片（青色）

影子档（`shadow_mode=true`）时，沙箱测试通过后推送修复建议：根因、建议补丁与完整 diff、沙箱测试结果，并注明「未创建 MR、未进入审批与发布，仅生成建议供人工参考」；流程到此为终态（`SHADOWED`，控制台显示「已建议（影子档）」）。这是渐进信任策略；当前生产档为**全链路**（`shadow_mode=false`，2026-10-13 起）：沙箱测试通过后创建 MR → 审批 → 公告倒计时 → 金丝雀发布；演示档 `demo-policy.yaml` 同为全链路。归档评估后决定采纳时，可对影子建议执行**续接**（控制台需求分析闭环会话内 / `POST /api/requirements/analyses/{analysis_id}/resume-shadow`，admin 角色）：不重新分析、不重新生成补丁，以原建议补丁续接执行完整链路（完整沙箱验证 → MR → 审批 → 公告 → 金丝雀发布）；续接流程 id 固定 `aiops-req-{analysis_id}-v{version}-resume1`（防重复续接），已发布成功（终态）的需求不可续接，未批准会话不可续接（均 409 精确拒绝）。留痕前缀 `shadow-`。

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

卡片按钮点击后，IM 平台会回调部署的回调网关。网关需：

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

首次使用需在登录页填入访问令牌（viewer / operator / admin 角色权限不同）。

导航为四级分组（「总览 / 跨系统待办 / 被监控系统 / 平台」，2026-10 控制台重构后）：

| 分组 | 页面 | 操作 | 说明 |
| --- | --- | --- | --- |
| 总览 | 系统总览 | 看板 | 流程统计 / 待办计数 / 最近发布（数据源：Temporal + `workflow_runs`，生产档跨实例一致） |
| 总览 | 全部流程 | 查看流程列表与详情 | 点击行在其下端展开行内简要流程图（绿=已完成 / 强调色=当前进行中，待审批与公告期为黄 / 灰=未到达 / 红叉=转人工断点，终态按闸门事件链精确标注中断点与原因）；详情含根因 / 补丁 diff / 测试报告 / 发布结果 |
| 跨系统待办 | 审批中心 | 审批（闸门 2） | operator 及以上；受保护目录自动要求二级审批 |
| 跨系统待办 | 转人工待办 | 处置 ESCALATED 流程 | 登记处理结果 / 关闭待办（含 SLA 巡检提示） |
| 跨系统待办 | 发布窗口 | 发布窗口指令（闸门 3） | 立即发布 / 取消 / 排队补丁；倒计时以服务端时间为准 |
| 被监控系统 | 系统清单管理 | 应用清单维护 | admin；增删改应用（URL / service / 日志路径 / 备注）；「从标准接口探测」一键回填 |
| 被监控系统 | （动态系统列表） | 系统工作台 `/systems/:appId` | 每个已登记系统的专属入口：探测状态 / 巡检结果 / 日志 / 修复历史 |
| 平台 | 需求基线 | 需求分析闭环 | 拉取被监控系统需求 → 智能分析 → 反馈迭代 → 批准进入修复；影子档建议归档评估后可续接执行完整链路 |
| 平台 | 审计回看 | 审计查询 | 生产档读 `audit_events` 表；支持按 actor / 动作 / 时间窗聚合报表 |
| 平台 | 系统状态 | 配置与开关 | 策略来源与配置项生效方式（hot/restart/snapshot）、限速与 kill switch 状态 |
| 平台 | 用户管理（admin） | 用户与会话 | 创建/停用用户、角色分配、会话吊销 |
| 平台 | 帮助中心 | 使用帮助 | 常见操作指引 |
| — | 左下角「访问令牌」 | 令牌状态 | 查看自身令牌状态 / 清单 / 触发轮换（仅自动轮换开启时显示「立即轮换」） |

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
| 401「凭证无效或已过期」 | 令牌错误或被重置注册表吊销；读注册表取当前令牌（部署手册 4.1） |
| 写操作返回 429 | 触发限速（写 20/min）；稍后重试，或按容量调 `AIOPS_RATE_LIMIT_WRITE_PER_MIN` |
| 认证失败连续 429 | 按 IP 的防爆破限速（10/min）；等待窗口恢复 |
| 流程列表与 Temporal 不一致 | 生产档由 `workflow_runs` 同步任务周期补齐；检查 BFF 日志同步错误 |
| 直连 8600 / 9091 返回 404 或无响应 | 正常：production 档指标/健康端点仅接受 loopback 对端；从 Caddy 入口（8643）访问，或确认请求确发自本机 |
| 运行演示脚本被拒绝（ModeConfigError） | 演示工具在 production 档 fail-closed（如 `demo_cli.py` / `demo_log_generator.py`）；演示请显式 `AIOPS_MODE=demo` |
| IM 群指令无响应 | production 未配置 `AIOPS_IM_TOKEN` 或未接可信网桥时通道整体关闭（不回落常规鉴权）；如已配置，确认发送者在 `AIOPS_IM_USERS` 映射内 |

---

## 附录 A 命令速查卡

```bash
# 一键启动（受控）：依赖预检 + 6 常驻组件（含 BFF 控制台）+ 组件守护（不自动灌日志/起流程）
./start.sh                         # 无隐式动作；./start.sh web 加前端热更；底层即 demo-up.sh
./start.sh status                  # 查看组件与守护状态、生效档位快照
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
