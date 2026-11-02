# AIOps 值班 Runbook

> 入口：控制台「系统总览 → 运营度量 → 值班告警」，或 `GET /api/metrics/ops?days=7` 的 `duty_alerts`。
> 每条告警含 `code` / `level` / `value` / `threshold` / `detail` / `runbook`；面板点击「Runbook」
> 时由 `GET /api/metrics/ops/runbook?code=...` 读取本文对应小节（**同源，不另存副本**）。
> `level=unknown` 表示**数据不足、无法判断**，不是「正常」。禁止把 unknown 当成绿色。

## 0. 通用读法

- **数据面**：production 走 `workflow_runs` / `audit_events` / `log_evidence` 的 SQL 聚合
  （`source=warehouse`，`mirror_updated_at` 为最近一次 `/api/flows` 镜像时间）；镜像从未同步或
  DB 不可达时回退 Temporal 读侧并置 `degraded=warehouse-empty` / `warehouse`。
- **`degraded` 非空 = 对应分量不可读**，面板按空窗口展示。先修数据面（DB / Temporal / 审计 /
  待办存储 / 心跳文件），再解读指标；不要在分量缺失时下结论。
- **空样本**：分母为 0 的比率一律 `null`，页面显示「暂无数据」。看到 `0%` 说明分母非 0，
  是真实观测值；看到「暂无数据」不要当成 0%。
- **口径边界**：真实沙箱通过率只认 `UNIT_TEST`；`CONTRACT_TEST` / `STATIC_ONLY` 单列。
  观测面异常（Loki 不可用 / 无日志 / 日志过期 / 服务标签未映射）**不得**计入模型低置信。
- **处置留痕**：所有关闭走转人工待办的结构化关闭（`close_reason` / `actual_root_cause` /
  `verification` / `verdict`），不要直接改库状态。
- **升级路径**：值班（10 分钟判定）→ SRE（30 分钟未恢复）→ 研发值班（影响面扩大或需改代码）。
  每次升级在待办 `note` 中记录时间与结论。

## log-stall 日志采集停滞

**判定**：采集心跳（`<data_root>/log_ship_health.json`，production 为 `data/prod/`）存在不健康服务；
不健康服务数 ≥3 → `danger`，1–2 → `warn`；**无心跳文件** → `unknown`（不能判断，不等于正常）。

**立即动作**

1. `python scripts/aiops-watchdog.py status`：看采集组件是否存活、心跳是否新鲜。
2. `python ship_app_logs.py --audit-labels`（必要时加 `--check-loki`）：比对注册表与采集清单，
   检查近 60 分钟是否有日志。
3. 打开 `log_ship_health.json`，逐服务看 `last_success_at` / `consecutive_failures` / `last_error`。

**常见原因**：Loki 不可达、应用日志路径变更、`service` 标签未映射、采集位点损坏
（`data/log_ship_positions.json`）、文件权限变化。

**恢复验证**：`/api/metrics/ops` 的 `logship.unhealthy=0`、`logship.freshness_p95_seconds < 300`；
`--check-loki` 能看到近 60 分钟日志；面板 `log-stall` 条目消失。

**不要**：把「采集进程活着」当成「日志供给正常」；无心跳时写「正常」。

## escalation-backlog 待办积压

**判定**：存在超过 SLA（`AIOPS_ESCALATION_SLA_MINUTES`，默认 30 分钟）仍未处置的开放/已指派待办 →
`danger`；待办存储不可读 → `unknown`。

**立即动作**

1. 控制台「转人工待办」按状态排序，先处理 `open` 中最早升级的条目（面板 `escalation_detail.open_p95_seconds`）。
2. production 直查（只读）：

   ```sql
   SELECT status, COUNT(*) FROM escalations GROUP BY status;
   SELECT id, wf_id, service, escalated_at, assignee FROM escalations
    WHERE status <> 'closed' ORDER BY escalated_at LIMIT 20;
   ```

3. 分类处置：证据不足补采日志；补丁不可应用转研发；发布窗口冲突改约时间。

**恢复验证**：`escalation_detail.sla_breach=0`、`open_sample` 下降；每条关闭都有 `close_reason`。

**不要**：用脚本批量置 `closed`；用「已指派」冒充「已处置」。

## sandbox-failure-streak 沙箱连续失败

**判定**：最近 5 条终态流程按 `close_time` 倒序，连续 ≥3 条命中 `tests:failed`（沙箱测试拦截回炉）。

**立即动作**

1. 定位最近失败流程的 `wf_id`，读 `workflow_runs.result.test_report`（或流程详情「沙箱」区）：

   ```sql
   SELECT workflow_id, close_time, result->>'$.test_report'
     FROM workflow_runs WHERE close_time IS NOT NULL
    ORDER BY close_time DESC LIMIT 10;   -- MySQL 8 JSON 语法；SQLite 用 json_extract
   ```

