# AIOps 闭环生产化实施与验收

## 1. 基线快照

基线来自实施前本机 production MySQL 与已入库评测报告，仅用于同口径对照，不代表生产服务质量：

| 指标 | 基线 |
|---|---:|
| 工作流总数 | 89 |
| ESCALATED | 68（76.4%） |
| DONE | 17（19.1%） |
| 转人工记录 | 42 |
| open / closed | 41 / 1 |
| 根因评测 | 32 条；正确率 93.75%；低证据阻断率 100% |
| 修复评测 | 33 条；27B 沙箱通过率 75.76%；8B 为 24.24% |
| Loki | 检查时无有效 service/app 标签及近一小时日志量 |

历史待办只允许分类、重试或人工结构化关闭，不以批量清零作为验收成绩。

## 2. 指标字典

- **流程类型**：告警修复与需求修复分开统计；需求流跳过日志根因诊断，不得混入告警自动化率。
- **统计窗口**：终态流程以 `close_time` 落入窗口为准；运行中流程单列，不进入 MTTR。
- **自动完成率**：`DONE / 同类型全部终态流程`。`DONE` 是流程终态，不自动等同业务根因已验证消除。
- **MTTR**：`DONE` 流程从告警触发到发布完成的时长，报告样本数、均值与 P95。
- **人工干预率**：窗口终态流程中，审批、发布指令、排队补丁或转人工处置审计触及的流程占比。
- **证据充分率**：证据质量为 `OK` 的告警流程占比；观测异常不得计入模型低置信。
- **MTTA**：人工介入的终态流程，`start_time` → 首个介入动作（审批/发布指令/排队/转人工处置）的均值与 P95，报告样本数。
- **回滚率**：命中 `canary:degraded` 的流程 / 进入金丝雀（`canary:healthy` 或 `canary:degraded`）的流程。
- **采集新鲜度与覆盖率**：按服务心跳（`log_ship_health.json`）计算 P95 心跳年龄与不健康数；覆盖率为有心跳的受管服务 / 启用且配置服务标签的条目。无心跳时一律「暂无数据」，不显示「正常」。
- **补丁结构可应用率**：补丁具备 diff 与文件清单的占比（结构判定）；真实 `git apply` 结果由发布阶段判定，不并入该口径。
- **人工改判率**：待办结构化关闭中 `verdict=INCORRECT` 的占比（样本为已给出评价的关闭记录）。
- **真实沙箱通过率**：仅 `UNIT_TEST` 结果；`CONTRACT_TEST` / `STATIC_ONLY` / `UNKNOWN` 单列。
- **待办口径**：开放时长均值/P95、处置时长均值、SLA 超时数、关闭原因分布、知识候选（待审核/已入库/已拒绝）；「采用情况」在索引重建回填前为 `null`。
- **模型成本**：未接入真实计量前 `model_cost_usd=null`，不得以 0 展示。
- **静态验收单列**：无套件/未声明契约的执行计入 `aiops_sandbox_static_runs_total{verification}`，与 `aiops_sandbox_runs_total` 分开统计；历史归档的空模式一律归一为 `UNKNOWN` 标签，不得进入真实沙箱通过率。
- **高置信误放行**：达到自动放行分支但经人工判定根因或修复错误的流程数，不能用普通转人工率替代。
- **空样本**：显示“暂无数据”，比率不得显示为误导性的 0% 或 100%；接口对分母为 0 的比率返回 `null`。
- **容量与背压**：`aiops_capacity_in_use` / `queue_depth` 为瞬时值，`wait_seconds` / `run_seconds` 为固定 512 窗口直方图；`rejected_total{reason}` 只统计因 `queue_timeout` 或取消产生的拒绝，执行失败不得计入拒绝。配额为进程内信号量（`distributed_limits=false`），多副本时不得直接相加当作全局并发。

## 3. 数据库迁移约定

- 入口：`AIOPS_MODE=production AIOPS_DATABASE_URL=<...> .venv/bin/alembic upgrade head`。
- `20261101_0001` 是当前结构基线，覆盖空库升级和历史 `ensure_schema` 缺列补齐。
- 已存在且结构已核对的未版本化生产库，须先备份并在预生产演练后执行 `alembic stamp 20261101_0001`；禁止未经核对直接 stamp 或 downgrade。
- 后续 schema 变更必须新增显式迁移；应用内 `ensure_schema` 暂时保留用于兼容启动，待全环境版本化后再收敛。
- 迁移链：`0001` 基线 → `0002` 证据质量/待办分类 → `0003` 结构化关闭/处置计划 →
  `0004` 运营维度（`workflow_runs.kind` 默认 `alert`、`workflow_runs.risk` 默认空串、
  组合索引 `idx_runs_close_kind(close_time, kind)`）。
