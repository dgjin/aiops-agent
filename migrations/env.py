"""Alembic 运行环境：连接串只从运行环境读取，不写入版本库。"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from aiops_agent import db, mode
from aiops_agent.config import load_dotenv

load_dotenv()
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url = mode.db_url()
if url is None:
    raise RuntimeError("Alembic 仅用于 production；请设置 AIOPS_MODE=production 与 AIOPS_DATABASE_URL")
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
target_metadata = db.metadata


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
