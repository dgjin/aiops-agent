"""escalations 增加 run_id（M1 重试溯源）。

重试启动新流程时，需要把原件 ``parent_run_id`` 写入证据上下文；同一 ``wf_id``
可能被重跑（run_id 变化），仅存 wf_id 无法确定被重试的是哪一次运行。

空库与历史库幂等：``20261101_0001`` 基线会先调用 ``db.ensure_schema``（已包含新列），
因此升级前必须 inspect，缺列才加；历史行由 ``server_default`` 兜底为空串
（旧待办无 run_id，重试时 parent_run_id 留空而不是伪造）。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "20261101_0005"
down_revision = "20261101_0004"
branch_labels = None
depends_on = None

TABLE = "escalations"
COLUMN = "run_id"


def _existing(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if COLUMN not in _existing(TABLE):
        op.add_column(
            TABLE,
            Column(COLUMN, String(128), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if COLUMN in _existing(TABLE):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
