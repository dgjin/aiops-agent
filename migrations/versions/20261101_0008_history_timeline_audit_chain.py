"""完整历史、统一时间线与审计证据包（M4）。

新增：

- ``workflow_events``：Temporal History 投影，``(workflow_id,run_id,event_id)`` 幂等；
- ``workflow_runs.last_history_event_id/history_synced_at/history_complete``：同步位置与完整性；
- ``audit_events.event_id/command_id/prev_hash/entry_hash``：tamper-evident 哈希链字段
  （历史行保持 NULL，链从首条新记录开始）；
- ``audit_chain_heads``：单链受控链头（数据库行锁串行写入）；
- ``audit_archives``：本地归档清单；清理只删除已归档且 SHA-256 复核通过的行。

幂等：空库在 ``20261101_0001`` 已由 ``db.ensure_schema`` 建出新表/新列，本迁移先
inspect 再补，避免重复 DDL。降级仅用于隔离演练：删除链字段会使既有审计无法再校验，
生产执行前必须先导出归档。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261101_0008"
down_revision = "20261101_0007"
branch_labels = None
depends_on = None

_AUDIT_COLUMNS = (
    sa.Column("event_id", sa.String(64), nullable=True),
    sa.Column("command_id", sa.String(64), nullable=True),
    sa.Column("prev_hash", sa.String(64), nullable=True),
    sa.Column("entry_hash", sa.String(64), nullable=True),
)

_RUN_COLUMNS = (
    sa.Column("last_history_event_id", sa.BigInteger(), nullable=True),
    sa.Column("history_synced_at", sa.DateTime(), nullable=True),
    sa.Column("history_complete", sa.Boolean(), nullable=False, server_default=sa.text("0")),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_inspector().get_table_names())


def _columns(table: str) -> set[str]:
    inspector = _inspector()
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = _inspector()
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def _create_tables() -> None:
    existing = _tables()
    if "workflow_events" not in existing:
        op.create_table(
            "workflow_events",
            sa.Column("workflow_id", sa.String(255), primary_key=True),
            sa.Column("run_id", sa.String(128), primary_key=True),
            sa.Column("event_id", sa.BigInteger(), primary_key=True),
            sa.Column("event_time", sa.DateTime(), nullable=True),
            sa.Column("event_type", sa.String(64), nullable=False, server_default=""),
            sa.Column("detail", sa.JSON(), nullable=False),
            sa.Column("mode", sa.String(16), nullable=False, server_default="production"),
            sa.Column("synced_at", sa.DateTime(), nullable=False),
        )
        op.create_index("idx_wf_events_type", "workflow_events", ["event_type"])
        op.create_index("idx_wf_events_time", "workflow_events", ["event_time"])
    if "audit_chain_heads" not in existing:
        op.create_table(
            "audit_chain_heads",
            sa.Column("scope", sa.String(64), primary_key=True),
            sa.Column("last_event_id", sa.String(64), nullable=False, server_default=""),
            sa.Column("last_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if "audit_archives" not in existing:
        op.create_table(
            "audit_archives",
            sa.Column("archive_id", sa.String(64), primary_key=True),
            sa.Column("day", sa.String(16), nullable=False),
            sa.Column("path", sa.String(1024), nullable=False),
            sa.Column("rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("first_id", sa.BigInteger(), nullable=True),
            sa.Column("last_id", sa.BigInteger(), nullable=True),
            sa.Column("first_ts", sa.DateTime(), nullable=True),
            sa.Column("last_ts", sa.DateTime(), nullable=True),
            sa.Column("chain_ok", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("manifest_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="archived"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("idx_audit_archives_day", "audit_archives", ["day"])


def upgrade() -> None:
    _create_tables()

    existing = _columns("audit_events")
    for column in _AUDIT_COLUMNS:
        if column.name not in existing:
            op.add_column("audit_events", column)
    audit_indexes = _indexes("audit_events")
    if "idx_audit_event" not in audit_indexes:
        op.create_index("idx_audit_event", "audit_events", ["event_id"])
    if "idx_audit_command" not in audit_indexes:
        op.create_index("idx_audit_command", "audit_events", ["command_id"])

    existing = _columns("workflow_runs")
    for column in _RUN_COLUMNS:
        if column.name not in existing:
            op.add_column("workflow_runs", column)


def downgrade() -> None:
    existing = _columns("workflow_runs")
    with op.batch_alter_table("workflow_runs") as batch:
        for column in reversed(_RUN_COLUMNS):
            if column.name in existing:
                batch.drop_column(column.name)

    # SQLite 的 batch 模式会反射并重建索引：必须先删掉引用待删列的索引，
    # 否则重建时会报 “no such column: event_id/command_id”。
    indexes = _indexes("audit_events")
    for name in ("idx_audit_event", "idx_audit_command"):
        if name in indexes:
            op.drop_index(name, table_name="audit_events")

    existing = _columns("audit_events")
    with op.batch_alter_table("audit_events") as batch:
        for column in reversed(_AUDIT_COLUMNS):
            if column.name in existing:
                batch.drop_column(column.name)

    tables = _tables()
    if "audit_archives" in tables:
        if "idx_audit_archives_day" in _indexes("audit_archives"):
            op.drop_index("idx_audit_archives_day", table_name="audit_archives")
        op.drop_table("audit_archives")
    if "audit_chain_heads" in tables:
        op.drop_table("audit_chain_heads")
    if "workflow_events" in tables:
        indexes = _indexes("workflow_events")
        for name in ("idx_wf_events_type", "idx_wf_events_time"):
            if name in indexes:
                op.drop_index(name, table_name="workflow_events")
        op.drop_table("workflow_events")
