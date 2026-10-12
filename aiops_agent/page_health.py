"""页面健康判据（在线探针共用）：发现「端口活着但页面白屏」类故障。

被监控应用的「在线」判定分两层：连接层（有任何 HTTP 响应即在线的，由各调用方负责）
与页面层（本模块）。页面层两种模式：

- 精确关键字：条目配置了 ``probe_keyword``（如 ``<div id="root"``）→ 响应内容须包含该
  关键字（框架特定，可锁定具体挂载点）；
- 通用判据（未配置关键字时的默认）：不依赖任何具体框架与挂载点名称——
    1) 响应内容非空（抓 200 + 空响应/白屏）；
    2) 非 HTML（JSON/文本健康端点）：非空即通过；
    3) 疑似 HTML：须完整闭合（抓半截响应），且 ``<body>`` 段含可渲染容器
       （排除 script/style/noscript 等——抓「只剩入口脚本、挂载点缺失」的白屏；
       无标准 body 段时退化为全文判定）。

读取被截断（响应 ≥ 读取上限，内容量大显然非白屏）时跳过结构判定，避免误报。

能力边界：静态判据不执行 JavaScript，渲染级白屏（骨架完好但前端运行时崩溃）不在此列。

调用方：bff/aggregator.probe_monitored_app（控制台实时探测 + app_prober 巡检）、
aiops_agent/release._page_probe_once（直连发布探针）、check_aiops_interface.py（接入体检）。
"""

from __future__ import annotations

import re

_HTML_HINT_SAMPLE = 4096  # 疑似 HTML 的头部采样长度（DOCTYPE / <html 必在开头）
_HTML_HINT_RE = re.compile(r"<(?:html\b|!doctype\s+html)", re.IGNORECASE)
_HTML_CLOSE_RE = re.compile(r"</html\s*>", re.IGNORECASE)
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# 可渲染容器：排除纯元信息/脚本类标签后，出现任何元素即视为有渲染内容
_VISIBLE_TAG_RE = re.compile(
    r"<(?![/]?(?:script|style|link|meta|noscript|template|title|base|head|html|body)\b)"
    r"[/]?[A-Za-z][A-Za-z0-9:.-]*",
    re.IGNORECASE,
)


def page_problem(body: str, keyword: str = "", *, truncated: bool = False) -> str:
    """页面判据：返回问题描述（人类可读）；健康时返回空串。

    ``keyword`` 非空 → 精确包含（配置者显式锁定，如 ``<div id="root"``）；
    ``keyword`` 为空 → 通用白屏判据（不依赖具体挂载点名称）。
    ``truncated`` → 响应已达到读取上限（内容量大），跳过 HTML 结构判定（保守放行）。
    """
    check = (keyword or "").strip()
    if check:
        if check in body:
            return ""
        return f"响应内容缺少关键字 {check!r}（页面可能白屏/异常）"

    text = (body or "").strip()
    if not text:
        return "响应内容为空（疑似页面白屏/异常）"
    if not _HTML_HINT_RE.search(text[:_HTML_HINT_SAMPLE]):
        return ""  # 非 HTML（JSON/文本健康端点）：非空即通过
    if truncated:
        return ""
    if not _HTML_CLOSE_RE.search(text):
        return "页面 HTML 未完整闭合（疑似半截响应）"
    match = _BODY_RE.search(text)
    scope = match.group(1) if match else text  # 无标准 body 段时退化为全文判定
    stripped = _COMMENT_RE.sub("", scope).strip()
    if not stripped:
        return "页面 body 为空（疑似白屏）"
    if not _VISIBLE_TAG_RE.search(stripped):
        return "页面 body 无可见内容（疑似挂载点缺失/白屏）"
    return ""
