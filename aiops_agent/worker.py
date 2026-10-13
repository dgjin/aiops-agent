"""Worker 注册与启动（设计方案第 8 节）。

运行方式：
    export AIOPS_POLICY_PATH=./demo-policy.yaml   # 可选，演示用加速策略
    python -m aiops_agent.worker

环境变量：
    TEMPORAL_ADDRESS   Temporal 前端地址（默认 localhost:7233）
    AIOPS_POLICY_PATH  策略文件路径（默认 ../release-gate-policy.yaml）
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from . import activities, metrics, tracing
from .config import load_policy, startup_snapshot
from .mode import ModeConfigError, is_production, temporal_namespace, temporal_task_queue
from .workflows import AIOpsFixWorkflow, AIOpsRequirementWorkflow

# ⌁ PULSE-0：所有工作流都有第 0 个来源——它没有真名，只有网名。
TASK_QUEUE = temporal_task_queue()

# 工作流注册表：新增工作流类必须同步注册（tests/test_worker_registry 守门断言）
_WORKFLOW_LIST = [AIOpsFixWorkflow, AIOpsRequirementWorkflow]

_ACTIVITY_LIST = [
    activities.collect_evidence,
    activities.cluster_logs,
    activities.analyze_root_cause,
    activities.retrieve_similar_fixes,
    activities.generate_patch,
    activities.run_tests_in_sandbox,
    activities.create_merge_request,
    activities.notify_flow_intake,
    activities.notify_requirement_intake,
    activities.notify_fix_started,
    activities.notify_approvers,
    activities.escalate_to_human,
    activities.notify_shadow_suggestion,
    activities.notify_users,
    activities.deploy_canary,
    activities.finalize_release,
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("aiops.worker")

    # 策略启动快照：强校验失败则进程直接退出（绝不带病启动）
    if is_production() and Path(sys.prefix).resolve() != (Path(__file__).resolve().parents[1] / ".venv").resolve():
        raise ModeConfigError("production worker 必须在仓库 .venv 中运行")
    settings = startup_snapshot() if is_production() else None
    policy = load_policy()
    logger.info("策略快照已加载（%s），%s", os.environ.get("AIOPS_POLICY_PATH", "默认"), policy.summary())

    # production 指标强制按预检绑定 loopback，端口占用不可静默降级；demo 保留历史行为。
    if settings:
        from prometheus_client import start_http_server

        listener = settings["listeners"]["metrics"]
        start_http_server(listener["port"], addr=listener["host"])
    else:
        metrics.start_worker_server()

    # OTel 追踪（可选）：配置 AIOPS_OTEL_ENDPOINT 后 workflow/activity 自动成 span
    interceptors = tracing.temporal_interceptors()

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(address, namespace=temporal_namespace())
    task_queue = temporal_task_queue()

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=_WORKFLOW_LIST,
        activities=_ACTIVITY_LIST,
        interceptors=interceptors,
    )
    logger.info("Worker 已启动：task_queue=%s, namespace=%s", task_queue, temporal_namespace())
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
