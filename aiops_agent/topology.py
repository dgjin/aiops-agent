"""服务依赖拓扑（P2-2 浅拓扑）：为 triage 注入「依赖关系 + 依赖健康」上下文。

设计（对齐评估报告 P2-2「浅拓扑图：被监控应用依赖关系 | 根因定位精度」）：
- 数据源：被监控应用清单的 ``depends_on`` 字段（demo=JSON 文件 / production=MySQL，
  与 app_registry 同一份、同样热生效）+ app_prober 落盘的巡检状态
  （``data/probe_status.json``，缺失/损坏时仅省略健康信息）；
- 语义：``depends_on`` = 本服务依赖的服务（上游提供者）；反向索引得到
  「依赖本服务的服务」（下游调用方）；服务名以清单 ``service`` 字段为准；
- 注入形态（``dependency_context``）：本服务依赖列表（附探测健康）、依赖本服务的
  列表、以及不健康的依赖高亮——供 LLM 判断根因在本服务 / 上游依赖 / 下游调用方；
- 无依赖且无被依赖 → 返回 ``None``（不注入、prompt 保持稳定，零行为变更）；
- 任何读取异常 → ``None``（拓扑是增强信息，绝不阻断 triage）。
"""

from __future__ import annotations

import json
import logging

from . import app_registry, mode

log = logging.getLogger("aiops.topology")

PROBE_STATUS_FILE = "probe_status.json"


def _probe_states() -> dict:
    """probe_status.json 的 ``apps`` 映射（按 app_id 索引；缺失/损坏 → {}）。"""
    try:
        payload = json.loads(
            (mode.data_root() / PROBE_STATUS_FILE).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    apps = payload.get("apps") if isinstance(payload, dict) else None
    return apps if isinstance(apps, dict) else {}


def _service_index(apps: list[dict]) -> dict[str, dict]:
    """service → 清单条目（同名取首条；空 service 忽略）。"""
    index: dict[str, dict] = {}
    for app in apps:
        service = str(app.get("service") or "").strip()
        if service and service not in index:
            index[service] = app
    return index


def dependency_context(service: str) -> dict | None:
    """给定服务 → triage 拓扑上下文；无任何依赖关系时返回 None。

    返回结构::

        {
          "service": "order",
          "depends_on":  [{"service": "coupon", "probe": {"ok": false, "failures": 62, "last_error": "..."} | None}, ...],
          "depended_by": [{"service": "gateway", "probe": {...} | None}, ...],
          "unhealthy":   ["coupon", ...]   # 依赖中探测明确不健康者（根因候选）
        }
    """
    wanted = (service or "").strip()
    if not wanted:
        return None
    try:
        apps = app_registry.load_apps()
    except Exception as exc:  # noqa: BLE001 - 拓扑读取失败不阻断 triage
        log.warning("[topology] 清单读取失败，跳过拓扑注入: %s", exc)
        return None

    index = _service_index(apps)
    raw_depends = index.get(wanted, {}).get("depends_on")
    depends_on = (
        [str(item).strip() for item in raw_depends if isinstance(item, str) and str(item).strip()]
        if isinstance(raw_depends, list)
        else []
    )
    depended_by = [
        svc
        for svc, app in index.items()
        if isinstance(app.get("depends_on"), list) and wanted in app["depends_on"]
    ]
    if not depends_on and not depended_by:
        return None

    probe = _probe_states()

    def with_health(svc: str) -> dict:
        item = index.get(svc)
        record = probe.get(item["id"]) if item else None
        health: dict | None = None
        if isinstance(record, dict):
            health = {
                "ok": record.get("ok") is True,
                "failures": int(record.get("failures") or 0),
                "last_error": str(record.get("last_error") or "")[:120],
            }
        return {"service": svc, "probe": health}

    deps = [with_health(svc) for svc in depends_on]
    return {
        "service": wanted,
        "depends_on": deps,
        "depended_by": [with_health(svc) for svc in depended_by],
        "unhealthy": [d["service"] for d in deps if (d["probe"] or {}).get("ok") is False],
    }
