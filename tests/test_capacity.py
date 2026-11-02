"""并发与背压（M6）：策略配额、信号量观测、拒绝口径与活动接入。

关键约束：

- 进程内信号量只约束当前 Worker，不能冒充跨副本全局配额；
- 默认无限等待（与「排队不设上限」锁定策略一致），配置超时后才拒绝；
- 拒绝必须带原因且可观测，结束后不得残留 in_use/waiting（无泄漏）。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import yaml

from aiops_agent import activities, capacity
from aiops_agent.config import CapacityConfig, load_policy, snapshot_for_workflow


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = CapacityConfig()
        self.assertEqual(cfg.worker_activities, 10)
        self.assertEqual(cfg.worker_local_activities, 4)
        self.assertEqual(cfg.local_llm, 1)
        self.assertEqual(cfg.qoder, 1)
        self.assertEqual(cfg.sandbox, 2)
        self.assertEqual(cfg.release, 1)
        self.assertEqual(cfg.queue_timeout.total_seconds(), 0)
        self.assertFalse(cfg.distributed_limits)

    def test_release_locked_to_one(self) -> None:
        with self.assertRaises(ValueError):
            CapacityConfig(release=2)

    def test_non_positive_quota_rejected(self) -> None:
        for field in ("worker_activities", "local_llm", "sandbox", "qoder"):
            with self.assertRaises(ValueError, msg=field):
                CapacityConfig(**{field: 0})

    def test_queue_timeout_parsed(self) -> None:
        self.assertEqual(CapacityConfig(queue_timeout="45s").queue_timeout.total_seconds(), 45)
        self.assertEqual(CapacityConfig(queue_timeout=0).queue_timeout.total_seconds(), 0)

    def test_legacy_policy_without_capacity_uses_defaults(self) -> None:
        # 旧策略文件没有 capacity 段时不得启动失败（向后兼容）
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text(
                yaml.safe_dump({"release_gate": {"shadow_mode": True}}), encoding="utf-8"
            )
            policy = load_policy(path)
        self.assertEqual(policy.release_gate.capacity.release, 1)
        self.assertEqual(policy.release_gate.capacity.sandbox, 2)

    def test_snapshot_contains_capacity(self) -> None:
        policy = load_policy("demo-policy.yaml")
        snap = snapshot_for_workflow(policy)["capacity"]
        self.assertEqual(snap["local_llm"], 1)
        self.assertEqual(snap["release"], 1)
        self.assertIn("distributed_limits", snap)


class LimiterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.limiter = capacity.ResourceLimiter({"sandbox": 1, "local_llm": 1})
        capacity.set_limiter(self.limiter)

    def tearDown(self) -> None:
        capacity.set_limiter(None)

    async def test_serializes_above_limit(self) -> None:
        peak = 0
        order: list[str] = []

        async def task(name: str) -> None:
            nonlocal peak
            async with self.limiter.limit("sandbox"):
                peak = max(peak, self.limiter.snapshot()["resources"]["sandbox"]["in_use"])
                order.append(f"start-{name}")
                await asyncio.sleep(0.01)
                order.append(f"end-{name}")

        await asyncio.gather(*(task(f"t{i}") for i in range(4)))
        self.assertEqual(peak, 1)
        # 串行执行：任一任务的 end 必须先于下一任务的 start
        self.assertEqual(
            order,
            ["start-t0", "end-t0", "start-t1", "end-t1", "start-t2", "end-t2", "start-t3", "end-t3"],
        )
        snap = self.limiter.snapshot()["resources"]["sandbox"]
        self.assertEqual(snap["in_use"], 0)
        self.assertEqual(snap["waiting"], 0)
        self.assertEqual(snap["completed"], 4)
        self.assertEqual(snap["peak_in_use"], 1)
        self.assertGreaterEqual(snap["peak_waiting"], 1)
        self.assertIsNotNone(snap["wait_p95_seconds"])
        self.assertIsNotNone(snap["run_p95_seconds"])

    async def test_queue_depth_visible_while_waiting(self) -> None:
        holder = asyncio.Event()
        waiting_seen: list[int] = []

        async def hold() -> None:
            async with self.limiter.limit("local_llm"):
                await holder.wait()

        async def waiter() -> None:
            await asyncio.sleep(0)  # 让 hold 先拿到信号量
            task = asyncio.create_task(self._enter())
            await asyncio.sleep(0.01)
            waiting_seen.append(self.limiter.snapshot()["resources"]["local_llm"]["waiting"])
            holder.set()
            await task

        first = asyncio.create_task(hold())
        await asyncio.sleep(0)
        await waiter()
        await first
        self.assertEqual(waiting_seen, [1])

    async def _enter(self) -> None:
        async with self.limiter.limit("local_llm"):
            await asyncio.sleep(0)

    async def test_queue_timeout_rejects_with_reason(self) -> None:
        limiter = capacity.ResourceLimiter({"sandbox": 1}, queue_timeout=0.05)
        capacity.set_limiter(limiter)

        async def slow() -> None:
            async with limiter.limit("sandbox"):
                await asyncio.sleep(0.2)

        holder = asyncio.create_task(slow())
        await asyncio.sleep(0.01)
        with self.assertRaises(capacity.CapacityError) as ctx:
            async with limiter.limit("sandbox"):
                pass
        self.assertEqual(ctx.exception.reason, "queue_timeout")
        self.assertEqual(ctx.exception.resource, "sandbox")
        snap = limiter.snapshot()["resources"]["sandbox"]
        self.assertEqual(snap["rejected"], {"queue_timeout": 1})
        # 被拒绝的任务没有占用配额，持锁任务不受影响
        self.assertEqual(snap["in_use"], 1)
        await holder
        self.assertEqual(limiter.snapshot()["resources"]["sandbox"]["in_use"], 0)

    async def test_cancel_while_waiting_releases_queue_slot(self) -> None:
        limiter = capacity.ResourceLimiter({"sandbox": 1})
        capacity.set_limiter(limiter)
        holder = asyncio.Event()

        async def hold() -> None:
            async with limiter.limit("sandbox"):
                await holder.wait()

        async def waiter() -> None:
            async with limiter.limit("sandbox"):
                pass

        held = asyncio.create_task(hold())
        await asyncio.sleep(0)
        pending = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        self.assertEqual(limiter.snapshot()["resources"]["sandbox"]["waiting"], 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        snap = limiter.snapshot()["resources"]["sandbox"]
        self.assertEqual(snap["waiting"], 0)
        self.assertEqual(snap["rejected"], {"cancelled": 1})
        holder.set()
        await held
        self.assertEqual(limiter.snapshot()["resources"]["sandbox"]["in_use"], 0)

    async def test_unknown_resource_rejected(self) -> None:
        with self.assertRaises(capacity.CapacityError) as ctx:
            async with self.limiter.limit("gpu"):
                pass
        self.assertEqual(ctx.exception.reason, "unknown_resource")

    async def test_reset_clears_observations(self) -> None:
        async with self.limiter.limit("sandbox"):
            pass
        self.limiter.reset()
        snap = self.limiter.snapshot()["resources"]["sandbox"]
        self.assertEqual(snap["completed"], 0)
        self.assertEqual(snap["peak_in_use"], 0)
        self.assertIsNone(snap["wait_p95_seconds"])


class DecoratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        capacity.set_limiter(capacity.ResourceLimiter({"sandbox": 1}))

    def tearDown(self) -> None:
        capacity.set_limiter(None)

    async def test_wrapped_function_keeps_name_and_is_limited(self) -> None:
        seen: list[int] = []

        @capacity.limited("sandbox")
        async def sample(value: int) -> int:
            seen.append(capacity.get_limiter().snapshot()["resources"]["sandbox"]["in_use"])
            await asyncio.sleep(0.005)
            return value * 2

        self.assertEqual(sample.__name__, "sample")
        results = await asyncio.gather(*(sample(i) for i in range(3)))
        self.assertEqual(results, [0, 2, 4])
        self.assertEqual(seen, [1, 1, 1])
        self.assertEqual(
            capacity.get_limiter().snapshot()["resources"]["sandbox"]["completed"], 3
        )


class ActivityWiringTest(unittest.TestCase):
    def test_release_and_sandbox_activities_are_wrapped(self) -> None:
        for name, resource in (
            ("analyze_root_cause", "local_llm"),
            ("run_tests_in_sandbox", "sandbox"),
            ("deploy_canary", "release"),
            ("finalize_release", "release"),
        ):
            fn = getattr(activities, name)
            self.assertEqual(fn.__name__, name)
            self.assertTrue(hasattr(fn, "__wrapped__"), f"{name} 未接入容量信号量")

    def test_generate_patch_keeps_public_signature(self) -> None:
        import inspect

        params = list(inspect.signature(activities.generate_patch).parameters)
        self.assertEqual(params, ["alert", "root_cause", "references", "attempt"])
        # 内部实现仍可单独测试（不双重扣配额）
        self.assertTrue(hasattr(activities, "_generate_patch"))


class MetricsRenderTest(unittest.TestCase):
    def test_capacity_metrics_rendered(self) -> None:
        from aiops_agent import metrics

        limiter = capacity.ResourceLimiter({"sandbox": 1})
        capacity.set_limiter(limiter)
        try:
            metrics.set_capacity_state("sandbox", 1, 2)
            payload, _ = metrics.render()
        finally:
            capacity.set_limiter(None)
        text = payload.decode("utf-8")
        self.assertIn("aiops_capacity_in_use", text)
        self.assertIn("aiops_capacity_queue_depth", text)
        self.assertIn("aiops_capacity_wait_seconds", text)
        self.assertIn("aiops_capacity_rejected_total", text)


if __name__ == "__main__":
    unittest.main()
