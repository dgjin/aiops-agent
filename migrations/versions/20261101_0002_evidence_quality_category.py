"""M1：转人工待办增加处置分类，日志证据归档增加质量摘要。

幂等说明：
    - 基线（0001）直接调用 ``db.ensure_schema``，空库建表时已包含本迁移新增列，
      因此 upgrade 必须先 inspect 再补列，不能无条件 ``add_column``；
    - 已 stamp 到 0001 的历史库缺少这两列，由本迁移补齐；
    - downgrade 仅回退本迁移新增列（SQLite 走 batch 重建表），不触碰其它结构。
"""
from alembic import op
from sqlalchemy import inspect

revision = "20261101_0002"
down_revision = "20261101_0001"
branch_labels = None
depends_on = None

COLUMNS = (
    ("escalations", "category"),
    ("log_evidence", "quality"),
)


def _existing(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    missing = [(table, column) for table, column in COLUMNS if column not in _existing(table)]
    for table, column in missing:
        if table == "escalations":
            op.add_column(table, op.Column(column, op.String(32), nullable=False, server_default=""))
        else:
            op.add_column(table, op.Column(column, op.JSON(), nullable=True))


def downgrade() -> None:
    for table, column in COLUMNS:
        if column not in _existing(table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