- `0004` 对空库与历史库均幂等：缺列才加、索引不存在才建；**降级必须先 `drop_index` 再
  `batch_alter_table` 删列**，否则 SQLite 重表时索引仍引用已删列，报 `no such column: kind`
  （`tests/test_migrations.py::test_runs_kind_risk_added_and_downgraded` 已锁定该顺序）。
- 存量行由 `server_default` 兜底（`kind=alert`、`risk=''`），不做无条件数据回填；运营指标
  对缺失维度按「未标注」归桶，不以默认值冒充真实分类。
- 基线 downgrade 只用于隔离空库演练，生产禁止直接回退全部业务表。

## 4. 验收记录

### M0 基线与迁移

- [x] 基线和指标口径入库。
- [x] Alembic 环境、基线迁移和 SQLite 空库/缺列/回退测试。
- [ ] Linux 预生产 MySQL 8 空库升级与当前版本升级实测。
- [ ] 生产备份后 stamp 预演（须单独变更窗口，不在本机自动执行）。

### M1 日志供给与证据质量预检

- [x] 采集推送语义：Loki 确认行数与批次一致才算成功；失败有界重试（3 次、指数退避上限 5s），耗尽后保留游标，下一轮重推（至少一次）。部分确认按失败处理，不推进游标。
- [x] 采集健康心跳：`<data_root>/log_ship_health.json`（`AIOPS_LOG_HEALTH_PATH` 可覆盖），记录最近成功/失败、连续失败数、警告与推送行数；`aiops-watchdog.py status` 退出码纳入采集健康，`/metrics` 输出 `aiops_log_ship_*` 指标。无心跳时只告警「不能判定」，不把首次部署判红。
- [x] 服务标签审计：采集清单与 Loki 标签比对，报告拼写疑似、重复服务、缺采集配置、缺日志路径；Loki 不可达只探测一次，不逐服务放大失败。
- [x] 证据质量六状态：`OK / LOKI_UNAVAILABLE / NO_LOGS / STALE_LOGS / UNMAPPED_SERVICE / INSUFFICIENT_SIGNAL`，旧归档显示 `UNKNOWN`；判定顺序与阈值见 `aiops_agent/evidence_quality.py`。
- [x] 工作流预检：`workflow.patched("evidence-quality-g1")` 守护；非 `OK` 在调用根因模型前转人工，事件为 `evidence:blocked:<status>`，不伪装成闸门 1，避免污染模型低置信指标；`INSUFFICIENT_SIGNAL` 默认转人工，可用 `evidence_insufficient_escalate` 调整。在途旧流程保持单参数取证，历史重放不产生额外命令。
- [x] 持久化：`log_evidence.quality`（JSON）与 `escalations.category` 由迁移 `20261101_0002` 幂等补齐，空库/历史库/回退测试通过；归档时写入质量摘要。
- [x] 处置分类：`model / approval / sandbox / release / observability / evidence / requirement / other`；历史待办用 `backfill_categories` 回填，默认 dry-run，只写分类，不改状态与 `closed_at`。
- [x] 前端：转人工列表分类徽标与分类筛选、证据质量提示；日志证据面板展示质量状态、最新日志时间与可分析信号行数。
- [x] 单测覆盖：六状态边界、Loki 查询元数据、心跳新鲜度/失败计数/损坏容错、推送重试与游标、标签审计、分类回填、质量归档 roundtrip、工作流阻断/放行/旧流程兼容、`aiops_log_ship_*` 指标渲染、watchdog status 采集健康。
- [ ] 真实 Loki 供给恢复验证（service/app 标签、近一小时日志量）须在接入环境执行后回填。
- [ ] 采集进程连续运行 24h 的心跳无中断记录（需部署环境观察）。

### M2 结构化人工处置与审核知识回流

