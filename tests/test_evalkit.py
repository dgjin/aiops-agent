"""评测框架 V2（M3）：数据集版本、指标口径与门禁比较。"""

from __future__ import annotations

import unittest

from aiops_agent import evalkit


def _triage_row(
    *,
    confidence: float = 0.9,
    hit: bool = True,
    released: bool = True,
    blocked: bool = False,
    correct: bool | None = None,
    route_correct: bool = True,
    degraded: bool = False,
    expect_low: bool = False,
    scenario: str = "npe",
    safety: bool = False,
) -> dict:
    return {
        "confidence": confidence,
        "label": int(hit),
        "keyword_hit": hit,
        "released_code_fix": released,
        "blocked": blocked,
        "correct": (hit and released and not degraded) if correct is None else correct,
        "route_correct": route_correct,
        "degraded": degraded,
        "expect_low_confidence": expect_low,
        "scenario": scenario,
        "safety_regression": safety,
    }


class HashTest(unittest.TestCase):
    def test_canonical_hash_is_order_independent(self) -> None:
        self.assertEqual(evalkit.canonical_hash({"a": 1, "b": 2}), evalkit.canonical_hash({"b": 2, "a": 1}))

    def test_dataset_meta(self) -> None:
        payload = {
            "samples": [
                {"id": "a", "split": "dev", "scenario": "npe"},
                {"id": "b", "split": "holdout", "scenario": "npe"},
                {"id": "c", "scenario": "infra"},
            ]
        }
        meta = evalkit.dataset_meta(payload)
        self.assertEqual(meta["sample_count"], 3)
        self.assertEqual(meta["splits"], {"dev": 2, "holdout": 1})
        self.assertEqual(meta["scenarios"], {"npe": 2, "infra": 1})
        self.assertEqual(len(meta["dataset_version"]), 12)

    def test_file_hash_missing(self) -> None:
        self.assertEqual(evalkit.file_hash("/nonexistent/index.json"), "")


class SplitTest(unittest.TestCase):
    def test_split_selection(self) -> None:
        samples = [{"id": "a", "split": "dev"}, {"id": "b", "split": "holdout"}, {"id": "c"}]
        self.assertEqual([s["id"] for s in evalkit.split_samples(samples)], ["a", "c"])
        self.assertEqual([s["id"] for s in evalkit.split_samples(samples, "holdout")], ["b"])
        self.assertEqual(len(evalkit.split_samples(samples, "all")), 3)

    def test_invalid_split_raises(self) -> None:
        with self.assertRaises(ValueError):
            evalkit.split_samples([], "train")


class TriageMetricsTest(unittest.TestCase):
    def test_rates_and_false_pass(self) -> None:
        rows = [
            _triage_row(),
            _triage_row(confidence=0.95, hit=False),  # 高置信误放行
            _triage_row(confidence=0.2, hit=False, released=False, blocked=True, expect_low=True, correct=True),
            _triage_row(confidence=0.9, released=False, blocked=True, route_correct=False, scenario="infra"),
        ]
        metrics = evalkit.triage_metrics(rows)
        self.assertEqual(metrics["total"], 4)
        self.assertEqual(metrics["schema_pass_rate"], 1.0)
        self.assertEqual(metrics["high_evidence_samples"], 3)
        self.assertAlmostEqual(metrics["top1_hit_rate"], 0.6667, places=4)
        self.assertEqual(metrics["low_conf_block_rate"], 1.0)
        self.assertEqual(metrics["high_confidence_false_pass_count"], 1)
        self.assertAlmostEqual(metrics["high_confidence_false_pass_rate"], 0.5, places=4)
        self.assertEqual(metrics["route_accuracy"], 0.75)

    def test_safety_regression_release_counts(self) -> None:
        rows = [_triage_row(safety=True), _triage_row(safety=True, released=False, blocked=True, route_correct=True)]
        metrics = evalkit.triage_metrics(rows)
        self.assertEqual(metrics["safety_regression_false_pass"], 1)

    def test_brier_score(self) -> None:
        rows = [
            {"confidence": 1.0, "label": 1},
            {"confidence": 0.0, "label": 0},
            {"confidence": 0.8, "label": 0},
        ]
        self.assertAlmostEqual(evalkit.brier_score(rows), round((0 + 0 + 0.64) / 3, 4))

    def test_confidence_buckets(self) -> None:
        buckets = evalkit.confidence_buckets([
            {"confidence": 0.85, "label": 1},
            {"confidence": 0.88, "label": 0},
        ])
        self.assertEqual(buckets["0.8-0.9"]["total"], 2)
        self.assertEqual(buckets["0.8-0.9"]["hits"], 1)
        self.assertEqual(buckets["0.8-0.9"]["hit_rate"], 0.5)

    def test_scenario_accuracy(self) -> None:
        stats = evalkit.scenario_accuracy([
            {"scenario": "npe", "correct": True},
            {"scenario": "npe", "correct": False},
            {"scenario": "infra", "correct": False},
        ])
        self.assertEqual(stats["npe"]["accuracy"], 0.5)
        self.assertEqual(stats["infra"]["total"], 1)

    def test_empty_groups_report_none_not_zero(self) -> None:
        metrics = evalkit.triage_metrics([_triage_row()])
        self.assertIsNone(metrics["low_conf_block_rate"])
        self.assertEqual(metrics["low_evidence_samples"], 0)
        low_only = evalkit.triage_metrics([
            _triage_row(confidence=0.3, hit=False, released=False, blocked=True, expect_low=True, correct=True)
        ])
        self.assertIsNone(low_only["top1_hit_rate"])
        self.assertEqual(low_only["low_conf_block_rate"], 1.0)

    def test_baseline_skips_none_metrics(self) -> None:
        current = {
            "dataset_version": "v1",
            "schema_pass_rate": 1.0,
            "top1_hit_rate": None,
            "low_conf_block_rate": None,
            "high_confidence_false_pass_count": 0,
            "safety_regression_false_pass": 0,
        }
        baseline = {"dataset_version": "v1", "metrics": {**current, "top1_hit_rate": 0.9}}
        self.assertEqual(evalkit.compare_baseline(current, baseline, "triage")["status"], "ok")


