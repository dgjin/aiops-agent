"""运行模式层：production（默认）/ demo，双模互不影响。

设计要点：

- **单一代码库双模**：``AIOPS_MODE`` 决定持久化后端与运行参数——
  ``demo`` 保持零外部依赖的文件后端（JSON / JSONL，即既有演示形态，历史数据零迁移）；
  ``production`` 启用 MySQL 持久化（审计 / 令牌 / 清单 / 流程速查 / 系统开关）；
- **默认 production 且 fail-fast**：忘记配置数据库时 :func:`db_url` 抛错，
  避免"以为落库、实际静默丢数据"；演示经 ``.env`` / ``scripts/demo-up.sh`` 显式声明 demo；
- **数据目录隔离**：demo → ``<repo>/data/``（既有演示数据原地保留）；
  production → ``<repo>/data/prod/``（可用 ``AIOPS_DATA_ROOT`` 显式覆盖）；
- **测试**：``tests/conftest.py`` 强制 ``AIOPS_MODE=demo``，测试永不触达生产库。

配置::

    AIOPS_MODE=demo|production                          # 默认 production
    AIOPS_DATABASE_URL=mysql+pymysql://<user>@<host>:3306/aiops?charset=utf8mb4
    AIOPS_DATA_ROOT=/var/lib/aiops                      # 可选：显式数据根目录（覆盖模式默认）
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_MODE = "AIOPS_MODE"
ENV_DB_URL = "AIOPS_DATABASE_URL"
ENV_DATA_ROOT = "AIOPS_DATA_ROOT"

MODE_PRODUCTION = "production"
MODE_DEMO = "demo"

_REPO_ROOT = Path(__file__).resolve().parent.parent


class ModeConfigError(RuntimeError):
    """模式配置错误（如 production 缺数据库连接串）——由启动入口转 fail-fast。"""


def mode() -> str:
    """当前运行模式（运行时读取环境变量，便于测试与子进程注入）。"""
    value = (os.environ.get(ENV_MODE) or "").strip().lower()
    if value in ("", MODE_PRODUCTION):
        return MODE_PRODUCTION
    if value == MODE_DEMO:
        return MODE_DEMO
    raise ModeConfigError(f"{ENV_MODE} 仅支持 {MODE_PRODUCTION} / {MODE_DEMO}：{value!r}")


def is_demo() -> bool:
    return mode() == MODE_DEMO


def is_production() -> bool:
    return mode() == MODE_PRODUCTION


def data_root() -> Path:
    """数据根目录：``AIOPS_DATA_ROOT`` 优先；否则 demo → ``data/``、production → ``data/prod/``。"""
    explicit = (os.environ.get(ENV_DATA_ROOT) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if is_demo():
        return _REPO_ROOT / "data"
    return _REPO_ROOT / "data" / "prod"


def db_url() -> str | None:
    """数据库连接串；**demo 模式恒为 None**（演示永不连接任何数据库）。

    production 未配置 ``AIOPS_DATABASE_URL`` 时抛 :class:`ModeConfigError`（fail-fast，
    不允许"静默降级到文件"——那会让生产数据悄悄丢失）。
    """
    if is_demo():
        return None
    url = (os.environ.get(ENV_DB_URL) or "").strip()
    if url:
        return url
    raise ModeConfigError(
        f"production 模式必须配置 {ENV_DB_URL}"
        "（示例：mysql+pymysql://<user>:<password>@<host>:3306/aiops?charset=utf8mb4）；"
        f"纯演示请显式设置 {ENV_MODE}={MODE_DEMO}"
    )