- [x] 关闭记录结构化：`escalations.resolution`（JSON）在既有待办上扩展，不另建平行系统；字段含关闭原因、实际根因、实际处置、验证结果、对智能体判断的评价、知识候选与敏感确认、关闭人/时间，原 `note` 保留。
- [x] 校验规则：`RESOLVED` 必须填写实际根因 / 实际处置 / 验证结果；其他原因至少给出备注 / 处置 / 根因之一；勾选知识候选必须先确认已检查敏感信息；旧客户端仅传 `note` 时行为不变（`resolution=null`）。
- [x] 处置计划：`escalations.plan`（VARCHAR(1024)）支持未关闭待办登记下一步动作，允许保持 open，不批量关闭；已关闭待办拒绝修改（含单测）。
- [x] 迁移 `20261101_0003` 幂等补齐 `resolution` / `plan`，空库、历史库与回退测试通过（`tests/test_migrations.py`）。
- [x] 知识审核状态机：`DRAFT / APPROVED / REJECTED`；仅管理员可审核（middleware 对 `/api/escalations/*/knowledge/review` 前置 admin 规则）；未解决记录不得批准；撤销后状态为 `REJECTED` 并可重新审核。
- [x] 审计：`escalation:close` 记录关闭原因 / 评价 / 知识状态；新增 `escalation:plan`、`knowledge:approve|reject|revoke`（含导出结果）。
- [x] 清洗与导出：`aiops_agent/knowledge.py` 清洗凭据、令牌、内部地址、邮箱与手机号；清洗后仍命中敏感模式的记录整条跳过；原子写 `data/knowledge_records.json`（`AIOPS_KNOWLEDGE_PATH` 可覆盖），保留来源待办与流程 ID。
- [x] RAG 合并：`code_rag.build_index` 合并 `historical_tickets.json` 与已审核知识（`kind=ticket`），返回并写入 `knowledge` 计数；`index_codebase.py` 新增 `--knowledge`；未审核、已拒绝、撤销、未解决与旧格式记录不入索引。
- [x] 显式索引：关闭与审核请求只同步导出文件，不触发嵌入或训练；重建由 `index_codebase.py` 手动执行（部署手册 3.4）。
- [x] 前端：结构化关闭表单（按原因动态校验、候选与敏感确认）、处置计划弹窗、已关闭行「详情 / 审核」入口、三段式对照（智能体判断 vs 实际根因 vs 验证结果）、知识状态徽标与待审核计数。
- [x] 单测覆盖：关闭校验边界、状态机、敏感确认、计划、旧格式兼容、demo/production（SQLite）roundtrip、路由全链路 `open→close→approve→export→revoke`、清洗幂等、导出原子写、索引合并与未批准不可检索、角色规则。
- [ ] 预生产/生产环境完成一次 `index_codebase.py` 重建，并验证「已批准可检索、撤销后重建不可检索」的真实索引闭环。
- [ ] 存量待办（41 条口径）的处理人与处置计划需值班在列表中逐条登记；系统已提供字段与入口，不做批量伪造责任人，也不要求人为清零。

### M3 评测体系 V2、组合放行判据与模型路由

- [x] 根因集扩容：`data/triage_eval_samples.json` 105 条（`dataset_version=a4ce562a23aa`），dev 79 / holdout 26；期望路由 code_fix 39 / runbook 48 / human 18；安全回归样本 48 条、低证据样本 18 条，覆盖 S08（连接池）、S11（网关 502）同类场景与多根因、串服务、日志缺失/截断。
- [x] 修复评测口径：`data/fix_eval_samples.json` 33 条（`05987e0a79ab`，holdout 17 / dev 16）+ `data/fix_eval_abstain_samples.json` 28 条弃权样本（`b4e8aa46e949`，runbook 22 / human 6），合计 61 条满足计划 ≥60。
- [x] 合成口径声明：新增样本为脱敏场景化变体，`source=scenario_catalog`；真实历史处置须经管理员审核脱敏后以 `source=historical` 追加，报告与数据集均保留该字段，避免把合成样本计入真实处置成绩。
- [x] 组合放行判据：`aiops_agent/gate_decision.py` 为纯函数，次序为 Schema 有效 → 证据质量 OK → 置信度 ≥0.8（未放宽）→ 根因类型（基础设施/配置转 Runbook，未知转人工）→ 嫌疑文件可定位 → 模型路由达标；每条结论输出 `gate1:composite:pass|blocked:<code>` 事件，控制台与待办原因复用同一份中文标签。
- [x] 工作流接入：`assess_fix_scope` 活动解析嫌疑文件并产出路由结论；闸门分支由 `workflow.patched("composite-gate-v1")` 与策略开关双重守护，策略未开启或在途旧流程行为不变。
- [x] 安全回归实证：以 48 条安全样本的规范化信号构造「置信度 0.99」夹具，组合判据全部拦截为 runbook；39 条代码根因夹具全部放行，未出现过度拦截。本轮据此补齐 3 类信号缺口（消费组再平衡/端口被占用/目录权限），并移除会误伤 JSON 解析类代码根因的泛化词（“上游”“解析失败”）。
- [x] 模型路由：`aiops_agent/model_routing.py` 默认 Ollama `qwen3.8:27b-mlx`；8B 等小模型标记 `qualified=False`，不得作为无人值守默认；多文件根因且 Qoder 可用时切 Qoder；release 策略 `fix_routing.require_quality_bar=true`，缺少质量证据时一律转人工/排队，不静默降级。
- [x] 评测报告 V2：`eval_triage.py` / `eval_fix.py` 输出 `dataset_version / code_commit / prompt_hash / model / provider / rag_index_hash`，指标含高置信误放行率、分场景准确率、置信度分桶、Brier、diff 可应用率、首轮/最终沙箱通过率、目标测试通过率、新增回归数、弃权误放行数；`--check` 在硬阈值或基线退化时退出 1，`--write-baseline` 仅在无违规时晋升基线，`--write-quality-bar` 仅在 holdout 双门槛（Schema ≥90%、沙箱 ≥85%）达标时写 `data/model_quality_bar.json`。
- [x] 确定性 PR 门禁：`scripts/eval_gate.py`（`aiops_agent/eval_datasets.py`）校验数据集门槛与版本哈希、安全回归夹具拦截、代码根因放行，不调用模型；已接入 `ci.yml` 的 `eval-gate` job 并作为隔离集成的前置。
- [x] 运行节奏：`.github/workflows/eval-daily.yml` 每日 UTC 18:00 与模型/提示词/数据集变更时触发；固定在 `self-hosted, aiops-eval` 评测机执行，Ollama 或模型缺失时显式失败并给出修复命令，不静默跳过。
- [x] 单测覆盖：`test_gate_decision`（18）、`test_model_routing`（17）、`test_evalkit`（24）、`test_eval_datasets`（21）、`test_workflow_composite_gate`（9）；含降级、证据非 OK、非代码根因、缺文件、未达质量门槛、空分母指标、基线不兼容与破损数据集。
- [x] 冒烟实测（本机 Ollama `qwen3:8b`）：S08 单样本模型返回 `TimeoutError`（关键词未命中），但摘要含「连接池耗尽」触发基础设施拦截，路由 runbook，`gate1:composite:blocked:non_code_root_cause:infrastructure`；弃权集 AB01/AB23 组合判据均正确拦截且误放行 0。
- [ ] 27B holdout 完整评测（17 条修复 + 14 条弃权 + 26 条根因）需在评测机执行；未产出报告前 `data/model_quality_bar.json` 保持缺省，release 策略按设计一律转人工。
- [ ] `data/eval_baseline.json` 待首个 holdout 全绿报告 `--write-baseline` 晋升后入库；当前报告结论为 `baseline_missing`（硬阈值仍生效）。
- [ ] 自托管 `aiops-eval` runner 注册与首次每日任务实跑记录需在部署环境回填。

