"""被监控应用注册表：把告警 service 映射到修复目标仓库与验证契约（热生效）。

数据源：``data/monitored_apps.json``（与 BFF 控制台同一份清单；可用
``AIOPS_MONITORED_APPS_PATH`` 覆盖，测试可指向临时文件）。
**每次调用读盘**——控制台修改清单后，修复链路下一次执行即生效，无需重启 worker。

未注册 / 未配置 repo / 目录不存在 → ``resolve()`` 返回 None，
调用方回退既有 demo-app 行为（零行为变更）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MONITORED_APPS_PATH = BASE_DIR / "data" / "monitored_apps.json"


def monitored_apps_path() -> Path:
    """清单文件路径（AIOPS_MONITORED_APPS_PATH 可覆盖，便于测试与多环境）。"""
    override = os.environ.get("AIOPS_MONITORED_APPS_PATH")
    return Path(override) if override else DEFAULT_MONITORED_APPS_PATH


def load_apps() -> list[dict]:
    """读取清单（缺文件/损坏/结构异常 → 空列表，调用方回退默认行为）。"""
    try:
        payload = json.loads(monitored_apps_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload if isinstance(payload, list) else []
    return [item for item in items if isinstance(item, dict)]


def resolve(service: str) -> dict | None:
    """按 service 解析修复目标。

    返回 ``{"name": str, "repo": Path, "url": str, "contract": {"keyword": str} | None}``；
    未注册 / 未配置 repo / 目录不存在时返回 None（调用方回退 demo-app）。
    """
    wanted = (service or "").strip().lower()
    if not wanted:
        return None
    for app in load_apps():
        if str(app.get("service") or "").strip().lower() != wanted:
            continue
        repo_raw = str(app.get("repo") or "").strip()
        if not repo_raw:
            return None
        repo = Path(repo_raw)
        if not repo.is_dir():
            return None
        keyword = str(app.get("probe_keyword") or "").strip()
        return {
            "name": str(app.get("name") or ""),
            "repo": repo,
            # 应用对外 URL：契约应用「直连发布」的真实探针地址（空串 → 调用方回落容器模式）
            "url": str(app.get("url") or "").strip(),
            "contract": {"keyword": keyword} if keyword else None,
        }
    return None
