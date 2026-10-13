"""AIOps MCP server：把控制台能力以 MCP 工具形态对外暴露（P4⑬，市场对标 P2-4）。

定位：**薄 HTTP 适配层**——每个工具调用转发到控制台 BFF 的 ``/api/*``，复用其
集中式鉴权（Bearer 令牌 → viewer/operator/admin）、操作审计、写守卫
（``_write_guard``：运行中 + 阶段允许 + 防重复）、kill switch 与限速；MCP 层
不做任何业务重复实现，行为与控制台 / ChatOps 完全一致（同一 BFF 端点、同一守卫）。

工具（9 个）：
    只读（viewer 令牌即可）：
        ops_overview            运营总览（计数 + 运行中 + 终态分布 + 运营指标）
        list_flows              流程列表（status/service/stage 过滤）
        get_flow                流程详情（含结果与制品）
        list_pending_approvals  待审批队列（一级 + 二级，按 deadline 排序）
        list_escalations        转人工待办（SLA / 再升级标记）
        query_audit             审计查询（终态流程 + 操作留痕）
        check_system_health     依赖健康（Temporal / 修复链路依赖降级）
    写（需 operator 及以上令牌；BFF 侧仍有写守卫与审计）：
        approve_flow            一级审批（approve / reject）
        second_approve_flow     二级审批（受保护目录流程）

错误呈现：BFF 失败以 MCP ``is_error`` 结果返回，内容含原始原因（HTTP 状态码 + BFF
错误消息），如「Error executing tool approve_flow: BFF 409: 流程已结束（COMPLETED）…」；
网络不可达显示连接错误原因。本地 BFF 调用显式绕过系统代理（HTTP_PROXY 干扰实测可见）。

配置（环境变量；未设置 AIOPS_MCP_TOKEN 时调用会在 BFF 侧 401，错误信息会提示）：
    AIOPS_MCP_BASE_URL  BFF 地址，默认 http://127.0.0.1:8600
    AIOPS_MCP_TOKEN     控制台令牌（会话令牌或静态令牌，透传给 BFF）

运行（工程根目录）：
    .venv/bin/python mcp_server.py                 # stdio（MCP 客户端本地拉起，主模式）
    .venv/bin/python mcp_server.py --http          # streamable-http（仅 demo，127.0.0.1:8700/mcp）

传输安全门禁：production 一律禁用 ``--http``（仅 stdio；HTTP 无入站调用者映射，
不构成多用户授权服务）；demo 的 ``--http`` 也仅允许 loopback 监听（拒绝 0.0.0.0 等）。

依赖：mcp>=2.3（见 requirements-mcp.txt，可选组件不拖累主链路；本模块顶层
不 import mcp，未安装时仅 ``build_server`` 不可用，工具函数仍可单测）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

# 触发 .env 自动加载（与 worker / BFF / cleanup 一致的模式）；依赖缺失时静默跳过
try:  # pragma: no cover - 取决于 aiops_agent 包可导入性
    from aiops_agent import config as _config  # noqa: F401
    from aiops_agent import mode as runtime_mode
except ImportError:  # pragma: no cover
    runtime_mode = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "http://127.0.0.1:8600"
DEFAULT_HTTP_PORT = 8700

# 全部工具名（注册清单与测试、文档共用的单一事实源）
TOOL_NAMES: tuple[str, ...] = (
    "ops_overview",
    "list_flows",
    "get_flow",
    "list_pending_approvals",
    "list_escalations",
    "query_audit",
    "check_system_health",
    "approve_flow",
    "second_approve_flow",
)


class BffError(RuntimeError):
    """BFF 调用失败：携带 HTTP 状态码（0 = 网络不可达），供 MCP 端呈现可诊断错误。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"BFF {status}: {message}" if status else message)
        self.status = status
        self.message = message