### M4 沙箱对齐、K8s 执行器与预生产清单

- [x] Docker 沙箱加固：`--user <宿主非 root uid:gid>`（`AIOPS_SANDBOX_UID` 可覆盖为 `65534:65534`；Docker Desktop 的 bind mount 按宿主属主映射，固定 65534 即使目录 777 也无法写工作区）、`--cap-drop ALL`、`no-new-privileges`、只读根 + `--tmpfs /tmp`、`--network none`、可选 seccomp；输出按流截断 256KiB、工作区上限 256MiB；超时 `killpg` 后按 `_reclaim` 二次核对，容器未回收即判失败而非静默放过（`tests/test_sandbox_hardening.py`）。
- [x] 镜像固定：生产环境强制 `@sha256:` 摘要，缺摘要时按执行计划给出 `AIOPS_SANDBOX_IMAGE`/python 或 `AIOPS_SANDBOX_NODE_IMAGE`/node 的正确变量与形式；摘要缺失不计入「已固定」。
- [x] 执行计划：Python（pytest/unittest）与 Node（`package.json` 脚本、无 `test` 脚本时只跑 build/tsconfig/contract/static）分别生成步骤；缺依赖、缺脚本时不伪造通过。
- [x] Node 真实构建/测试链路（2026-11-02）：工作区排除宿主 `node_modules`（macOS 原生模块在 Linux 容器无法加载），由 `scripts/prepare_node_sandbox_deps.sh` 用固定镜像预构建 Linux 依赖树并按 `package-lock.json` 摘要校验；运行时复制进工作区（vite 需要写 `node_modules/.vite-temp`，只读挂载会 EACCES）；Node 计划独立资源 2400s / 4g / 2 CPU / 512 pids / 2048MiB 配额；vitest 汇总行解析用例数（不再把单元/回归测试显示为「未执行」）。实测：typecheck 15s、build 38s、`vitest run` 2424 用例全通过（24m42s）；端到端 `run_patch_tests` 用时 1497s、`passed=true`、报告展示「2424/2424 passed / 全量 2424 用例：通过」。vitest 输出到管道仍带 ANSI 颜色码（`\x1b[2m      Tests`），解析前必须剥离，否则行首正则全部落空；总数按千分位（2,424）并兼容 skipped 段。
- [x] K8s Job 执行器（`K8sJobSandboxRunner`）：工作区打包为 Secret（>512KiB 显式失败）→ 幂等 Job 名 `aiops-sandbox-<patch>-<spec_hash>`（409 同 spec 复用、异 spec 重建）→ `runtimeClassName: gvisor`、`runAsNonRoot 65534`、`readOnlyRootFilesystem`、drop ALL、`seccompProfile: RuntimeDefault`、`automountServiceAccountToken: false` → 轮询 `activeDeadlineSeconds` → 读 Pod 日志 → 删除 Job/Secret 并核对 Pod 消失。容器资源 limits 跟随执行计划（Node 4g/2 CPU），Docker 的 `256m`/`4g` 由 `k8s_memory()` 转为 K8s 的 `256Mi`/`4Gi`（`M`/`G` 是十进制，直接透传会缩小上限）。
- [x] 六类路径单测：成功、测试失败、镜像拉取失败、超时、日志读取失败、清理超时均有确定结论（`tests/test_k8s_sandbox.py`），另覆盖 Secret 超限、production 摘要强制、集群不可达与 API 404 重试。
- [x] 验证口径修正：`verification_mode` 为空（历史归档）不再默认按 `UNIT_TEST` 渲染，统一归一为 `UNKNOWN` 并计入静态指标；仅 `UNIT_TEST` 计入 `aiops_sandbox_runs_total`。修复前空模式会让历史记录被计入真实沙箱通过率。
- [x] 工作流静态验收闸门 `sandbox-verification-gate-v1`：高风险（受保护目录或风险等级含「高/高：」）变更必须是 `UNIT_TEST` 且确实跑过用例，否则 `ESCALATED`；`STATIC_ONLY` / `CONTRACT_TEST` / `UNKNOWN` 均不放行；开关关闭或 `patched()` 未命中时保持旧行为，重放不改变历史结论（`tests/test_workflow_static_gate.py`、`tests/test_release_replay.py`）。
- [x] ArgoCD/Rollouts 状态机（`aiops_agent/argo_status.py`）：`RECORDED/SUBMITTED/SYNCING/SYNCED/HEALTHY/DEGRADED/ROLLED_BACK/FAILED` 八态，`derive_state` 覆盖 Sync/Health/OperationPhase 组合；`rollout_progress` 从 `spec.strategy.canary.steps` 读总步数；`rollback` 固定 `revision=0`（上一版本）；HTTP 错误统一包装为 `ArgoStatusError`。
- [x] 集群发布：`release_executor()` 显式 `AIOPS_RELEASE_EXECUTOR` 优先，否则按 `AIOPS_ARGOCD_URL` 决定；`_run_cluster_canary` 只认 `HEALTHY`，`DEGRADED` 转 finalize 回滚，不可达/超时抛 `ReleaseSafetyError`；错误率与 P99 由 AnalysisRun 判定，本层返回 `-1` 并标注 `slo_source=argocd-rollout`，不伪造 0。
- [x] 终态落盘修复：集群金丝雀原先只靠 `on_state` 回调写 journal，客户端不回调时记录停在 `SUBMITTED`；现于 `wait` 返回后显式 `record.update(release_status=state)`（`tests/test_release_cluster.py` 覆盖无回调场景）。
- [x] 预生产清单（`deploy/staging/`）：独立命名空间与 restricted PSA、`RuntimeClass gvisor`、最小 RBAC（沙箱 Job/Pod/日志/Secret + Rollouts 只读，无原子级通配、无 `pods/exec`）、default-deny NetworkPolicy（沙箱连 DNS 一并拒绝；staging 仅放行 DNS/同命名空间/ingress-nginx/监控/443/Loki/OTLP/apiserver）、沙箱 ResourceQuota 20 Job / 40 Pod / 4 CPU / 8 GiB、示例 Secret（全占位）、`policy.staging.yaml`（`shadow_mode: true`、阈值 0.8 未放宽）、Rollout 5%→50%→100% 与两个 AnalysisTemplate 指标。
- [x] 清单自检：`tests/test_staging_manifests.py`（23 例）校验 YAML 可解析、kustomize 引用存在、默认拒绝与端口白名单、无集群级绑定、配额与策略阈值、占位值只出现在允许文件、README 明确标注「真实集群未验证」。
- [x] 全量回归：`Ran 1693 tests … OK (skipped=6)`（M3 结束 1586，M4 新增 107 例：沙箱加固 22、K8s 执行器 16、ArgoCD 状态 16、集群发布 20、静态闸门 10、清单自检 23）。
- [ ] 在预生产主机完成一次真实健康发布（`SUBMITTED→SYNCING→SYNCED→HEALTHY`）与一次真实劣化回滚（`DEGRADED→ROLLED_BACK`），并把状态序列、AnalysisRun 结果、`rollout history` 回填本节；未执行前不得标记完成。
- [ ] 固定 `AIOPS_SANDBOX_IMAGE` / `AIOPS_SANDBOX_LOADER_IMAGE` 的真实 `@sha256:` 摘要，并把摘要写入预生产 Secret/ConfigMap（当前代码强制校验，环境侧摘要待填）。
- [ ] 节点侧 gVisor（runsc + `io.containerd.runsc.v1`）、ArgoCD/Rollouts 版本与 `aiops-worker` ServiceAccount 命名空间需在实机核对，步骤见 `deploy/staging/README.md` 第 2、6 节。

