"""控制台操作审计：追加写 data/web-audit/audit-YYYYMMDD.jsonl（设计方案 6.4）。

每个写操作（审批 / 窗口指令 / 排队）成功后被记录一行 JSONL，前端不可绕过。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _audit_dir() -> Path:
    return DATA_DIR / "web-audit"


def write_audit(
    *,
    actor: str,
    action: str,
    wf_id: str,
    params: dict | None = None,
    result: str = "signaled",
) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": action,
        "wf_id": wf_id,
        "params": params or {},
        "result": result,
    }
    directory = _audit_dir()
    directory.mkdir(parents=True, exist_ok=True)  # 先建目录再写盘，避免静默失败
    path = directory / f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_audit(limit: int = 100) -> list[dict]:
    """读取最近操作审计（按文件倒序 + 行倒序），坏行跳过。"""
    directory = _audit_dir()
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("audit-*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(rows) >= limit:
                return rows
    return rows