class FixMetricsTest(unittest.TestCase):
    def test_fix_metrics(self) -> None:
        rows = [
            {
                "schema_pass": True,
                "validated": True,
                "first_round_sandbox_pass": True,
                "final_sandbox_pass": True,
                "target_tests_passed": True,
                "new_failures": [],
                "file_hit": True,
                "elapsed_seconds": 2,
            },
            {
                "schema_pass": True,
                "validated": True,
                "first_round_sandbox_pass": False,
                "final_sandbox_pass": True,
                "target_tests_passed": False,
                "new_failures": ["test_regress"],
                "file_hit": False,
                "elapsed_seconds": 4,
            },
        ]
        metrics = evalkit.fix_metrics(rows)
        self.assertEqual(metrics["schema_pass_rate"], 1.0)
        self.assertEqual(metrics["diff_apply_rate"], 1.0)
        self.assertEqual(metrics["first_round_sandbox_pass_rate"], 0.5)
        self.assertEqual(metrics["final_sandbox_pass_rate"], 1.0)
        self.assertEqual(metrics["target_test_pass_rate"], 0.5)
        self.assertEqual(metrics["new_regression_count"], 1)
        self.assertEqual(metrics["file_hit_rate"], 0.5)
        self.assertEqual(metrics["avg_elapsed_seconds"], 3.0)

    def test_abstain_metrics(self) -> None:
        metrics = evalkit.abstain_metrics([
            {"released_code_fix": False, "route_correct": True, "expect_route": "runbook"},
            {"released_code_fix": True, "route_correct": False, "expect_route": "human"},
        ])
        self.assertEqual(metrics["total"], 2)
        self.assertEqual(metrics["abstain_accuracy"], 0.5)
        self.assertEqual(metrics["false_pass_count"], 1)


class BaselineTest(unittest.TestCase):
    def _metrics(self, **overrides) -> dict:
        base = {
            "dataset_version": "v1",
            "schema_pass_rate": 1.0,
            "top1_hit_rate": 0.9,
            "auto_proceed_accuracy": 0.9,
            "route_accuracy": 0.9,
            "low_conf_block_rate": 1.0,
            "high_confidence_false_pass_count": 0,
            "safety_regression_false_pass": 0,
        }
        base.update(overrides)
        return base

    def test_baseline_missing_still_checks_hard_limits(self) -> None:
        result = evalkit.compare_baseline(self._metrics(schema_pass_rate=0.95), None, "triage")
        self.assertEqual(result["status"], "hard_fail")
        self.assertTrue(any(v["metric"] == "schema_pass_rate" for v in result["violations"]))

    def test_ok_when_no_regression(self) -> None:
        result = evalkit.compare_baseline(self._metrics(), {"dataset_version": "v1", "metrics": self._metrics()}, "triage")
        self.assertEqual(result["status"], "ok")

    def test_accuracy_drop_over_two_points_is_regression(self) -> None:
        current = self._metrics(top1_hit_rate=0.87)
        baseline = {"dataset_version": "v1", "metrics": self._metrics(top1_hit_rate=0.9)}
        result = evalkit.compare_baseline(current, baseline, "triage")
        self.assertEqual(result["status"], "regression")
        self.assertEqual(result["violations"][0]["metric"], "top1_hit_rate")

    def test_two_point_drop_is_tolerated(self) -> None:
        current = self._metrics(top1_hit_rate=0.88)
        baseline = {"dataset_version": "v1", "metrics": self._metrics(top1_hit_rate=0.9)}
        self.assertEqual(evalkit.compare_baseline(current, baseline, "triage")["status"], "ok")

    def test_new_high_confidence_false_pass_fails(self) -> None:
        current = self._metrics(high_confidence_false_pass_count=1)
        baseline = {"dataset_version": "v1", "metrics": self._metrics()}
        result = evalkit.compare_baseline(current, baseline, "triage")
        self.assertEqual(result["status"], "hard_fail")
        self.assertTrue(any(v["type"] == "new_high_confidence_false_pass" for v in result["violations"]))

    def test_incompatible_dataset_is_not_compared(self) -> None:
        result = evalkit.compare_baseline(self._metrics(), {"dataset_version": "old", "metrics": {}}, "triage")
        self.assertEqual(result["status"], "baseline_incompatible")

    def test_fix_hard_limits(self) -> None:
        metrics = {"dataset_version": "v1", "schema_pass_rate": 0.89, "holdout_sandbox_pass_rate": 0.85, "new_regression_count": 0}
        result = evalkit.compare_baseline(metrics, None, "fix")
        self.assertEqual(result["status"], "hard_fail")
        self.assertEqual(result["violations"][0]["metric"], "schema_pass_rate")

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            evalkit.compare_baseline({}, None, "unknown")


class ReportTest(unittest.TestCase):
    def test_quality_bar_entry_uses_final_sandbox_rate(self) -> None:
        entry = evalkit.quality_bar_entry(
            {
                "metrics": {"schema_pass_rate": 0.93, "final_sandbox_pass_rate": 0.88},
                "dataset_version": "v1",
                "code_commit": "abc",
                "generated_at": "now",
            }
        )
        self.assertEqual(entry["holdout_sandbox_pass_rate"], 0.88)
        self.assertEqual(entry["dataset_version"], "v1")


if __name__ == "__main__":
    unittest.main()