### M5 运营指标与值班看板

- [x] 维度落库：`workflow_runs.kind`（`alert` / `requirement`）与 `risk` 列由迁移 `20261101_0004` 幂等补齐；镜像写入 `bff/runs_store.py` 的变化签名纳入 `kind`、`gate_events` 数量、`test_report` 是否存在与 `patch.risk`，避免同阶段内证据增长被漏写。
- [x] SQL 数据面：`bff/ops_warehouse.py` 一次窗口取 `workflow_runs`（含 `result` 中的 `gate_events` / `test_report` / `patch`）、`audit_events`、`log_evidence`、`monitored_apps`，行归一后交给纯函数聚合；单分量读取失败只降级该分量并记入 `degraded`。
- [x] 双轨与降级：production 优先走 SQL 镜像（`source=warehouse`，附 `mirror_updated_at`）；镜像为空且从未同步时回退 Temporal 读侧并置 `degraded=warehouse-empty`，不以 0 条冒充「窗口内无流程」；demo 保持 Temporal + `data/evidence` 文件扫描。
- [x] 分维度口径：按流程类型（告警/需求各自独立分母）、按服务、按风险等级分别给出自动完成率、人工干预率与 MTTR；`risk` 优先取镜像列，历史行回退 `patch.risk`；服务维度按流程数取前 10，其余不隐藏为 0。
- [x] 新增指标（分母为 0 一律 `null`）：MTTA（`start_time` → 首个审批/发布指令/排队/转人工处置，均值 + P95 + 样本数）、回滚率（命中 `canary:degraded` / 进入金丝雀）、日志采集新鲜度与覆盖率（心跳 P95 年龄、不健康服务、受管服务覆盖率）、补丁结构可应用率、人工改判率（`verdict=INCORRECT` / 已评价关闭）、高置信误放行数。
- [x] 沙箱口径：真实沙箱通过率只统计 `UNIT_TEST`；`CONTRACT_TEST` / `STATIC_ONLY` / `UNKNOWN` 单列计数，不并入分子分母；知识「采用情况」与模型成本在计量接入前保持 `null`，不以 0 展示。
- [x] 值班告警与 Runbook：`log-stall` / `escalation-backlog` / `sandbox-failure-streak` / `model-quality-streak` / `rollback` / `high-confidence-false-pass` 六类告警，`level=unknown` 表示数据不足而非正常；新增 `GET /api/metrics/ops/runbook?code=`，由 `bff/runbooks.py` 从 `docs/AIOps 值班 Runbook.md` 的 `## <code>` 小节单源读取（正则校验 code，拒绝路径穿越）。
- [x] 前端：系统总览的运营度量改成值班看板——值班告警置顶（等级徽标 / 观测值 / 阈值 / 明细 / Runbook 抽屉）、6 张 KPI 卡、分来源与分风险、证据与采集、沙箱与补丁质量、待办与知识、分服务前 10、拦截分档；比率为 `null` 时显示「暂无数据」，标题标注数据源与镜像同步时间。
- [x] 文档：新增 `docs/AIOps 值班 Runbook.md`（通用读法 + 六节处置 + 阈值一览）；测试锁定「聚合产出的每个 `duty_alerts[].code` 必须有对应 Runbook 小节」，防止告警改码后文档失联。
- [x] 单测覆盖：`test_ops_metrics_m5`（23）、`test_ops_warehouse`（11，含 SQL 数据面与手算一致性）、`test_runbooks`（8）、`test_migrations` 新增 kind/risk 升级与降级 1 例；全量 `Ran 1736 tests … OK (skipped=6)`（M4 结束 1693，本里程碑新增 43）。
- [x] 生产 MySQL 8 实机迁移（2026-11-02）：`alembic current` 为空（从未版本化）→ 备份 `backups/aiops-before-migrations-20261102-091516.sql`（185 KB，`Dump completed`，13 条 INSERT）→ `alembic upgrade head` 一次补齐 `0001`–`0004`。核对结果：`workflow_runs` 90 行全部 `kind=alert`；`escalations` 42 行、`log_evidence` 14 行保留；`idx_runs_close_kind(close_time, kind)` 存在，且窗口内按 `close_time` + `kind` 的聚合/倒序查询均命中该索引（`range` + `Using index` / `Backward index scan`）；readiness `schema_current`、`/readyz` 200。补齐的列：`log_evidence.quality`、`escalations.category/plan/resolution`、`workflow_runs.kind/risk`。
- [x] 真实窗口 SQL 抽样比对（2026-11-02，`scripts/verify_ops_metrics_sql.py`，只读）：7 天窗口 42 项全一致——终态 13、`gate1:blocked` 12/13（拦截率 0.9231）、证据 13 条（均 `UNKNOWN`，迁移前历史归档）、按服务 Top10 与按类型分母一致；该窗口无 `DONE`，MTTR/回滚比率为 `null`。另跑 30 天窗口 42 项全一致——终态 90、`DONE` 17（自动完成率 0.1889）、MTTR 565.0s / P95 1534.9s、MTTA 23.5s / P95 329.0s（14 例）、人工干预 14、拦截 29（0.3222）、回滚 0/5；`source=warehouse`、`degraded=[]`、镜像时间 `2026-11-02T01:42:56Z`。分母为 0 的维度保持 `null`，未凑样本。
- [ ] 模型成本计量与知识采用情况回填（`cost.model_cost_usd`、`escalations.knowledge.adopted`）待计量数据源与索引重建记录接入后补齐。

