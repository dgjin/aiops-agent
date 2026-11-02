"""容量演练（M6）：批次构造、配额/幂等/无泄漏检查与失败判定。

工具本身不伪造通过：``temporal`` 模式未完成启动核对时 ``evaluate`` 必须报问题；
fake 模式的断言也不能只看退出码，要核对峰值与残留。
"""

from __future__ import annotations

import unittest

from aiops_agent import capacity
from scripts import capacity_drill


class BuildBatchTest(unittest.TestCase):
    def test_duplicates_reuse_workflow_id(self) -> None:
        batch = capacity_drill.build_batch(20, duplicate_ratio=0.2)
        ids = [item["wf_id"] for item in batch]
        self.assertGreater(len(ids), len(set(ids)))
        self.assertTrue(all(item_id.startswith("aiops-fix-demo-app-drill-") for item_id in ids))

    def test_profile_coverage(self) -> None:
        batch = capacity_drill.build_batch(100)
        profiles = {item["profile"] for item in batch}
        self.assertEqual(profiles, {"normal", "duplicate", "no_logs", "model_timeout"})

    def test_zero_batch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            capacity_drill.build_batch(0)


class FakeRunTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, size: int) -> dict:
        limiter = capacity.ResourceLimiter({"local_llm": 1, "qoder": 1, "sandbox": 2, "release": 1})
        capacity.set_limiter(limiter)
        try:
            return await capacity_drill.run_fake_batch(
                capacity_drill.build_batch(size), limiter, model_delay=0, sandbox_delay=0
            )
        finally:
            capacity.set_limiter(None)

    async def test_batches_pass_and_respect_quota(self) -> None:
        for size in (20, 50, 100):
            report = await self._run(size)
            self.assertEqual(report["batch"], size)
            self.assertEqual(report["started"], report["unique_alerts"])
            self.assertEqual(report["deduped"], size - report["unique_alerts"])
            self.assertTrue(report["checks"]["quota_respected"], size)
            self.assertTrue(report["checks"]["no_leak"], size)
            self.assertTrue(report["checks"]["dedup_ok"], size)
            self.assertEqual(capacity_drill.evaluate(report), [], size)
            self.assertLessEqual(report["resources"]["local_llm"]["peak_in_use"], 1)
            self.assertLessEqual(report["resources"]["sandbox"]["peak_in_use"], 2)

    async def test_failures_are_classified(self) -> None:
        report = await self._run(50)
        self.assertGreater(report["failures"]["no_logs"], 0)
        self.assertGreater(report["failures"]["model_timeout"], 0)
        self.assertIn("queue_timeout", report["failures"])

    async def test_queue_timeout_is_counted_when_configured(self) -> None:
        limiter = capacity.ResourceLimiter({"local_llm": 1, "sandbox": 1}, queue_timeout=0.001)
        capacity.set_limiter(limiter)
        try:
            report = await capacity_drill.run_fake_batch(
                capacity_drill.build_batch(20), limiter, model_delay=0.01, sandbox_delay=0.01
            )
        finally:
            capacity.set_limiter(None)
        # 0.001s 超时在 20 条并发下必然出现拒绝；拒绝原因必须落到报告里
        self.assertGreater(report["failures"]["queue_timeout"], 0)
        self.assertGreater(
            report["resources"]["local_llm"]["rejected"].get("queue_timeout", 0), 0
        )
        self.assertTrue(report["checks"]["no_leak"])


class EvaluateTest(unittest.TestCase):
    def _report(self, **checks) -> dict:
        base = {"mode": "fake", "checks": {"quota_respected": True, "no_leak": True, "dedup_ok": True}}
        base["checks"].update(checks)
        return base

    def test_all_green(self) -> None:
        self.assertEqual(capacity_drill.evaluate(self._report()), [])

    def test_each_failure_is_reported(self) -> None:
        self.assertEqual(
            capacity_drill.evaluate(self._report(quota_respected=False)),
            ["资源并发超过配额"],
        )
        self.assertEqual(
            capacity_drill.evaluate(self._report(no_leak=False)),
            ["演练结束后仍有占用/等待"],
        )
        self.assertEqual(
            capacity_drill.evaluate(self._report(dedup_ok=False)),
            ["重复告警产生了多次执行"],
        )

    def test_temporal_without_check_cannot_pass(self) -> None:
        report = self._report()
        report["mode"] = "temporal"
        report["checked"] = False
        self.assertEqual(capacity_drill.evaluate(report), ["temporal 模式未完成启动核对"])


if __name__ == "__main__":
    unittest.main()
