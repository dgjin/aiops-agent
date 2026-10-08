"""需求基线条目客户端（被监控系统标准导出接口 v1.0）。

用户在系统内提交的需求 / 建议 / 缺陷经管理员评估分析、纳入「需求基线」后，
被监控系统经 ``GET /api/requirements/export`` 开放给 AIOps 做主动需求分析
（优先级研判 / 实现建议 / 排期参考）。接口发现方式两种：

- 服务清单 ``GET /.well-known/aiops.json`` 的 ``requirements_path`` 字段（推荐，见 app_manifest.py）；
- 能力自描述 ``GET /.well-known/requirements.json``（``export_path`` / ``auth`` / ``query``）。

契约要点（v1.0，全部 UTF-8 JSON；响应**无 success 字段**，以 HTTP 状态码为准）：

================  ==========================================================
字段              说明
================  ==========================================================
spec_version      接口规范版本（如 "1.0"）；主版本不符仅告警
service           服务标识（与告警 / 日志 / 清单中的 ``service`` 一致，如 nl2sql）
system            系统名称（人类可读）
exportedAt        导出时点（ISO 8601，可作下次增量轮询的 since，``>=`` 语义）
filter            服务端确认的过滤条件回显
returned          本次返回条数（与 ``entries`` 长度一致）
entries           条目数组：id / kind / title / content / status / priority /
                  baselineVersion / assessment / submitter / department /
                  reviewer / reviewedAt / createdAt / updatedAt
================  ==========================================================

认证：``Authorization: Bearer <OPS_API_TOKEN>``（令牌值与被监控系统 ``OPS_API_TOKEN``
一致）；经参数或环境变量 ``NL2SQL_OPS_TOKEN`` 注入。无凭据 / 令牌错误 → HTTP 401；
``status`` / ``kind`` / ``since`` 非法 → 400（错误体 ``{error: 中文说明}``）。

本模块只做「拉取 + 校验」：不落盘、不引入第三方依赖（标准库 urllib 实现），
拉取失败是常态（含超时 / 非 JSON / 结构不符 / 401），
:func:`fetch_requirements` **绝不抛异常**。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

REQUIREMENTS_PATH = "/api/requirements/export"
SPEC_VERSION = "1.0"
TOKEN_ENV = "NL2SQL_OPS_TOKEN"
DEFAULT_TIMEOUT = 5.0
# 数据接口而非探测接口：limit 上限 500 条 × 单条正文上限 5000 字符（UTF-8 最坏约 ×3）
# ≈ 10MB，取 16MB 上限防（错误配置的）大响应拖垮调用方
MAX_BODY = 16 * 1024 * 1024


def export_url(
    base_url: str,
    *,
    status: str = "BASELINED",
    kind: str | None = None,
    since: str | None = None,
    limit: int | None = 200,
) -> str:
    """由应用根地址推导导出地址（容忍带 / 不带尾斜杠），并拼装查询参数。

    值统一做百分号编码（空格 → ``%20``），避免以 ``+`` 传达
    在与部分网关组合时被二次解码为空格。
    """
    params = {"status": (status or "BASELINED").strip().upper()}
    if kind:
        params["kind"] = str(kind).strip().upper()
    if since:
        params["since"] = str(since).strip()
    if limit:
        try:
            params["limit"] = str(int(limit))
        except (TypeError, ValueError):
            pass  # 非法 limit 交由服务端默认值处理（缺省 200）
    query = urlencode(params, quote_via=quote)
    return (base_url or "").strip().rstrip("/") + REQUIREMENTS_PATH + "?" + query


def _resolve_token(token: str | None) -> str:
    """令牌解析：参数优先，回退环境变量（与被监控系统 OPS_API_TOKEN 配套）。"""
    if token and token.strip():
        return token.strip()
    return (os.environ.get(TOKEN_ENV) or "").strip()


def _read_error_detail(exc: urllib.error.HTTPError) -> str:
    """尽力读取错误响应体中的说明（``{error: 中文说明}``），失败返回空串。"""
    try:
        payload = json.loads(exc.read(4096).decode("utf-8"))
    except Exception:  # noqa: BLE001 - 错误体读取失败不影响主流程
        return ""
    if isinstance(payload, dict):
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def fetch_requirements(
    base_url: str,
    *,
    status: str = "BASELINED",
    kind: str | None = None,
    since: str | None = None,
    limit: int | None = 200,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """拉取需求基线条目，**绝不抛异常**（拉取失败是常态）。

    返回 ``{"url", "ok", "error", "warnings", "spec_version", "service",
    "system", "exported_at", "filter", "returned", "entries"}``：
    ``ok=True`` 时 ``entries`` 为条目数组（``returned`` 为服务端报告的条数）；
    失败时 ``entries`` 为空数组、``error`` 为中文原因。
    """
    url = export_url(base_url, status=status, kind=kind, since=since, limit=limit)
    result: dict = {
        "url": url,
        "ok": False,
        "error": "",
        "warnings": [],
        "spec_version": "",
        "service": "",
        "system": "",
        "exported_at": "",
        "filter": {},
        "returned": 0,
        "entries": [],
    }
    headers = {"User-Agent": "aiops-agent/0.1", "Accept": "application/json"}
    resolved = _resolve_token(token)
    if resolved:
        headers["Authorization"] = f"Bearer {resolved}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            result["error"] = (
                "鉴权失败：应用返回 HTTP 401（请确认被监控系统已配置 OPS_API_TOKEN，"
                "并经 --token 或环境变量 NL2SQL_OPS_TOKEN 提供同一令牌）"
            )
            return result
        detail = _read_error_detail(exc)
        result["error"] = f"应用返回 HTTP {exc.code}" + (f"：{detail}" if detail else "")
        return result
    except Exception as exc:  # noqa: BLE001 - 超时/拒连/DNS 等一律转拉取失败
        result["error"] = f"无法访问需求基线条目接口：{exc}"
        return result

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        result["error"] = f"接口响应不是合法 JSON：{exc}"
        return result
    if not isinstance(payload, dict):
        result["error"] = "接口响应不是 JSON 对象"
        return result

    version = payload.get("spec_version")
    if not isinstance(version, str) or not version.strip():
        result["error"] = "响应缺少 spec_version（该地址可能不是需求基线条目接口）"
        return result
    if version.strip().split(".", 1)[0] != SPEC_VERSION.split(".", 1)[0]:
        result["warnings"].append(
            f"spec_version={version.strip()!r} 与当前支持 {SPEC_VERSION} 主版本不同，按尽力兼容处理"
        )

    entries = payload.get("entries")
    if not isinstance(entries, list):
        result["error"] = "响应缺少 entries 数组（该地址可能不是需求基线条目接口）"
        return result

    result["spec_version"] = version.strip()
    result["service"] = str(payload.get("service") or "")
    result["system"] = str(payload.get("system") or "")
    result["exported_at"] = str(payload.get("exportedAt") or "")
    filter_ = payload.get("filter")
    result["filter"] = filter_ if isinstance(filter_, dict) else {}
    result["entries"] = [item for item in entries if isinstance(item, dict)]
    returned = payload.get("returned")
    result["returned"] = returned if isinstance(returned, int) else len(result["entries"])
    result["ok"] = True
    return result