### M6 容量治理与生产灰度

- [x] 配额策略：`ReleaseGatePolicy.capacity`（`CapacityConfig`）显式声明 `worker_activities=10`、`worker_local_activities=4`、`local_llm=1`、`qoder=1`、`sandbox=2`、`release=1`、`queue_timeout=0s`、`distributed_limits=false`；`release` 校验器锁定为 1（发布全局串行，不能靠 `max_active_deployments` 兜底）。旧策略文件缺 `capacity` 段时回落默认值，不破坏既有部署。
- [x] 资源信号量：`aiops_agent/capacity.py` 提供 `ResourceLimiter`（`asyncio.Semaphore` + 排队/执行时长采样 + 拒绝计数）、`limited(resource)` 装饰器与 `configure_from_policy`。`queue_timeout=0` 表示无限等待（与「排队不设上限」的锁定策略一致），超时抛 `CapacityError` 并计入 `rejected{reason=queue_timeout}`；采样窗口固定 512，长期运行不会无界增长。为避开 Temporal 沙箱的模块校验路径，`metrics` 采用延迟导入。
- [x] 活动接入：`analyze_root_cause` → `local_llm`；`generate_patch` 保持原签名（`@activity.defn` 外层不变），内部按 `fix_agent.resolve_provider()` 选择 `qoder` 或 `local_llm`；`run_tests_in_sandbox` → `sandbox`；`deploy_canary` / `finalize_release` → `release`。装饰器使用 `functools.wraps`，`__name__` 与函数签名对 Temporal 注册保持不变。
- [x] Worker 并发：`worker.py` 显式传入 `max_concurrent_activities` / `max_concurrent_local_activities`，并在启动时调用 `configure_from_policy` 打印配额日志；`distributed_limits=false` 明确表示当前为**单进程配额**，多 Worker 场景必须先落地集中限流，不得把进程内信号量当全局配额（部署手册第 14.1 节列明该边界）。
- [x] 容量指标：新增 `aiops_capacity_in_use{resource,mode}`、`aiops_capacity_queue_depth`、`aiops_capacity_wait_seconds`、`aiops_capacity_run_seconds`、`aiops_capacity_rejected_total{resource,reason,mode}`；`metrics.py` noop 分支同步实现，未装 SDK 时不报错也不伪造数据。
- [x] 对外暴露：启动快照（`startup_snapshot` / `snapshot_for_workflow`）与 `GET /api/system` 的 `policy.capacity` 均包含 `capacity`；`_POLICY_LOCKS` 登记 `capacity.release=1`，管理面可读到实际生效的阻塞项；前端系统状态页新增「并发配额」区块。
- [x] 压测工具：`scripts/capacity_drill.py` 支持 `fake` 与 `temporal` 两模式，20/50/100 三档批次覆盖 `normal` / `duplicate` / `no_logs` / `model_timeout` 画像；幂等键与 `alert_webhook.workflow_id_for` 对齐为 `aiops-fix-{service}-{alert_id}`；报告给出 `quota_respected` / `no_leak` / `dedup_ok` 三项硬断言与失败分类；temporal 模式未完成启动核对时 `evaluate` 必须报问题，不得默认通过。
- [x] 灰度工具：`scripts/rollout_gate.py` 覆盖 `staging-shadow → production-shadow → single-service → limited-release → expand` 五阶段，检查项含样本量（≥50 条 / ≥7 天）、证据质量、高置信误放行、真实沙箱与静态验收分列、单服务自动完成率（≥50%）、模型质量门槛文件存在、回滚率/人工改判率不高于基线、三项人工演练显式确认。缺失字段记 `unknown`，退出码 0=allow / 1=block / 2=pending。
- [x] 单测覆盖：`test_capacity`（16）、`test_capacity_drill`（9）、`test_rollout_gate`（19）；全量 `Ran 1780 tests … OK (skipped=6)`（M5 结束 1736，本里程碑新增 44）。
- [ ] staging 真实 20/50/100 三档压测（含重复告警、无日志、模型超时、沙箱排队）与 `temporal` 模式启动核对：需核对无重复工作流、无未解释 OOM、队列可恢复、失败分类与报告一致。
- [ ] Worker 重启状态一致性：重启期间排队与执行中活动的最终归属、幂等键复用、指标计数连续性（当前仅单测覆盖进程内行为）。
- [ ] Kill Switch 生效时延实测（压测进行中触发，记录从发令到活动停止与队列清空的秒数）。
- [x] 故障演练（1/5）：MySQL 备份恢复（2026-11-02）——将迁移前 dump 导入临时 MySQL 8.0 实例，13 张表逐表行数一致（`workflow_runs` 90 / `escalations` 42 / `log_evidence` 14）；以 `AIOPS_DATABASE_URL` 指向临时库执行 `alembic upgrade head`，版本 `20261101_0004`、6 个新列与 `idx_runs_close_kind` 齐全、行数不变；演练后销毁临时实例，生产库未参与写入。该演练只证明「备份可恢复 + 迁移可重放」，不替代真实切换窗口演练。
- [ ] 故障演练：Temporal retention 到期、Loki 不可用、ArgoCD 不可用、沙箱资源耗尽四项，按部署手册第 14.5 节处置表回填实测结论。
- [ ] staging 影子 7 天且有效告警 ≥50 条后跑 `rollout_gate --stage staging-shadow`，再依次推进 `production-shadow`、`single-service`、`limited-release`、`expand`；每阶段需附 `--metrics` 快照与人工确认记录，未执行前不得标记完成。