class BffClient:
    """迷你 HTTP 客户端（纯 stdlib）：统一鉴权头 / 错误转译 / 信封剥离。

    仅依赖 urllib，保证在未安装 mcp 包的环境里仍可独立单测。
    """

    def __init__(
        self, base_url: str | None = None, token: str | None = None, timeout: float = 15.0
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("AIOPS_MCP_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.token = token if token is not None else os.environ.get("AIOPS_MCP_TOKEN", "")
        self.timeout = timeout
        # 显式关闭代理：本地/内网 BFF 不应受系统代理（HTTP_PROXY / macOS 网络设置）影响
        # ——否则 127.0.0.1 请求可能被代理吞掉返回 502 等误导性错误（本机实测暴露）。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict | None = None
    ) -> dict:
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = self.base_url + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err_payload = json.loads(exc.read() or b"{}")
                if isinstance(err_payload, dict):
                    detail = str(err_payload.get("error") or err_payload.get("detail") or "")
            except (json.JSONDecodeError, ValueError):
                detail = ""
            if exc.code == 401 and not detail:
                detail = "鉴权失败（检查 AIOPS_MCP_TOKEN 是否为有效控制台令牌）"
            raise BffError(exc.code, detail or f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise BffError(0, f"无法连接控制台 BFF（{self.base_url}）：{exc.reason}") from exc
        if isinstance(payload, dict):
            # 响应生成时间戳：对 MCP 调用方无语义，剥离以节省 token
            payload.pop("server_time", None)
        return payload

    def get(self, path: str, **params: Any) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body=body)


# ----------------------------------------------------------------------
# 工具实现（BffClient 注入，无 mcp 依赖 → 可独立单测）
# ----------------------------------------------------------------------


def tool_ops_overview(client: BffClient, days: int = 7) -> dict:
    """运营总览：counts + running + 近 7 日终态分布 + 运营指标（懒合并降级）。"""
    overview = client.get("/api/overview")
    try:
        ops = client.get("/api/metrics/ops", days=days)
    except BffError as exc:  # 运营指标不可得时降级：总览字段仍可用
        ops = {"error": str(exc)}
    return {**overview, "ops_metrics": ops}


def tool_list_flows(
    client: BffClient, status: str = "all", service: str = "", stage: str = "", limit: int = 100
) -> dict:
    return client.get("/api/flows", status=status, service=service, stage=stage, limit=limit)


def tool_get_flow(client: BffClient, wf_id: str) -> dict:
    return client.get(f"/api/flows/{urllib.parse.quote(wf_id, safe='')}")


def tool_list_pending_approvals(client: BffClient) -> dict:
    return client.get("/api/approvals")


def tool_list_escalations(client: BffClient, status: str = "all") -> dict:
    return client.get("/api/escalations", status=status)


def tool_query_audit(
    client: BffClient, q: str = "", stage: str = "", days: int = 30, limit: int = 50
) -> dict:
    return client.get("/api/audit", q=q, stage=stage, days=days, limit=limit)


def tool_check_system_health(client: BffClient) -> dict:
    health = client.get("/api/health")
    subsystems = client.get("/api/subsystems")
    return {**health, **subsystems}


def tool_approve_flow(client: BffClient, wf_id: str, decision: str) -> dict:
    return client.post(f"/api/flows/{urllib.parse.quote(wf_id, safe='')}/approval", {"decision": decision})


def tool_second_approve_flow(client: BffClient, wf_id: str, decision: str) -> dict:
    return client.post(
        f"/api/flows/{urllib.parse.quote(wf_id, safe='')}/second-approval", {"decision": decision}
    )


# ----------------------------------------------------------------------
# MCP 注册（惰性导入 mcp：可选组件）
# ----------------------------------------------------------------------


def build_server(client: BffClient | None = None) -> Any:
    """构建 MCPServer 并注册全部工具（mcp 未安装时抛 SystemExit 带安装提示）。

    错误呈现：工具抛出的 ``BffError`` 在注册层升级为双身份异常 ``BffToolError``
    （BffError + ToolError 多重继承）——对工具层仍是 BffError（如 ops_overview 的
    指标降级分支可捕获），对 MCP 层是 ToolError（客户端收到 is_error=True 且
    content 含可读原因，而非被 SDK 吞成 "Error executing tool …"），原因随结果到达模型。
    """
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as exc:  # pragma: no cover - 取决于本地是否安装 mcp
        raise SystemExit(
            "缺少 mcp 依赖：pip install -r requirements-mcp.txt（或 pip install 'mcp>=2.3,<3'）"
        ) from exc

    class BffToolError(BffError, ToolError):
        """双身份错误：BffError（工具层降级可捕获）+ ToolError（MCP is_error 携带原因）。"""

    class _TranslatedClient(BffClient):
        """注册层客户端：BFF 错误升级为双身份异常（其余行为完全继承父类）。"""

        def request(
            self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict | None = None
        ) -> dict:
            try:
                return super().request(method, path, params=params, body=body)
            except BffError as exc:
                raise BffToolError(exc.status, exc.message) from exc

    base = client or BffClient()
    bff = _TranslatedClient(base_url=base.base_url, token=base.token, timeout=base.timeout)
    server = MCPServer(
        name="aiops-agent",
        title="AIOps 自动运维智能体",
        description="自主修复闭环控制台的能力出口：流程查询、人工审批、待办与健康自检。",
        instructions=(
            "面向外部 Agent 的 AIOps 能力出口。查询类工具可直接调用；审批类工具为写操作，"
            "需 operator 及以上权限令牌，且经控制台写守卫（运行中 + 阶段允许 + 防重复）与操作审计。"
            "审批前建议先调用 list_pending_approvals 核对流程阶段与截止时间，"
            "并用 get_flow 查看细节后再执行 approve/reject。"
        ),
        version="1.0.0",
    )

    @server.tool(
        name="ops_overview",
        title="运营总览",
        description=(
            "只读。运营总览：流程计数（运行中/待审批/公告中/今日完成/转人工/硬失败）、"
            "运行中流程列表、近 7 日终态分布，以及运营指标（MTTR、自动修复率、"
            "人工干预率、闸门拦截率）。days 为运营指标统计窗口（1-30，默认 7）。"
        ),
    )
    def ops_overview(days: int = 7) -> dict:
        return tool_ops_overview(bff, days=days)

    @server.tool(
        name="list_flows",
        title="流程列表",
        description=(
            "只读。查询运维流程列表。status 过滤：all/open/closed；"
            "service 按被监控服务名精确过滤（缺省不过滤）；stage 按阶段名过滤"
            "（如 WAIT_APPROVAL/NOTIFYING/DONE）；limit 上限 500，默认 100。"
        ),
    )
    def list_flows(status: str = "all", service: str = "", stage: str = "", limit: int = 100) -> dict:
        return tool_list_flows(bff, status=status, service=service, stage=stage, limit=limit)

    @server.tool(
        name="get_flow",
        title="流程详情",
        description=(
            "只读。查询单个流程的完整详情（阶段、审批状态、结果、制品）。"
            "wf_id 需为完整流程 ID（形如 aiops-fix-<service>-<alert>-<ts>）。"
        ),
    )
    def get_flow(wf_id: str) -> dict:
        return tool_get_flow(bff, wf_id=wf_id)

    @server.tool(
        name="list_pending_approvals",
        title="待审批队列",
        description=(
            "只读。待审批队列：一级审批（三闸门之人工审批）与受保护目录流程的二级审批，"
            "按截止时间升序排列。返回项含 pending 分类与 deadline。"
        ),
    )
    def list_pending_approvals() -> dict:
        return tool_list_pending_approvals(bff)

    @server.tool(
        name="list_escalations",
        title="转人工待办",
        description=(
            "只读。转人工待办列表（低置信度升级 / 修复失败 / 硬失败）——"
            "status 过滤 all/open/assigned/closed；含责任人、SLA 与超时再升级标记。"
        ),
    )
    def list_escalations(status: str = "all") -> dict:
        return tool_list_escalations(bff, status=status)

    @server.tool(
        name="query_audit",
        title="审计查询",
        description=(
            "只读。审计查询：近 days 天终态流程（q 关键词匹配流程/服务/补丁 ID，"
            "stage 过滤终态类型）与最近操作留痕（操作者、动作、结果）。"
            "days 范围 1-365 默认 30；limit 上限 500 默认 50。"
        ),
    )
    def query_audit(q: str = "", stage: str = "", days: int = 30, limit: int = 50) -> dict:
        return tool_query_audit(bff, q=q, stage=stage, days=days, limit=limit)

    @server.tool(
        name="check_system_health",
        title="系统健康自检",
        description=(
            "只读。系统依赖健康自检：Temporal 连接与延迟 + 修复链路依赖"
            "（Loki/Ollama/沙箱等）就绪度；返回 degraded 清单与 degraded_mode 标志。"
        ),
    )
    def check_system_health() -> dict:
        return tool_check_system_health(bff)

    @server.tool(
        name="approve_flow",
        title="一级审批",
        description=(
            "写操作（需 operator 及以上令牌）。对 WAIT_APPROVAL 阶段流程提交一级审批："
            "decision=approve 批准进入公告倒计时，decision=reject 驳回终止。"
            "经 BFF 写守卫（运行中 + 阶段允许 + 防重复提交）与操作审计，失败原因见错误信息。"
        ),
    )
    def approve_flow(wf_id: str, decision: Literal["approve", "reject"] = "approve") -> dict:
        return tool_approve_flow(bff, wf_id=wf_id, decision=decision)

    @server.tool(
        name="second_approve_flow",
        title="二级审批",
        description=(
            "写操作（需 operator 及以上令牌）。受保护目录（auth/payment/crypto）流程的二级审批。"
            "前提：一级审批已 approve 且二级尚未提交；decision=approve|reject。"
            "经 BFF 写守卫与操作审计。"
        ),
    )
    def second_approve_flow(wf_id: str, decision: Literal["approve", "reject"] = "approve") -> dict:
        return tool_second_approve_flow(bff, wf_id=wf_id, decision=decision)

    return server


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp_server.py", description="AIOps MCP server（控制台能力 → MCP 工具）"
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help=f"以 streamable-http 传输运行（默认 stdio；HTTP 默认 127.0.0.1:{DEFAULT_HTTP_PORT}/mcp）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="--http 监听地址（默认 127.0.0.1）")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_HTTP_PORT, help=f"--http 监听端口（默认 {DEFAULT_HTTP_PORT}）"
    )
    parser.add_argument("--base-url", default="", help="覆盖 AIOPS_MCP_BASE_URL（默认 http://127.0.0.1:8600）")
    return parser


