"""escalations 增加 retrying 状态、重试跟踪、通知送达与记录版本（M3）。

背景：人工重试此前「启动即关闭」原待办，新流程失败后无人接手；SLA 再升级也不区分
通知是否真实送达。本迁移补齐三类字段：

- ``retrying`` 状态与 ``retry_wf_id/parent_run_id/retry_started_at/retry_outcome``：
  重试启动后原待办保持跟踪，DONE 才自动关闭，其他终态重开；
- ``notify_*``：投递方式 / 次数 / 下次重试时间 / 最近错误，只有 live 才算已通知；
- ``version``：写操作 CAS，避免改派、关闭和重试并发覆盖。

空库与历史库幂等：``20261101_0001`` 基线会先调用 ``db.ensure_schema``（metadata 已含
新列与新枚举值），升级前必须 inspect，缺列才加。MySQL 需显式扩展 ENUM；SQLite 的
Enum 仅作 VARCHAR 校验，无需改列。降级把 ``retrying`` 回退为 ``open`` 后再收窄枚举，
避免历史行落在非法枚举值上（历史已关闭记录不批量改写）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261101_0007"
down_revision = "20261101_0006"
branch_labels = None
depends_on = None

TABLE = "escalations"

# 新列定义（顺序与 db.py 保持一致，便于 readiness 对照）
COLUMNS = (
    sa.Column("retry_wf_id", sa.String(255), nullable=False, server_default=""),
    sa.Column("parent_run_id", sa.String(128), nullable=False, server_default=""),
    sa.Column("retry_started_at", sa.DateTime(), nullable=True),
    sa.Column("retry_outcome", sa.String(32), nullable=False, server_default=""),
    sa.Column("retry_outcome_at", sa.DateTime(), nullable=True),
    sa.Column("notify_mode", sa.String(16), nullable=False, server_default=""),
    sa.Column("notify_attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("notify_next_retry_at", sa.DateTime(), nullable=True),
    sa.Column("notify_error", sa.Text(), nullable=True),
    sa.Column("notify_updated_at", sa.DateTime(), nullable=True),
    sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
)

_STATUS_OLD = sa.Enum("open", "assigned", "closed", name="escalation_status")
_STATUS_NEW = sa.Enum("open", "assigned", "retrying", "closed", name="escalation_status")


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE)}


def _is_mysql() -> bool:
    return op.get_bind().dialect.name in ("mysql", "mariadb")


def upgrade() -> None:
    existing = _existing_columns()
    for column in COLUMNS:
        if column.name not in existing:
            op.add_column(TABLE, column)
    if _is_mysql():
        op.alter_column(
            TABLE,
            "status",
            existing_type=_STATUS_OLD,
            type_=_STATUS_NEW,
            existing_nullable=False,
        )


def downgrade() -> None:
    if _is_mysql():
        # 先收敛数据再收窄枚举：retrying 回退为 open（等待人工重新处置）
        op.execute(f"UPDATE {TABLE} SET status='open' WHERE status='retrying'")
        op.alter_column(
            TABLE,
            "status",
            existing_type=_STATUS_NEW,
            type_=_STATUS_OLD,
            existing_nullable=False,
        )
    existing = _existing_columns()
    with op.batch_alter_table(TABLE) as batch:
        for column in reversed(COLUMNS):
            if column.name in existing:
                batch.drop_column(column.name)