2. 区分失败类型：环境问题（依赖缺失、网络超时、磁盘满）→ 修环境；补丁问题（断言失败、语法错误）
   → 回炉重生成。
3. 核对 `verification_mode`：只有 `UNIT_TEST` 是真实测试；`STATIC_ONLY` / `CONTRACT_TEST` 不算通过率，
   不要用它们「冲」指标。

**恢复验证**：`sandbox.real_pass_rate` 回升，`sandbox-failure-streak` 消失；失败原因为环境时在待办记录。

**不要**：跳过沙箱或把 `STATIC_ONLY` 计入真实通过率；降低测试阈值换绿灯。

## model-quality-streak 模型连续低置信

**判定**：最近 5 条终态中连续 ≥3 条命中 `gate1:blocked:confidence`（置信度 < 0.8）→ `warn`。

**立即动作**

1. 先看证据质量：`evidence.by_status` 中 `LOKI_UNAVAILABLE` / `NO_LOGS` / `STALE_LOGS` /
   `UNMAPPED_SERVICE` 占比高 → 属**观测面问题**，转 `log-stall` 处理，不得计入模型低置信。
2. 证据充分（`OK`）仍连续低置信：核对检索命中的代码片段、`root_cause` 与告警上下文是否匹配；
   检查模型路由与版本（`GET /api/system` / 配置项）。
3. 抽取 2–3 条典型案例，记录到待办 `note`，供模型评测集回归。

**恢复验证**：连续低置信计数 <3；`evidence.ok_rate` 稳定；新增案例进入评测集。

**不要**：下调 `confidence_threshold`（固定 0.8）绕过闸门；把观测异常写成「模型不行」。

## rollback 发布回滚

**判定**：窗口内出现 `canary:degraded`（金丝雀劣化自动回滚）；`rollback.rate = degraded / 进入金丝雀`
（`canary:healthy` 或 `canary:degraded`）→ `warn`。

**立即动作**

1. 按流程 `wf_id` 查 `gate_events` 中的 `canary:*` 序列，确认回滚是否完成（`kubectl argo rollouts get rollout <name>` 或发布详情页）。
2. 核对回滚后的副本与镜像摘要（旧版本全量、`Healthy`），确认线上无残留灰度的错误版本。
3. 归因：指标劣化 / 错误率 / 延迟阈值设置是否合理；把结论写入待办结构化关闭。

**恢复验证**：Rollout `Healthy`；新窗口 `rollback.sample=0`；`by_service` 对应服务成功率恢复。

**不要**：在回滚未完成时重新放量；删除回滚记录掩盖劣化。

## high-confidence-false-pass 高置信误放行

**判定**：曾通过组合判据 `gate1:composite:pass` 且置信度 ≥0.8，最终仍 `ESCALATED` → `danger`。
这是最贵的错误：机器「自信地」判断错误，需要人工兜底。

**立即动作**

1. 逐条复盘：根因是否命中（`resolution.verdict`）、补丁是否可应用、审批为何驳回。
2. 在结构化关闭中把 `verdict` 记为 `INCORRECT`（计入 `sandbox.override_rate` 人工改判率），
   并通过知识审核提交知识候选（`knowledge.draft` → `APPROVED`）。
3. 若同类根因重复出现，提交评测样本，纳入 `holdout` 沙箱回归。

**恢复验证**：`sandbox.high_confidence_false_pass=0`；`override_rate` 趋势下降；知识候选已审核。

**不要**：把误放行记成「人工干预」了事；修改历史 `gate_events` 掩盖证据。

## 附：告警阈值一览

| code | level 触发 | 阈值来源 |
| --- | --- | --- |
| `log-stall` | 不健康服务 ≥3 → danger；1–2 → warn；无心跳 → unknown | `log_health.snapshot()` |
| `escalation-backlog` | SLA 超时 >0 → danger；存储不可读 → unknown | `AIOPS_ESCALATION_SLA_MINUTES`（默认 30 分钟） |
| `sandbox-failure-streak` | 连续 ≥3 → danger | `tests:failed` 事件链 |
| `model-quality-streak` | 连续 ≥3 → warn | `gate1:blocked:confidence` 事件链 |
| `rollback` | 窗口内 >0 → warn | `canary:degraded` 事件链 |
| `high-confidence-false-pass` | >0 → danger | `gate1:composite:pass` + 置信度 ≥0.8 + 最终 ESCALATED |

口径实现见 `bff/ops_metrics.py`（`_duty_alerts` / `_consecutive` / `_sandbox`），
数据面见 `bff/ops_warehouse.py`；两处修改必须同步本文档的阈值描述。
