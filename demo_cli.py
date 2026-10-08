"""AIOps Agent 演示 CLI：启动流程 + 发送信号（设计方案 8.3）。

前置条件（两个终端）：
    终端 1: Temporal 服务端，例如  temporal server start-dev
            （或 Docker：docker run --rm -p 7233:7233 temporalio/temporal \
              server start-dev --ip 0.0.0.0）
    终端 2: export AIOPS_POLICY_PATH=./demo-policy.yaml   # 演示加速策略
            python -m aiops_agent.worker

用法示例：
    python demo_cli.py start --service order --alert-id a-1001
    python demo_cli.py status --wf-id aiops-fix-order-a-1001
    python demo_cli.py approve --wf-id aiops-fix-order-a-1001
    python demo_cli.py second-approve --wf-id aiops-fix-order-a-1001
    python demo_cli.py queue-patch --wf-id aiops-fix-order-a-1001 \
        --new-wf-id aiops-fix-order-a-1002 --service order --alert-id a-1002
    python demo_cli.py deploy-now --wf-id aiops-fix-order-a-1001    # 或 cancel
    python demo_cli.py result --wf-id aiops-fix-order-a-1001
    python demo_cli.py cleanup --dry-run                          # 数据清理预演（TTL）

演示分支（start/queue-patch 的 --description 关键词）：
    low-conf         闸门 1：置信度不足转人工
    protected        受保护目录：需二级审批
    test-fail        测试首次失败，回炉后通过
    test-always-fail 测试始终失败，重试耗尽转人工
    canary-bad       金丝雀劣化，自动回滚
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from aiops_agent import logs
from aiops_agent.config import load_policy, snapshot_for_workflow
from aiops_agent.models import Alert
from aiops_agent.workflows import AIOpsFixWorkflow

TASK_QUEUE = "aiops-tasks"
_ROOT = Path(__file__).resolve().parent


def _address() -> str:
    return os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")


def _wf_id(service: str, alert_id: str) -> str:
    return f"aiops-fix-{service}-{alert_id}"


async def _client() -> Client:
    return await Client.connect(_address())


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


async def cmd_start(args: argparse.Namespace) -> None:
    policy = load_policy()
    snapshot = snapshot_for_workflow(policy)
    # 策略一致性提示：workflow 快照与 worker 必须使用同一 policy，否则行为错配
    print(f"策略快照：{snapshot['policy_source']}（{policy.summary()}）")
    if "demo-policy" not in snapshot["policy_source"]:
        print("提示：当前未使用演示加速策略；演示请先 export AIOPS_POLICY_PATH=./demo-policy.yaml")
    alert = Alert(
        alert_id=args.alert_id,
        service=args.service,
        severity=args.severity,
        description=args.description or "",
    )
    client = await _client()
    wf_id = _wf_id(args.service, args.alert_id)
    handle = await client.start_workflow(
        AIOpsFixWorkflow.run,
        args=[alert, snapshot],
        id=wf_id,
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,  # 幂等防重：重复告警不重复开流程
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
    )
    print(f"已启动 workflow: {handle.id}")
    print(f"查询状态: python demo_cli.py status --wf-id {handle.id}")


async def cmd_approve(args: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle(args.wf_id)
    await handle.signal(AIOpsFixWorkflow.submit_approval, args.decision)
    print(f"已发送一级审批信号: {args.decision}")


async def cmd_second_approve(args: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle(args.wf_id)
    await handle.signal(AIOpsFixWorkflow.submit_second_approval, args.decision)
    print(f"已发送二级审批信号: {args.decision}")


async def cmd_deploy_now(args: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle(args.wf_id)
    await handle.signal(AIOpsFixWorkflow.submit_deploy_command, "deploy_now")
    print("已发送窗口指令: deploy_now（跳过剩余等待，立即部署）")


async def cmd_cancel(args: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle(args.wf_id)
    await handle.signal(AIOpsFixWorkflow.submit_deploy_command, "cancel")
    print("已发送窗口指令: cancel（队列保留，队首补丁将自动开启新周期）")


async def cmd_queue_patch(args: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle(args.wf_id)
    alert = Alert(
        alert_id=args.alert_id,
        service=args.service,
        description=args.description or "",
    )
    await handle.signal(AIOpsFixWorkflow.submit_patch, args=[args.new_wf_id, alert])
    print(f"已入队新补丁: {alert.alert_id}（未来 workflow id: {args.new_wf_id}）")


async def cmd_status(args: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle(args.wf_id)
    result = await handle.query(AIOpsFixWorkflow.status)
    print(_dump(result))


async def cmd_cleanup(args: argparse.Namespace) -> None:
    """数据清理（优化方案 3.6 / P5-03）：按 TTL 清理审计/沙箱/通知/Qoder/ArgoCD/流程记录。"""
    from aiops_agent import cleanup

    report = cleanup.run_cleanup(dry_run=args.dry_run)
    label = "（预演，未实际删除）" if report["dry_run"] else ""
    print(f"数据清理完成{label}：mode={report['mode']}")
    for key, value in report["deleted"].items():
        print(f"  - {key}: {value}")
    if report["errors"]:
        print("错误：")
        for message in report["errors"]:
            print(f"  - {message}")


async def cmd_result(args: argparse.Namespace) -> None:
    client = await _client()
    handle = client.get_workflow_handle(args.wf_id)
    print("等待工作流结束（若仍在等待审批或倒计时，可用 status 先查看当前阶段）...")
    result = await handle.result()
    print(_dump(result))


# ----------------------------------------------------------------------
# doctor：演示/运行环境一键自检
# ----------------------------------------------------------------------


def _tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001 - 自检只关心可达性
        return None


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    """仅判断 HTTP 是否可达（2xx/3xx 即视为可达，不解析 body）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def _check_temporal() -> tuple[bool, str]:
    address = _address()
    host, _, port = address.partition(":")
    try:
        port_num = int(port or "7233")
    except ValueError:
        return False, f"{address} 端口格式非法 → 使用 host:port 格式（如 localhost:7233）"
    if not _tcp_open(host, port_num):
        return False, f"{address} 不可达 → docker compose up -d temporal"
    return True, f"{address} 已连接"