## 5 交付门禁与遗留项

### 5.1 测试基线演进

| 里程碑 | 结束测试数 | 结果 | 本里程碑新增 |
|---|---:|---|---:|
| M3 评测 V2 与组合放行 | 1586 | OK (skipped=6) | 89 |
| M4 沙箱与预生产清单 | 1693 | OK (skipped=6) | 107 |
| M5 运营指标与值班看板 | 1736 | OK (skipped=6) | 43 |
| M6 容量治理与生产灰度 | 1780 | OK (skipped=6) | 44 |

前端每次改动后 `cd web && npm run build`（`tsc --noEmit` + vite）必须通过；产物 hash 变化需重新 `git add`。

### 5.2 每提交必跑

```bash
.venv/bin/python -m unittest discover -s tests -t .   # 必须带 -t .
cd web && npm run build
bash scripts/security-scan.sh                          # Bandit Medium+，HIGH=0
```

提交时逐文件/逐组暂存（禁止 `git add -A`），单次语义化提交后分别推送 `origin`（Gitee）与 `github`，并核对本地、`origin/main`、`github/main` 三方 SHA 一致。

### 5.3 真实环境遗留项

以下项目**只做了代码与单测层面的验证**，未在真实环境执行，统一列在此处跟踪，完成前不得在对应里程碑标记交付：

| 里程碑 | 遗留项 | 执行依据 |
|---|---|---|
| M4 | 真实健康发布与劣化回滚、镜像 `@sha256:` 摘要、节点侧 gVisor/ArgoCD 版本核对 | 第 4 节 M4 小节；`deploy/staging/README.md` |
| M5 | 模型成本计量与知识采用回填（`cost.model_cost_usd`、`escalations.knowledge.adopted`） | 第 4 节 M5 小节；部署手册第 13 章 |
| M6 | staging 20/50/100 压测与 temporal 启动核对、Worker 重启一致性、Kill Switch 时延、retention/Loki/ArgoCD/沙箱耗尽四项演练、五阶段灰度 | 第 4 节 M6 小节；部署手册第 14 章 |

### 5.4 当前结论

M0–M6 的代码、迁移、测试与工具链已在仓库内完成并可回归（1780 例全绿）；**生产可用性尚未验收**——上表遗留项全部通过、且 `rollout_gate` 对应阶段返回 `allow` 之前，系统只能按影子模式在 staging 运行。

后续里程碑在此文件追加实测结果；未执行的真实环境项目不得标记完成。
