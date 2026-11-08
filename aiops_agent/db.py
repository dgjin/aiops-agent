"""数据库层（production 模式）：SQLAlchemy 引擎 + 幂等建表 + 时间转换。

选型与设计说明：

- **生产 MySQL 8.0**（InnoDB / utf8mb4），驱动 PyMySQL；单测可用 SQLite 临时库
  覆盖同一套 DB 分支逻辑（URL 由 ``AIOPS_DATABASE_URL`` 注入，本层不写死方言）；
- **同步引擎**（非 async）：本系统全部 DB 访问点均为低频管理操作（审计写入 /
  令牌校验 / 清单 CRUD / 流程速查 upsert），同步接口让 demo 文件后端与生产 DB
  后端共用同一函数签名，调用点零分支；BFF 为内部运维控制台（低并发），
  阻塞代价可忽略；
- **连接池**：``pool_recycle=3600`` + ``pool_pre_ping=True``（防 MySQL
  ``wait_timeout`` 断连后使用死连接）；
- **建表**：:func:`ensure_schema`（``create_all(checkfirst=True)`` + 显式补缺失列）
  幂等（首次连接自动执行）；schema 稳定后再引入 Alembic 版本化迁移，
  避免双套建表机制漂移。老库升级必须走 ``ensure_schema``：``create_all`` 只建
  缺失表、不补列（如 ``monitored_apps.depends_on``）。

表一览：

=============== ============================================================
system_kv       系统键值（kill switch / 令牌注册表元数据）
console_tokens  令牌注册表（production 替代 ``data/console_tokens.json``）
console_users   控制台用户（密码登录；production 替代 ``data/console_users.json``）
console_sessions  登录会话（仅存 sha256 指纹；production 替代 ``data/console_sessions.json``）
monitored_apps  被监控应用清单（production 替代 ``data/monitored_apps.json``）
escalations     转人工处置待办（production 替代 ``data/escalations.json``；P1-3）
requirement_analyses  需求智能分析会话（production 替代 ``data/requirement_analyses.json``）
audit_events    控制台操作审计（production 替代 JSONL，支持跨实例聚合查询）
workflow_runs   流程执行速查（BFF 汇总时 upsert，供报表与历史检索）
alert_domains   告警故障域（C 批次）：当前聚合事件指针与窗口起点（跨实例唯一）
alert_events    告警聚合事件（C 批次）：代表告警/流程 + pending/started + 租约认领
alert_members   事件成员（C 批次）：(domain_key, alert_id) 唯一去重，跨实例幂等计数
log_evidence    日志证据归档（转人工核对）：流程 TRIAGING 采集的原始日志 + 模板聚类
workflow_commands  写命令 outbox（M2）：意图审计 + 投递状态 + 有界重试
workflow_events     Temporal History 投影（M4）：(workflow_id,run_id,event_id) 幂等事件
audit_chain_heads   审计哈希链链头（M4）：受控串行写入的 tamper-evident 链
audit_archives      审计归档清单（M4）：清理前必须已归档且校验通过
=============== ============================================================
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn

from aiops_agent import mode

# 自增主键：MySQL BIGINT，SQLite 退化为 INTEGER（SQLite 仅 INTEGER PK 可自增）
_BIGINT = BigInteger().with_variant(Integer, "sqlite")

metadata = MetaData()

system_kv = Table(
    "system_kv",
    metadata,
    Column("kv_key", String(128), primary_key=True),
    Column("kv_value", Text, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

console_tokens = Table(
    "console_tokens",
    metadata,
    Column("id", _BIGINT, primary_key=True, autoincrement=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("user_name", String(128), nullable=False),
    Column("role", Enum("viewer", "operator", "admin", name="token_role"), nullable=False),
    Column(
        "state",
        Enum("active", "previous", "revoked", name="token_state"),
        nullable=False,
        server_default="active",
    ),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime),
    Column("retired_at", DateTime),
    Column("rotated_from", String(64)),
    Index("idx_tokens_state", "state"),
)

monitored_apps = Table(
    "monitored_apps",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("url", String(512), nullable=False),
    Column("service", String(128), nullable=False, server_default=""),
    Column("log_path", String(512), nullable=False, server_default=""),
    # 页面健康关键字（可选，空=仅连接级探测）：响应内容须包含该关键字才算在线；
    # 旧生产库升级需迁移：ALTER TABLE monitored_apps ADD COLUMN probe_keyword VARCHAR(256) NOT NULL DEFAULT '';
    Column("probe_keyword", String(256), nullable=False, server_default=""),
    # 健康检查路径（可选，空=根路径 "/"）：Manifest 自动预填；探测/巡检按该路径校验关键字
    # 旧生产库升级需迁移：ALTER TABLE monitored_apps ADD COLUMN health_path VARCHAR(512) NOT NULL DEFAULT '';
    Column("health_path", String(512), nullable=False, server_default=""),
    # 修复目标仓库路径（可选，空=不参与自动修复）；AIOps 修复引擎据此定位并生成补丁。
    # 旧生产库升级需迁移：ALTER TABLE monitored_apps ADD COLUMN repo VARCHAR(1024) NOT NULL DEFAULT '';
    Column("repo", String(1024), nullable=False, server_default=""),
    # 依赖关系（P2-2 浅拓扑）：本应用依赖的服务名 JSON 数组（如 ["coupon"]）；
    # 供 triage 注入「上游依赖/下游调用方 + 依赖健康」上下文。
    # 旧生产库升级需迁移：ALTER TABLE monitored_apps ADD COLUMN depends_on JSON NULL;
    Column("depends_on", JSON),
    Column("enabled", Boolean, nullable=False, server_default=text("1")),
    # 注意：MySQL 8 严格模式下 TEXT 列不允许 DEFAULT（''）——不设 server_default，
    # 由写入路径显式提供空串（bff/monitored_apps.py 的 _insert_apps 恒赋值）
    Column("note", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", _BIGINT, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("actor", String(128), nullable=False),
    Column("action", String(64), nullable=False),
    Column("workflow_id", String(255), nullable=False, server_default=""),
    Column("target", String(255)),
    Column("detail", JSON, nullable=False),
    Column("result", String(32), nullable=False, server_default="ok"),
    Column("mode", String(16), nullable=False, server_default="production"),
    # ---- tamper-evident 哈希链（M4）----
    # event_id 为条目身份；prev_hash/entry_hash 串成单链（历史行可为 NULL，链从首条新记录开始）
    Column("event_id", String(64)),
    Column("command_id", String(64)),
    Column("prev_hash", String(64)),
    Column("entry_hash", String(64)),
    Index("idx_audit_ts", "ts"),
    Index("idx_audit_wf", "workflow_id"),
    Index("idx_audit_actor", "actor"),
    Index("idx_audit_event", "event_id"),
    Index("idx_audit_command", "command_id"),
)

workflow_runs = Table(
    "workflow_runs",
    metadata,
    Column("workflow_id", String(255), primary_key=True),
    Column("run_id", String(128), nullable=False, server_default=""),
    Column("stage", String(32), nullable=False, server_default=""),
    Column("exec_status", String(32), nullable=False, server_default=""),
    # 流程来源（M5）：alert=告警修复 / requirement=需求修复；运营指标必须分开统计
    Column("kind", String(16), nullable=False, server_default="alert"),
    # 变更风险等级原文（M5）：来自 patch.risk，指标侧按高/中/低/未知分桶
    Column("risk", String(32), nullable=False, server_default=""),
    Column("alert", JSON),
    Column("confidence", Float),
    Column("patch_id", String(128)),
    Column("start_time", DateTime),
    Column("close_time", DateTime),
    Column("duration_seconds", Float),
    Column("result", JSON),
    # ---- Temporal History 同步位置（M4）----
    # last_history_event_id 是已落库的最大事件号；history_complete 表示终态流程已完成最终同步
    Column("last_history_event_id", _BIGINT, nullable=True),
    Column("history_synced_at", DateTime, nullable=True),
    Column("history_complete", Boolean, nullable=False, server_default=text("0")),
    Column("mode", String(16), nullable=False, server_default="production"),
    Column("updated_at", DateTime, nullable=False),
    Index("idx_runs_stage", "stage"),
    Index("idx_runs_started", "start_time"),
    Index("idx_runs_close_kind", "close_time", "kind"),
)

# ---- 写命令 outbox（M2，语义见 bff/commands.py）----
# 意图审计与命令行在同一事务写入；投递失败可按 next_retry_at 有界重试，
# 状态固定为 PENDING/SUBMITTED/ACCEPTED/FAILED（业务审批结果另由工作流记录）。
workflow_commands = Table(
    "workflow_commands",
    metadata,
    Column("command_id", String(64), primary_key=True),
    # approval / second-approval / deploy-command / queue-patch
    Column("command_type", String(32), nullable=False),
    Column("wf_id", String(255), nullable=False),
    # 命令针对的流程运行实例（防止同名流程重跑后把回执归到新 run）
    Column("run_id", String(128), nullable=False, server_default=""),
    Column("actor", String(128), nullable=False),
    Column("channel", String(32), nullable=False),
    # 审计动作名（chatops:/card-callback: 等通道后缀在意图阶段固定，重试/回执沿用）
    Column("audit_action", String(64), nullable=False, server_default=""),
    # 原始请求载荷（审批信封或 deploy/queue 参数；工作流按 command_id 去重）
    Column("payload", JSON, nullable=False),
    # 审计附加字段（decision/comment/operator 等；意图行与结果行共用同口径）
    Column("audit_params", JSON),
    Column("status", String(16), nullable=False, server_default="PENDING"),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("5")),
    Column("next_retry_at", DateTime),
    Column("last_error", Text),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("submitted_at", DateTime),
    Column("completed_at", DateTime),
    Index("idx_commands_status_next", "status", "next_retry_at"),
    Index("idx_commands_wf", "wf_id"),
)

# ---- Temporal History 投影（M4，语义见 bff/history_projector.py）----
# 以 (workflow_id,run_id,event_id) 幂等写入；detail 保存完整 HistoryEvent JSON，
# 不受 UI 列表 200 条上限影响。运行中增量追加，终态完成一次最终同步。
workflow_events = Table(
    "workflow_events",
    metadata,
    Column("workflow_id", String(255), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("event_id", _BIGINT, primary_key=True),
    Column("event_time", DateTime),
    Column("event_type", String(64), nullable=False, server_default=""),
    Column("detail", JSON, nullable=False),
    Column("mode", String(16), nullable=False, server_default="production"),
    Column("synced_at", DateTime, nullable=False),
    Index("idx_wf_events_type", "event_type"),
    Index("idx_wf_events_time", "event_time"),
)

# ---- 审计哈希链链头（M4）----
# 单链 scope=audit；写入方在同一事务/文件锁内读取链头 → 追加条目 → 更新链头。
# 明确边界：tamper-evident（可发现篡改），不等同外部 WORM 不可变存储。
audit_chain_heads = Table(
    "audit_chain_heads",
    metadata,
    Column("scope", String(64), primary_key=True),
    Column("last_event_id", String(64), nullable=False, server_default=""),
    Column("last_hash", String(64), nullable=False, server_default=""),
    Column("updated_at", DateTime, nullable=False),
)

# ---- 审计归档清单（M4）----
# 清理任务只删除「已有归档记录且校验通过」的过期审计；归档本体保留在本地。
audit_archives = Table(
    "audit_archives",
    metadata,
    Column("archive_id", String(64), primary_key=True),
    Column("day", String(16), nullable=False),
    Column("path", String(1024), nullable=False),
    Column("rows", Integer, nullable=False, server_default=text("0")),
    # 归档覆盖的审计行 ID 区间（production 清理按区间删除，不按时间二次推断）
    Column("first_id", _BIGINT, nullable=True),
    Column("last_id", _BIGINT, nullable=True),
    Column("first_ts", DateTime),
    Column("last_ts", DateTime),
    Column("chain_ok", Boolean, nullable=False, server_default=text("0")),
    Column("manifest_sha256", String(64), nullable=False, server_default=""),
    Column("status", String(16), nullable=False, server_default="archived"),
    Column("created_at", DateTime, nullable=False),
    Index("idx_audit_archives_day", "day"),
)

escalations = Table(
    "escalations",
    metadata,
    Column("id", String(160), primary_key=True),  # esc-<wf_id>
    Column("wf_id", String(255), nullable=False),
    # 原流程 run_id（M1 重试溯源）：重试流程以 parent_run_id 关联，避免同名 wf_id 重跑混淆
    Column("run_id", String(128), nullable=False, server_default=""),
    Column("alert_id", String(255), nullable=False, server_default=""),
    Column("service", String(128), nullable=False, server_default=""),
    # 自动升级原因摘要（由 gate_events 翻译；MySQL TEXT 无 DEFAULT，写入显式提供）
    Column("auto_reason", Text, nullable=False),
    # 处置分类（M1）：model/approval/sandbox/release/observability/evidence/requirement/other；
    # 历史待办由 bff.escalations.backfill_categories 回填，只分类不改变状态。
    Column("category", String(32), nullable=False, server_default=""),
    # 结构化关闭记录（M2）：close_reason/actual_root_cause/actual_action/verification/
    # verdict/knowledge_candidate/knowledge_status 等；旧格式关闭为 NULL（前端按「未结构化」展示）
    Column("resolution", JSON),
    # 处置计划（M2）：未关闭待办的处理计划（关闭后保留，便于历史待办分派与追溯）
    Column("plan", String(1024), nullable=False, server_default=""),
    Column("alert", JSON),
    Column(
        "status",
        # retrying（M3）：人工重试已启动，原待办等待新流程终态；不立即关闭
        Enum("open", "assigned", "retrying", "closed", name="escalation_status"),
        nullable=False,
        server_default="open",
    ),
    Column("assignee", String(128), nullable=False, server_default=""),
    Column("note", Text, nullable=False),
    Column("escalated_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("closed_at", DateTime),
    Column("re_escalated", Boolean, nullable=False, server_default=text("0")),
    Column("history", JSON, nullable=False),
    # ---- 人工重试跟踪（M3）----
    # 新流程 ID 与原运行实例：DONE 才自动关闭，其他终态重开并保留完整父子链
    Column("retry_wf_id", String(255), nullable=False, server_default=""),
    Column("parent_run_id", String(128), nullable=False, server_default=""),
    Column("retry_started_at", DateTime),
    Column("retry_outcome", String(32), nullable=False, server_default=""),
    Column("retry_outcome_at", DateTime),
    # ---- 再升级通知真实送达（M3）----
    # 只有 notify_mode=live 才 re_escalated；recorded/failed 按退避持续重试
    Column("notify_mode", String(16), nullable=False, server_default=""),
    Column("notify_attempts", Integer, nullable=False, server_default=text("0")),
    Column("notify_next_retry_at", DateTime),
    Column("notify_error", Text),
    Column("notify_updated_at", DateTime),
    # 记录版本（M3 CAS）：人工写操作携带 version，避免并发覆盖
    Column("version", Integer, nullable=False, server_default=text("1")),
    Index("idx_escalations_status", "status"),
    Index("idx_escalations_wf", "wf_id"),
)

requirement_analyses = Table(
    "requirement_analyses",
    metadata,
    Column("id", String(160), primary_key=True),  # ra-<app_id>-<entry_id>
    Column("app_id", String(64), nullable=False),
    Column("service", String(128), nullable=False, server_default=""),
    Column("entry_id", String(64), nullable=False),
    Column("entry_title", String(512), nullable=False),
    Column("entry_kind", String(32), nullable=False, server_default=""),
    Column("entry_priority", String(16), nullable=False, server_default=""),
    # 需求条目快照（title/content/assessment/...，含全部展示字段；JSON 无 DEFAULT，写入显式提供）
    Column("entry_snapshot", JSON, nullable=False),
    Column(
        "status",
        Enum("analyzing", "analyzed", "failed", "approved", name="req_analysis_status"),
        nullable=False,
        server_default="analyzing",
    ),
    Column("current_version", Integer, nullable=False, server_default=text("0")),
    Column("versions", JSON, nullable=False),
    Column("approved", JSON),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Index("idx_req_analysis_app", "app_id"),
    Index("idx_req_analysis_status", "status"),
)

console_users = Table(
    "console_users",
    metadata,
    Column("username", String(64), primary_key=True),
    Column("password_hash", String(256), nullable=False),
    Column("role", Enum("viewer", "operator", "admin", name="user_role"), nullable=False),
    Column(
        "state",
        Enum("active", "disabled", name="user_state"),
        nullable=False,
        server_default="active",
    ),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

console_sessions = Table(
    "console_sessions",
    metadata,
    Column("id", _BIGINT, primary_key=True, autoincrement=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("user_name", String(64), nullable=False),
    Column("role", Enum("viewer", "operator", "admin", name="session_role"), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime),
    Column("revoked", Boolean, nullable=False, server_default=text("0")),
    Column("revoked_at", DateTime),
    Index("idx_sessions_user", "user_name"),
    Index("idx_sessions_expires", "expires_at"),
)

# ---- C 批次：告警事件聚合（跨实例持久化，语义见 aiops_agent/alert_store.py）----

# 故障域：domain_key = {mode}:{namespace}:{service}；current_event_id 指向当前聚合事件
# （未启动的 pending 事件保持指向，保证同域不会顶出第二条代表流程）
alert_domains = Table(
    "alert_domains",
    metadata,
    Column("domain_key", String(255), primary_key=True),
    Column("service", String(128), nullable=False),
    Column("runtime_mode", String(16), nullable=False, server_default="production"),
    Column("temporal_namespace", String(128), nullable=False, server_default=""),
    Column("current_event_id", String(160)),
    Column("window_started_at", DateTime),
    Column("updated_at", DateTime, nullable=False),
)

alert_events = Table(
    "alert_events",
    metadata,
    Column("event_id", String(160), primary_key=True),  # evt-{service}-{uuid8}
    Column("domain_key", String(255), nullable=False),
    Column("service", String(128), nullable=False),
    # 代表告警 / 策略快照：启动代表流程的原样入参；事件创建后不可变
    Column("representative_alert", JSON, nullable=False),
    Column("representative_wf_id", String(255), nullable=False),
    Column("policy_snapshot", JSON, nullable=False),
    Column("first_received_at", DateTime, nullable=False),
    Column(
        "status",
        Enum("pending", "started", name="alert_event_status"),
        nullable=False,
        server_default="pending",
    ),
    # 租约认领：claim_token 非空表示被某实例持有；lease 过期可被任意实例接管重试
    Column("claim_token", String(64), nullable=False, server_default=""),
    Column("lease_expires_at", DateTime),
    Column("retry_at", DateTime),  # 补偿退避：启动失败后的最早重试时间
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("error_summary", Text),
    Column("started_at", DateTime),
    Column("updated_at", DateTime, nullable=False),
    Index("idx_alert_events_status", "status", "retry_at"),
    Index("idx_alert_events_domain", "domain_key"),
)

# ---- 日志证据归档（转人工核对，语义见 aiops_agent/evidence_store.py）----
# 一个 workflow 一条证据：原始日志行只保留末尾 MAX_STORED_LINES 行，
# 行数与截断标记一并落库，避免截断后的视图被误当成全量。
log_evidence = Table(
    "log_evidence",
    metadata,
    Column("wf_id", String(255), primary_key=True),
    Column("alert_id", String(255), nullable=False, server_default=""),
    Column("service", String(128), nullable=False, server_default=""),
    Column("collected_at", DateTime, nullable=False),
    Column("archived_at", DateTime, nullable=False),
    Column("loki_available", Boolean, nullable=False, server_default=text("0")),
    Column("total_lines", Integer, nullable=False, server_default=text("0")),
    Column("stored_lines", Integer, nullable=False, server_default=text("0")),
    Column("truncated", Boolean, nullable=False, server_default=text("0")),
    Column("trace_ids", JSON, nullable=False),
    # 模板聚类：Drain3 降噪后的 [{id, pattern, count}]（最多 MAX_TEMPLATES 条）
    Column("templates", JSON, nullable=False),
    Column("compression_ratio", Float),
    # 原始日志行（文本列表；JSON 无 DEFAULT，写入显式提供）
    Column("lines", JSON, nullable=False),
    # 证据质量摘要（M1）：evidence_quality.evaluate 的六状态结果；旧归档为 NULL（前端显示 UNKNOWN）
    Column("quality", JSON),
    Index("idx_evidence_service", "service"),
)

alert_members = Table(
    "alert_members",
    metadata,
    Column("id", _BIGINT, primary_key=True, autoincrement=True),
    Column("event_id", String(160), nullable=False),
    Column("domain_key", String(255), nullable=False),
    Column("alert_id", String(255), nullable=False),
    Column("service", String(128), nullable=False),
    Column("received_at", DateTime, nullable=False),
    # 成员去重键：同故障域内 alert_id 只计一次（重复投递 / 多实例并发幂等）
    UniqueConstraint("domain_key", "alert_id", name="uq_alert_members_domain_alert"),
    Index("idx_alert_members_event", "event_id"),
)

_engine: Engine | None = None
_engine_url: str | None = None
_schema_ready = False


def get_engine() -> Engine:
    """返回进程级复用引擎（URL 变化时自动重建，供测试注入不同库）。

    首次调用即执行幂等建表（``checkfirst=True``），空库启动免手工 DDL。
    """
    global _engine, _engine_url, _schema_ready
    url = mode.db_url()
    if url is None:
        raise mode.ModeConfigError(
            "当前为演示模式（AIOPS_MODE=demo），数据库层未启用；请设置 AIOPS_MODE=production"
        )
    if _engine is None or _engine_url != url:
        kwargs: dict = {"pool_pre_ping": True, "echo": False, "future": True}
        if url.startswith("sqlite"):
            # SQLite：FastAPI 线程池 + 采集器多线程共用，放开线程检查；
            # 内存库需 StaticPool 才能让所有连接看到同一份数据
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url or url.endswith("://"):
                kwargs["poolclass"] = StaticPool
        else:
            # MySQL：方案连接池参数（wait_timeout 防断连）
            kwargs.update(pool_size=10, max_overflow=20, pool_recycle=3600)
        _engine = create_engine(url, **kwargs)
        _engine_url = url
        _schema_ready = False
    if not _schema_ready:
        ensure_schema(_engine)
        _schema_ready = True
    return _engine


def ensure_schema(engine: Engine | Connection) -> list[str]:
    """显式 schema 升级入口（幂等）：建缺失表 + 补缺失列，返回执行的补列 DDL。

    ``create_all(checkfirst=True)`` 只建缺失表、不补列：老库升级路径必须显式补列
    （如 ``monitored_apps.depends_on``）；readiness 以 metadata 为契约做校验。
    复合主键/主键列缺失直接跳过（无法 ALTER ADD），交由 readiness 暴露真实缺口。
    """
    metadata.create_all(engine, checkfirst=True)
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    statements: list[str] = []
    for name, table in metadata.tables.items():
        if name not in existing:
            continue
        actual = {col["name"] for col in inspector.get_columns(name)}
        for column in table.columns:
            if column.name in actual or column.primary_key:
                continue
            ddl = f"ALTER TABLE {name} ADD COLUMN {CreateColumn(column).compile(dialect=engine.dialect)}"
            # Alembic 传入已处于事务中的 Connection；应用启动传入 Engine。
            context = engine.begin() if isinstance(engine, Engine) else nullcontext(engine)
            with context as conn:
                conn.execute(text(ddl))
            statements.append(ddl)
    return statements


def reset_engine() -> None:
    """释放并清空引擎缓存（测试用；生产进程内无需调用）。"""
    global _engine, _engine_url, _schema_ready
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None
    _schema_ready = False


# ----------------------------------------------------------------------
# 时间转换：应用层统一 UTC，入库为 naive UTC（MySQL DATETIME 无时区）
# ----------------------------------------------------------------------


def to_dt(value: str | datetime | None) -> datetime | None:
    """ISO 字符串 / datetime → 入库用 naive UTC datetime；非法输入返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def to_iso(value: datetime | None) -> str | None:
    """入库 datetime（naive UTC）→ UTC ISO 字符串（自动微秒，与既有文件后端口径一致）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """当前 UTC 时间（含微秒，与文件后端历史格式一致）。"""
    return datetime.now(timezone.utc).isoformat()


def now_dt() -> datetime:
    """当前 UTC 的 naive datetime（入库用）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
