"""escalations 增加结构化关闭记录与处置计划（M2）。

新增列：
    - ``resolution``（JSON，可空）：close_reason / actual_root_cause / actual_action /
      verification / verdict / knowledge_candidate / knowledge_status 等；
    - ``plan``（VARCHAR(1024)，默认空串）：未关闭待办的处理计划。

空库与历史库幂等：``20261101_0001`` 基线会先调用 ``db.ensure_schema``（已包含新列），
因此升级前必须 inspect，缺列才加。MySQL 8 的 TEXT 不能带默认值，这里 plan 使用
VARCHAR 以便安全加列。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "20261101_0003"
down_revision = "20261101_0002"
branch_labels = None
depends_on = None

COLUMNS = (("escalations", "resolution"), ("escalations", "plan"))


def _existing(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    existing = _existing("escalations")
    if "resolution" not in existing:
        op.add_column("escalations", op.Column("resolution", op.JSON(), nullable=True))
    if "plan" not in existing:
        op.add_column(
            "escalations",
            op.Column("plan", op.String(1024), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for table, column in COLUMNS:
        if column not in _existing(table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
