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
- **真实沙箱通过率**：仅 `UNIT_TEST/CONTRACT_TEST` 结果；`STATIC_ONLY` 单列。
- **高置信误放行**：达到自动放行分支但经人工判定根因或修复错误的流程数，不能用普通转人工率替代。
- **空样本**：显示“暂无数据”，比率不得显示为误导性的 0% 或 100%。

## 3. 数据库迁移约定

- 入口：`AIOPS_MODE=production AIOPS_DATABASE_URL=<...> .venv/bin/alembic upgrade head`。
- `20261101_0001` 是当前结构基线，覆盖空库升级和历史 `ensure_schema` 缺列补齐。
- 已存在且结构已核对的未版本化生产库，须先备份并在预生产演练后执行 `alembic stamp 20261101_0001`；禁止未经核对直接 stamp 或 downgrade。
- 后续 schema 变更必须新增显式迁移；应用内 `ensure_schema` 暂时保留用于兼容启动，待全环境版本化后再收敛。
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

后续里程碑在此文件追加实测结果；未执行的真实环境项目不得标记完成。
