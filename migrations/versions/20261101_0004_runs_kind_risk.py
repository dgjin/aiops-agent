"""workflow_runs 增加 kind / risk（M5 运营指标分维度）。

新增列与联合索引：
    - ``kind``（VARCHAR(16)，默认 ``alert``）：告警修复 / 需求修复；
    - ``risk``（VARCHAR(32)，默认空串）：``patch.risk`` 原文，指标侧按高/中/低/未知分桶；
    - ``idx_runs_close_kind``（close_time, kind）：面板按窗口 + 来源取数。

空库与历史库幂等：``20261101_0001`` 基线会先调用 ``db.ensure_schema``（已包含新列与
索引），因此升级前必须 inspect，缺列/缺索引才补；历史行由 ``server_default`` 兜底为
``alert`` + 未知风险，不会把存量数据误计为已知低风险。

降级必须先删索引再删列：SQLite 的 batch recreate 会按反射结果重建索引，
若索引仍引用 ``kind`` 会在复制表阶段报 ``no such column: kind``。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "20261101_0004"
down_revision = "20261101_0003"
branch_labels = None
depends_on = None

TABLE = "workflow_runs"
COLUMNS = ("kind", "risk")
INDEX_NAME = "idx_runs_close_kind"


def _columns(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    existing = _columns(TABLE)
    if "kind" not in existing:
        op.add_column(
            TABLE,
            Column("kind", String(16), nullable=False, server_default="alert"),
        )
    if "risk" not in existing:
        op.add_column(
            TABLE,
            Column("risk", String(32), nullable=False, server_default=""),
        )
    if TABLE in inspect(op.get_bind()).get_table_names() and INDEX_NAME not in _indexes(TABLE):
        op.create_index(INDEX_NAME, TABLE, ["close_time", "kind"])


def downgrade() -> None:
    if TABLE not in inspect(op.get_bind()).get_table_names():
        return
    if INDEX_NAME in _indexes(TABLE):
        op.drop_index(INDEX_NAME, table_name=TABLE)
    existing = _columns(TABLE)
    for column in COLUMNS:
        if column not in existing:
            continue
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(column)