def _check_loki(service: str) -> tuple[bool, str]:
    base = logs.DEFAULT_LOKI_URL
    if not _http_ok(f"{base.rstrip('/')}/ready"):
        return False, f"{base} 不可达 → docker compose up -d loki"
    try:
        lines = logs.query_lines(service, lookback_minutes=10, limit=5)
    except Exception as exc:  # noqa: BLE001
        return False, f"{base} 查询失败: {exc}"
    if not lines:
        return (
            False,
            f"{base} 可达，但 {service} 近 10 分钟无日志 → 演示前先运行 "
            f"demo_log_generator.py --service {service} --mode surge --count 800",
        )
    return True, f"{base} 可达，{service} 近 10 分钟日志 {len(lines)} 行（窗口内有数据）"


def _check_ollama() -> tuple[bool, str]:
    from aiops_agent import triage

    base = triage.DEFAULT_OLLAMA_URL
    data = _http_json(f"{base.rstrip('/')}/api/tags")
    if data is None:
        return False, f"{base} 不可达 → ollama serve（并 ollama pull qwen3.8:27b-mlx）"
    models = [m.get("name", "") for m in data.get("models", [])]
    wanted = os.environ.get("AIOPS_TRIAGE_MODEL", triage.DEFAULT_MODEL)
    hit = any(m == wanted or m.startswith(wanted.split(":")[0]) for m in models)
    if not hit:
        return False, f"{base} 可达，但未找到模型 {wanted}（已有 {models or '无'}）→ ollama pull {wanted}"
    return True, f"{base} 可达，模型 {wanted} 就绪"


def _check_docker() -> tuple[bool, str]:
    if not _tcp_open("127.0.0.1", 2375, timeout=1.0) and not Path("/var/run/docker.sock").exists():
        # colima / Docker Desktop 的 sock 也可能在 ~/.colima 下，退一步用 CLI 探测
        import shutil
        import subprocess

        if shutil.which("docker") is None:
            return False, "未找到 docker CLI → 安装 colima 或 Docker Desktop"
        try:
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5, check=True
            )
        except Exception:  # noqa: BLE001
            return False, "docker 守护进程未响应 → colima start（或启动 Docker Desktop）"
        return True, "docker 可用（沙箱测试 / 金丝雀发布）"
    return True, "docker 可用（沙箱测试 / 金丝雀发布）"


