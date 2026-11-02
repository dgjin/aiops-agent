"""灰度放行检查（M6）：阶段门槛、未知项判定与人工确认。

核心约束：缺少指标或未确认的人工演练只能是 ``pending``/``unknown``，
不得因为「没报错」就放行；硬条件不满足一律 ``block``。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import rollout_gate

CONFIRMED = {"backup-restore", "kill-switch", "runbook"}


def _metrics(**overrides) -> dict:
    base = {
        "period_days": 7,
        "closed_total": 60,
        "auto_fix_rate": 0.6,
        "auto_fix_sample": 36,
        "by_service": [
            {"service": "pay", "closed": 12, "done": 8, "auto_fix_rate": 0.6667,
             "human_intervention_rate": 0.25},
            {"service": "idle", "closed": 3, "done": 0, "auto_fix_rate": 0.0,
             "human_intervention_rate": 0.0},
        ],
        "evidence": {"available": True, "sample": 60, "ok_rate": 1.0},
        "sandbox": {
            "high_confidence_false_pass": 0,
            "real_sample": 8,
            "real_pass_rate": 0.875,
            "static_count": 2,
            "override_rate": 0.1,
        },
        "rollback": {"rate": 0.0, "sample": 0, "released": 6},
    }
    base.update(overrides)
    return base


def _ids(result: dict, status: str) -> set[str]:
    return {item["id"] for item in result["checks"] if item["status"] == status}


class ShadowStageTest(unittest.TestCase):
    def test_green_shadow_stage_allows(self) -> None:
        result = rollout_gate.evaluate("staging-shadow", _metrics(), confirmed=CONFIRMED)
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(_ids(result, "fail"), set())

    def test_sample_shortfall_blocks(self) -> None:
        result = rollout_gate.evaluate(
            "staging-shadow", _metrics(closed_total=30), confirmed=CONFIRMED
        )
        self.assertEqual(result["decision"], "block")
        self.assertIn("sample", _ids(result, "fail"))

    def test_short_window_blocks(self) -> None:
        result = rollout_gate.evaluate(
            "production-shadow", _metrics(period_days=3), confirmed=CONFIRMED
        )
        self.assertIn("sample", _ids(result, "fail"))

    def test_missing_evidence_is_unknown_not_pass(self) -> None:
        result = rollout_gate.evaluate(
            "staging-shadow",
            _metrics(evidence={"available": False, "sample": 0, "ok_rate": None}),
            confirmed=CONFIRMED,
        )
        self.assertEqual(result["decision"], "pending")
        self.assertIn("evidence", _ids(result, "unknown"))

    def test_false_pass_blocks(self) -> None:
        result = rollout_gate.evaluate(
            "staging-shadow",
            _metrics(sandbox={"high_confidence_false_pass": 1}),
            confirmed=CONFIRMED,
        )
        self.assertEqual(result["decision"], "block")
        self.assertIn("false-pass", _ids(result, "fail"))

    def test_unconfirmed_manual_items_pending(self) -> None:
        result = rollout_gate.evaluate("staging-shadow", _metrics(), confirmed=set())
        self.assertEqual(result["decision"], "pending")
        self.assertEqual(_ids(result, "unknown"), set(rollout_gate.MANUAL_ITEMS))


class SingleServiceStageTest(unittest.TestCase):
    def test_named_service_passes(self) -> None:
        result = rollout_gate.evaluate(
            "single-service", _metrics(), service="pay", confirmed=CONFIRMED,
            quality_bar=Path("/"),
        )
        self.assertEqual(result["decision"], "allow", result["checks"])

    def test_unknown_service_is_pending(self) -> None:
        result = rollout_gate.evaluate(
            "single-service", _metrics(), service="nope", confirmed=CONFIRMED,
            quality_bar=Path("/"),
        )
        self.assertEqual(result["decision"], "pending")
        self.assertIn("auto-fix", _ids(result, "unknown"))

    def test_low_auto_fix_rate_blocks(self) -> None:
        result = rollout_gate.evaluate(
            "single-service", _metrics(), service="idle", confirmed=CONFIRMED,
            quality_bar=Path("/"),
        )
        self.assertEqual(result["decision"], "block")
        self.assertIn("auto-fix", _ids(result, "fail"))

    def test_missing_quality_bar_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "model_quality_bar.json"
            result = rollout_gate.evaluate(
                "single-service", _metrics(), service="pay", confirmed=CONFIRMED,
                quality_bar=missing,
            )
        self.assertEqual(result["decision"], "block")
        self.assertIn("quality-bar", _ids(result, "fail"))

    def test_static_only_samples_cannot_prove(self) -> None:
        result = rollout_gate.evaluate(
            "single-service",
            _metrics(sandbox={
                "high_confidence_false_pass": 0,
                "real_sample": 0,
                "real_pass_rate": None,
                "static_count": 5,
                "override_rate": None,
            }),
            service="pay",
            confirmed=CONFIRMED,
            quality_bar=Path("/"),
        )
        self.assertEqual(result["decision"], "pending")
        self.assertIn("sandbox-split", _ids(result, "unknown"))


class LimitedReleaseStageTest(unittest.TestCase):
    def _run(self, metrics: dict, baseline: dict | None) -> dict:
        return rollout_gate.evaluate(
            "limited-release", metrics, service="pay", confirmed=CONFIRMED,
            quality_bar=Path("/"), baseline=baseline,
        )

    def test_missing_baseline_pending(self) -> None:
        result = self._run(_metrics(), None)
        self.assertEqual(result["decision"], "pending")
        self.assertIn("baseline", _ids(result, "unknown"))

    def test_regression_blocks(self) -> None:
        metrics = _metrics(rollback={"rate": 0.4, "sample": 2, "released": 5})
        result = self._run(metrics, {"rollback_rate": 0.1, "override_rate": 0.1})
        self.assertEqual(result["decision"], "block")
        self.assertIn("baseline", _ids(result, "fail"))

    def test_within_baseline_allows(self) -> None:
        result = self._run(_metrics(), {"rollback_rate": 0.2, "override_rate": 0.2})
        self.assertEqual(result["decision"], "allow", result["checks"])


class ExpandStageTest(unittest.TestCase):
    def test_expand_requires_explicit_service(self) -> None:
        result = rollout_gate.evaluate(
            "expand", _metrics(), confirmed=CONFIRMED, quality_bar=Path("/"),
            baseline={"rollback_rate": 0.2, "override_rate": 0.2},
        )
        self.assertEqual(result["decision"], "pending")
        self.assertIn("service", _ids(result, "unknown"))

    def test_unknown_stage_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rollout_gate.evaluate("full", _metrics())


class CliTest(unittest.TestCase):
    def test_missing_metrics_exits_pending(self) -> None:
        self.assertEqual(rollout_gate.main(["--stage", "staging-shadow"]), 2)

    def test_cli_allow_and_block_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ops.json"
            path.write_text(json.dumps(_metrics()), encoding="utf-8")
            allow = rollout_gate.main(
                ["--stage", "staging-shadow", "--metrics", str(path),
                 "--confirm", ",".join(sorted(CONFIRMED))]
            )
            self.assertEqual(allow, 0)
            blocked = rollout_gate.main(
                ["--stage", "staging-shadow", "--metrics", str(path)]
            )
            self.assertEqual(blocked, 2)  # 未确认人工项 → pending

    def test_broken_metrics_file_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not-json", encoding="utf-8")
            self.assertEqual(
                rollout_gate.main(["--stage", "staging-shadow", "--metrics", str(path)]), 2
            )


if __name__ == "__main__":
    unittest.main()