def _enforce_transport_policy(args: argparse.Namespace) -> None:
    """传输安全门禁（启动监听前调用）：production 仅 stdio；HTTP 仅 demo + loopback。"""
    if not args.http:
        return
    if runtime_mode is None:  # pragma: no cover - 仓库内不可达
        raise SystemExit("无法加载 aiops_agent.mode，拒绝启用 MCP HTTP 传输")
    if runtime_mode.is_production():
        raise SystemExit(
            "MCP HTTP 传输在 production 禁用（未接入多用户授权网关，仅 stdio）；"
            "如需本地调试请显式设置 AIOPS_MODE=demo"
        )
    host = (args.host or "").strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit(f"MCP HTTP 仅允许 loopback 监听（收到 {host or '空值'}）")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    _enforce_transport_policy(args)
    client = BffClient(base_url=args.base_url or None)
    server = build_server(client)
    transport = "streamable-http" if args.http else "stdio"
    # stdio 模式下 stdout 是协议通道：启动信息只能写 stderr
    print(
        f"[aiops-mcp] transport={transport} bff={client.base_url} "
        f"token={'已配置' if client.token else '未配置（调用将被 BFF 401 拒绝）'}",
        file=sys.stderr,
    )
    if args.http:
        server.run(transport=transport, host=args.host, port=args.port)
    else:
        server.run(transport=transport)


if __name__ == "__main__":
    main()
