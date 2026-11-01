"""当前生产结构基线；已有未版本化库须先核对后 stamp，而非重复 upgrade。"""
from alembic import op

from aiops_agent import db

revision = "20261101_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 基线同时覆盖空库与历史 ensure_schema 结构；后续变更使用显式 op 迁移。
    db.ensure_schema(op.get_bind())


def downgrade() -> None:
    # 仅在隔离的迁移演练中回退全部基线表，生产禁止直接执行。
    db.metadata.drop_all(op.get_bind(), checkfirst=True)
