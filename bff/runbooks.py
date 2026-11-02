"""值班 Runbook 读取（M5）：单源 ``docs/AIOps 值班 Runbook.md``，按告警 code 取小节。

``ops_metrics`` 的 ``duty_alerts[].runbook`` 是「文件路径#锚点」；控制台走 Bearer
鉴权，浏览器无法直接打开仓库文件，因此由本模块按 code 取回对应 markdown 小节，
前端在抽屉里渲染——Runbook 与告警 code 永远同源，不额外维护一份副本。

code 仅允许 ``[a-z][a-z0-9-]{1,48}``（不参与路径拼接，仅用于标题匹配）。
"""

from __future__ import annotations

import re
from pathlib import Path

RUNBOOK_RELATIVE = "docs/AIOps 值班 Runbook.md"
_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,48}$")
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*\S)\s*$")


def runbook_path() -> Path:
    """仓库内 Runbook 绝对路径（工作目录无关）。"""
    return Path(__file__).resolve().parent.parent / RUNBOOK_RELATIVE


def section(code: str) -> dict:
    """返回 ``{code, title, body, path, known}``；未知 code / 文件缺失 → known=False。"""
    code = (code or "").strip()
    payload = {"code": code, "title": "", "body": "", "path": RUNBOOK_RELATIVE, "known": False}
    if not _CODE_RE.match(code):
        return payload
    try:
        lines = runbook_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return payload

    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = match.group(2).strip()
        if title.split()[0].strip("`：:") == code:
            start, level = index, len(match.group(1))
            break
    if start is None:
        return payload

    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = _HEADING_RE.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    body = "\n".join(lines[start + 1 : end]).strip("\n")
    payload.update({"title": lines[start].lstrip("#").strip(), "body": body, "known": bool(body)})
    return payload
