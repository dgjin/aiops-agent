"""一次性验证脚本（随证据包归档）：直连 Temporal 查询现有需求分析会话的真实发布状态。

对照 data/requirement_analyses.json 的 approved 会话 → gw.release_status(wf_id)，
检查「已发布成功（released）」判定在真实工作流数据上的行为。

用法：cd <仓库根> && .venv/bin/python docs/需求发布状态控制-2026-10-11/check-release-status.py
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 本文件位于 <仓库根>/docs/需求发布状态控制-2026-10-11/
sys.path.insert(0, str(ROOT))

from bff.temporal_gateway import TemporalGateway  # noqa: E402

DATA = ROOT / "data" / "requirement_analyses.json"


async def main() -> None:
    gw = TemporalGateway()
    entries = json.loads(DATA.read_text(encoding="utf-8"))
    approved = [
        (e["id"], (e.get("approved") or {}).get("wf_id") or "")
        for e in entries
        if e.get("status") == "approved"
    ]
    print(f"approved 会话：{len(approved)} 条\n")
    cache: dict[str, str] = {}
    for analysis_id, wf_id in approved:
        if wf_id not in cache:
            try:
                cache[wf_id] = await gw.release_status(wf_id)
            except Exception as exc:  # noqa: BLE001
                cache[wf_id] = f"ERR:{exc}"
        print(f"{analysis_id:26s} {wf_id:42s} -> {cache[wf_id]}")
    print()
    dist: dict[str, int] = {}
    for status in cache.values():
        dist[status] = dist.get(status, 0) + 1
    print("发布状态分布：", dist)


asyncio.run(main())
