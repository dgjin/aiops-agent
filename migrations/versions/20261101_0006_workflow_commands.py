"""新增 workflow_commands 写命令 outbox（M2）。

背景：BFF 写操作此前是「先 signal 再写审计」——Temporal 不可达时用户意图丢失，
signal 成功后审计写失败又会出现「无审计推进」。本表把「操作意图审计 + 待投递命令」
放进同一事务，投递状态可查询、失败可退避重试。

空库幂等：``20261101_0001`` 基线先调用 ``db.ensure_schema``（metadata 已含新表），
升级时用 ``checkfirst`` 建表；历史库则在此显式创建。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261101_0006"
down_revision = "20261101_0005"
branch_labels = None
depends_on = None

TABLE = "workflow_commands"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return
    op.create_table(
        TABLE,
        sa.Column("command_id", sa.String(64), primary_key=True),
        sa.Column("command_type", sa.String(32), nullable=False),
        sa.Column("wf_id", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("audit_action", sa.String(64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("audit_params", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_commands_status_next", TABLE, ["status", "next_retry_at"])
    op.create_index("idx_commands_wf", TABLE, ["wf_id"])


def downgrade() -> None:
    if TABLE not in _tables():
        return
    op.drop_index("idx_commands_wf", table_name=TABLE)
    op.drop_index("idx_commands_status_next", table_name=TABLE)
    op.drop_table(TABLE)
