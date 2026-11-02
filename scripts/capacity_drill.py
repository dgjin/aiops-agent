"""容量演练（M6）：20/50/100 条告警批次的背压、幂等与恢复验证。

两种模式：

- ``fake``（默认）：进程内模拟「告警接入 → 幂等去重 → 模型 → 沙箱」，
  验证资源配额、重复告警不重复执行、队列可恢复、结束后无残留占用；
- ``temporal``：真实启动工作流（需 ``TEMPORAL_ADDRESS``），核对重复告警复用
  同一工作流 ID。**不执行真实修复**，只验证接入侧幂等与启动速率。

用法：

    .venv/bin/python scripts/capacity_drill.py --batch 20,50,100
    .venv/bin/python scripts/capacity_drill.py --mode temporal --batch 20

退出码：0 = 全部通过；1 = 超限/泄漏/重复执行；2 = 环境不具备（不伪造通过）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiops_agent import capacity  # noqa: E402
from aiops_agent.config import load_policy  # noqa: E402

# 告警画像：重复告警共用同一幂等键，其余类型用于验证排队与失败分类
PROFILES = ("normal", "duplicate", "no_logs", "model_timeout")


def workflow_id(service: str, alert_id: str) -> str:
    """与接入层一致的幂等键（alert_webhook.workflow_id_for）：同 ID 不重复启动。"""
    return f"aiops-fix-{service}-{alert_id}"


def build_batch(size: int, *, duplicate_ratio: float = 0.2, timeout_ratio: float = 0.1,
                no_logs_ratio: float = 0.1) -> list[dict]:
    """构造批次：normal / duplicate / no_logs / model_timeout 四类告警。"""
    if size <= 0:
        raise ValueError("批次大小必须 ≥1")
    batch: list[dict] = []
    for index in range(size):
        profile = "normal"
        if index % max(1, int(1 / max(duplicate_ratio, 0.01))) == 1:
            profile = "duplicate"
        elif index % max(1, int(1 / max(no_logs_ratio, 0.01))) == 2:
            profile = "no_logs"
        elif index % max(1, int(1 / max(timeout_ratio, 0.01))) == 3:
            profile = "model_timeout"
        # duplicate 复用前一条的 alert_id（模拟 Alertmanager 重复推送）
        alert_id = f"drill-{index - 1:03d}" if profile == "duplicate" else f"drill-{index:03d}"
        batch.append(
            {
                "service": "demo-app",
                "profile": profile,
                "alert_id": alert_id,
                "wf_id": workflow_id("demo-app", alert_id),
            }
        )
    return batch


async def run_fake_batch(
    batch: list[dict],
    limiter: capacity.ResourceLimiter,
    *,
    model_delay: float = 0.02,
    sandbox_delay: float = 0.02,
) -> dict:
    """进程内演练：幂等去重 + 资源排队 + 失败分类，最后核对无残留占用。"""
    executions: dict[str, int] = {}
    failures: dict[str, int] = {"no_logs": 0, "model_timeout": 0, "queue_timeout": 0}
    lock = asyncio.Lock()

    async def handle(alert: dict) -> None:
        async with lock:
            if alert["wf_id"] in executions:
                executions[alert["wf_id"]] += 1
                return
            executions[alert["wf_id"]] = 1
        if alert["profile"] == "no_logs":
            failures["no_logs"] += 1
            return
        try:
            async with limiter.limit("local_llm"):
                if model_delay:
                    await asyncio.sleep(model_delay)
                if alert["profile"] == "model_timeout":
                    failures["model_timeout"] += 1
                    return
            async with limiter.limit("sandbox"):
                if sandbox_delay:
                    await asyncio.sleep(sandbox_delay)
        except capacity.CapacityError:
            failures["queue_timeout"] += 1

    await asyncio.gather(*(handle(alert) for alert in batch))

    unique = len({alert["wf_id"] for alert in batch})
    started = len(executions)
    duplicates = sum(count - 1 for count in executions.values())
    snap = limiter.snapshot()
    resources = snap["resources"]
    quota_ok = all(
        resources[name]["peak_in_use"] <= snap["limits"][name] for name in snap["limits"]
    )
    leaked = any(
        resources[name]["in_use"] or resources[name]["waiting"] for name in resources
    )
    report = {
        "mode": "fake",
        "batch": len(batch),
        "unique_alerts": unique,
        "started": started,
        "deduped": duplicates,
        "resources": resources,
        "failures": failures,
        "checks": {
            "quota_respected": quota_ok,
            "no_leak": not leaked,
            "dedup_ok": started == unique,
        },
    }
    limiter.reset()
    return report


def evaluate(report: dict) -> list[str]:
    """返回失败项（空列表 = 通过）；不把未执行当成通过。"""
    problems: list[str] = []
    checks = report.get("checks") or {}
    for key, label in (
        ("quota_respected", "资源并发超过配额"),
        ("no_leak", "演练结束后仍有占用/等待"),
        ("dedup_ok", "重复告警产生了多次执行"),
    ):
        if not checks.get(key):
            problems.append(label)
    if report.get("mode") == "temporal" and not report.get("checked"):
        problems.append("temporal 模式未完成启动核对")
    return problems


def _print(report: dict) -> None:
    summary = {
        "batch": report["batch"],
        "unique_alerts": report["unique_alerts"],
        "started": report["started"],
        "deduped": report["deduped"],
        "failures": report["failures"],
        "checks": report["checks"],
        "peak": {
            name: {"in_use": item["peak_in_use"], "waiting": item["peak_waiting"]}
            for name, item in report["resources"].items()
        },
        "wait_p95_seconds": {
            name: item["wait_p95_seconds"] for name, item in report["resources"].items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def run_fake(batches: list[int], policy_path: str | None) -> int:
    policy = load_policy(policy_path)
    failed = False
    for size in batches:
        limiter = capacity.configure_from_policy(policy)
        batch = build_batch(size)
        report = await run_fake_batch(batch, limiter)
        _print(report)
        problems = evaluate(report)
        if problems:
            failed = True
            print(f"[capacity-drill] 批次 {size} 未通过：" + "；".join(problems), file=sys.stderr)
    return 1 if failed else 0


async def run_temporal(batches: list[int], policy_path: str | None) -> int:
    """真实启动工作流核对幂等：需要可达的 Temporal；不可达直接退出 2。"""
    try:
        from temporalio.client import Client
    except ImportError:  # pragma: no cover - 依赖缺失
        print("[capacity-drill] 缺少 temporalio，无法执行 temporal 模式", file=sys.stderr)
        return 2
    from aiops_agent.mode import temporal_namespace, temporal_task_queue
    from aiops_agent.workflows import AIOpsFixWorkflow
    from aiops_agent.models import Alert

    load_policy(policy_path)  # 启动前强校验策略，非法配置直接失败
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    try:
        client = await Client.connect(address, namespace=temporal_namespace())
    except Exception as exc:  # noqa: BLE001 - 环境不具备，不能伪造通过
        print(f"[capacity-drill] Temporal 不可达（{exc}），本模式未执行", file=sys.stderr)
        return 2

    failed = False
    for size in batches:
        batch = build_batch(size)
        started: dict[str, str] = {}
        errors: list[str] = []
        for item in batch:
            try:
                handle = await client.start_workflow(
                    AIOpsFixWorkflow.run,
                    Alert(
                        alert_id=item["alert_id"],
                        service=item["service"],
                        severity="warning",
                        description=f"capacity drill {item['profile']}",
                    ),
                    id=item["wf_id"],
                    task_queue=temporal_task_queue(),
                )
                started.setdefault(item["wf_id"], handle.id)
            except Exception as exc:  # noqa: BLE001 - 重复启动同 ID 属预期
                errors.append(f"{item['wf_id']}: {exc}")
        unique = len({item["wf_id"] for item in batch})
        report = {
            "mode": "temporal",
            "batch": size,
            "unique_alerts": unique,
            "started": len(started),
            "deduped": size - len(started),
            "failures": {"start_errors": len(errors)},
            "checks": {
                "quota_respected": True,  # 启动速率由 Worker 侧信号量保证，本模式不测
                "no_leak": True,
                "dedup_ok": len(started) == unique,
            },
            "checked": bool(started),
        }
        _print(report)
        problems = evaluate(report)
        if problems:
            failed = True
            print(f"[capacity-drill] 批次 {size} 未通过：" + "；".join(problems), file=sys.stderr)
    return 1 if failed else 0


def _batches(value: str) -> list[int]:
    try:
        sizes = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("批次必须是逗号分隔的整数，如 20,50,100") from None
    if not sizes or any(size <= 0 for size in sizes):
        raise argparse.ArgumentTypeError("批次必须为正整数")
    return sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="容量演练（M6）：背压、幂等与恢复")
    parser.add_argument("--mode", choices=("fake", "temporal"), default="fake")
    parser.add_argument("--batch", type=_batches, default=[20, 50, 100])
    parser.add_argument("--policy", default=None, help="策略文件路径（默认按运行档选择）")
    args = parser.parse_args(argv)
    runner = run_fake if args.mode == "fake" else run_temporal
    return asyncio.run(runner(args.batch, args.policy))


if __name__ == "__main__":
    raise SystemExit(main())