def _check_policy() -> tuple[bool, str]:
    try:
        policy = load_policy()
    except Exception as exc:  # noqa: BLE001
        return False, f"策略加载失败: {exc}"
    source = os.environ.get("AIOPS_POLICY_PATH", "release-gate-policy.yaml（默认）")
    note = "演示加速" if "demo-policy" in str(source) else "生产策略（演示建议 export AIOPS_POLICY_PATH=./demo-policy.yaml）"
    return True, f"{source} 已加载（{note}）：{policy.summary()}"


def _check_web_dist() -> tuple[bool, str]:
    index = _ROOT / "web" / "dist" / "index.html"
    if index.is_file():
        return True, "web/dist 已构建（BFF 可托管控制台）"
    return False, "web/dist 缺失 → cd web && npm install && npm run build"


def _check_bff() -> tuple[bool, str]:
    data = _http_json("http://127.0.0.1:8600/api/health", timeout=2.0)
    if data and data.get("ok"):
        temporal = data.get("temporal", {})
        state = "Temporal 已连" if temporal.get("connected") else "Temporal 未连"
        return True, f"控制台运行中（http://127.0.0.1:8600，{state}）"
    return False, "控制台未启动 → .venv/bin/uvicorn bff.app:app --host 127.0.0.1 --port 8600"


async def cmd_doctor(args: argparse.Namespace) -> None:
    service = args.service
    checks = [
        ("Temporal", _check_temporal()),
        ("Loki 日志", _check_loki(service)),
        ("Ollama 模型", _check_ollama()),
        ("Docker 沙箱", _check_docker()),
        ("策略文件", _check_policy()),
        ("前端构建", _check_web_dist()),
        ("BFF 控制台", _check_bff()),
    ]
    print("AIOps 环境自检：\n")
    failed = 0
    for name, (ok, message) in checks:
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name:<10} {message}")
        if not ok:
            failed += 1
    print()
    if failed:
        print(f"共 {failed} 项未就绪，按上方提示修复后重试。")
        sys.exit(1)
    print("全部就绪，可运行演示：bash scripts/demo-up.sh")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo_cli.py", description="AIOps Agent 演示 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="启动修复流程")
    p.add_argument("--service", required=True, help="服务名，如 order")
    p.add_argument("--alert-id", required=True, help="告警 ID，如 a-1001")
    p.add_argument("--severity", default="critical", help="严重级别（默认 critical）")
    p.add_argument("--description", default="", help="告警描述（可带演示关键词）")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("approve", help="一级审批：approve / reject")
    p.add_argument("--wf-id", required=True)
    p.add_argument("--decision", default="approve", choices=["approve", "reject"])
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("second-approve", help="二级审批（受保护目录）：approve / reject")
    p.add_argument("--wf-id", required=True)
    p.add_argument("--decision", default="approve", choices=["approve", "reject"])
    p.set_defaults(func=cmd_second_approve)

    p = sub.add_parser("deploy-now", help="窗口内加速：跳过剩余等待立即部署")
    p.add_argument("--wf-id", required=True)
    p.set_defaults(func=cmd_deploy_now)

    p = sub.add_parser("cancel", help="窗口内取消发布（排队补丁保留）")
    p.add_argument("--wf-id", required=True)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("queue-patch", help="窗口内新补丁入队（仅 FIFO，不重置倒计时）")
    p.add_argument("--wf-id", required=True, help="当前处于 NOTIFYING 的 workflow id")
    p.add_argument("--new-wf-id", required=True, help="入队补丁未来使用的 workflow id")
    p.add_argument("--service", required=True)
    p.add_argument("--alert-id", required=True)
    p.add_argument("--description", default="")
    p.set_defaults(func=cmd_queue_patch)

    p = sub.add_parser("status", help="查询流程状态")
    p.add_argument("--wf-id", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("result", help="等待并获取最终审计记录")
    p.add_argument("--wf-id", required=True)
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("doctor", help="环境自检：Temporal/Loki/Ollama/Docker/策略/前端/控制台")
    p.add_argument("--service", default="order", help="检查日志新鲜度的服务名（默认 order）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("cleanup", help="数据清理：按 TTL 清理审计/沙箱/通知/Qoder/ArgoCD/流程记录")
    p.add_argument("--dry-run", action="store_true", help="预演：只统计不删除")
    p.set_defaults(func=cmd_cleanup)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，给出运行提示
        print(f"执行失败: {exc}", file=sys.stderr)
        print(
            "提示：请确认 Temporal 服务端已启动（如 temporal server start-dev），"
            "并已运行 python -m aiops_agent.worker；地址可用 TEMPORAL_ADDRESS 指定。",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
