"""模型路由（M3）：默认 27B、复杂多文件优先 Qoder、未达标不静默降级。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aiops_agent import model_routing as routing


class ParamSizeTest(unittest.TestCase):
    def test_sizes(self) -> None:
        self.assertEqual(routing.param_size("qwen3:8b"), 8.0)
        self.assertEqual(routing.param_size("qwen3.8:27b-mlx"), 27.0)
        self.assertEqual(routing.param_size("deepseek-r1:70b"), 70.0)
        self.assertEqual(routing.param_size("qoder"), 0.0)


class RouteFixTest(unittest.TestCase):
    def setUp(self) -> None:
        routing.reset_qoder_cache()

    def test_default_route_is_large_local_model(self) -> None:
        decision = routing.route_fix(
            ["order_service.py"],
            explicit_provider="ollama",
            explicit_model=None,
            qoder_available=False,
        )
        self.assertEqual(decision.provider, "ollama")
        self.assertEqual(decision.tier, "large")
        self.assertTrue(decision.qualified)
        self.assertEqual(decision.route, routing.ROUTE_CODE_FIX)

    def test_small_model_is_not_qualified(self) -> None:
        decision = routing.route_fix(
            ["order_service.py"],
            explicit_provider="ollama",
            explicit_model="qwen3:8b",
            qoder_available=False,
        )
        self.assertFalse(decision.qualified)
        self.assertIn("unverified_model", decision.reason)

    def test_require_quality_bar_routes_human(self) -> None:
        decision = routing.route_fix(
            ["order_service.py"],
            explicit_provider="ollama",
            explicit_model="qwen3:8b",
            qoder_available=False,
            require_quality_bar=True,
        )
        self.assertEqual(decision.route, routing.ROUTE_HUMAN)
        self.assertEqual(decision.reason, "quality_bar_not_met")

    def test_quality_bar_can_qualify_small_model(self) -> None:
        bar = {
            "models": {
                "ollama:qwen3:8b": {
                    "schema_pass_rate": 0.95,
                    "holdout_sandbox_pass_rate": 0.86,
                    "dataset_version": "abc123",
                }
            }
        }
        decision = routing.route_fix(
            ["order_service.py"],
            explicit_provider="ollama",
            explicit_model="qwen3:8b",
            qoder_available=False,
            quality_bar=bar,
            require_quality_bar=True,
            dataset_version="abc123",
        )
        self.assertTrue(decision.qualified)
        self.assertEqual(decision.route, routing.ROUTE_CODE_FIX)

    def test_quality_bar_dataset_mismatch_is_not_qualified(self) -> None:
        bar = {
            "models": {
                "ollama:qwen3:8b": {
                    "schema_pass_rate": 0.95,
                    "holdout_sandbox_pass_rate": 0.86,
                    "dataset_version": "old",
                }
            }
        }
        decision = routing.route_fix(
            ["order_service.py"],
            explicit_provider="ollama",
            explicit_model="qwen3:8b",
            qoder_available=False,
            quality_bar=bar,
            require_quality_bar=True,
            dataset_version="new",
        )
        self.assertFalse(decision.qualified)
        self.assertEqual(decision.route, routing.ROUTE_HUMAN)

    def test_multi_file_prefers_qoder_when_available(self) -> None:
        decision = routing.route_fix(
            ["a.py", "b.py"],
            explicit_provider=None,
            explicit_model=None,
            qoder_available=True,
        )
        self.assertEqual(decision.provider, "qoder")
        self.assertEqual(decision.reason, "multi_file_prefers_qoder")
        self.assertTrue(decision.multi_file)

    def test_multi_file_falls_back_when_qoder_unavailable(self) -> None:
        decision = routing.route_fix(
            ["a.py", "b.py"],
            explicit_provider="ollama",
            explicit_model=None,
            qoder_available=False,
        )
        self.assertEqual(decision.provider, "ollama")
        self.assertTrue(decision.multi_file)

    def test_explicit_provider_disables_auto_switch(self) -> None:
        decision = routing.route_fix(
            ["a.py", "b.py"],
            explicit_provider="ollama",
            explicit_model=None,
            qoder_available=True,
        )
        self.assertEqual(decision.provider, "ollama")
        self.assertEqual(decision.reason, "explicit_override")

    def test_multi_file_threshold_configurable(self) -> None:
        decision = routing.route_fix(
            ["a.py"],
            explicit_provider=None,
            qoder_available=True,
            multi_file_threshold=1,
        )
        self.assertEqual(decision.provider, "qoder")

    def test_labels_carry_observability_fields(self) -> None:
        decision = routing.route_fix(["a.py"], qoder_available=False)
        self.assertEqual(decision.labels["tier"], "large")
        self.assertIn("qualified", decision.labels)
        self.assertIn("reason", decision.labels)


class QualityBarFileTest(unittest.TestCase):
    def test_missing_and_corrupt_return_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(routing.load_quality_bar(Path(tmp) / "nope.json"))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not-json", encoding="utf-8")
            self.assertIsNone(routing.load_quality_bar(bad))

    def test_load_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bar.json"
            payload = {
                "models": {
                    "ollama:qwen3.8:27b-mlx": {
                        "schema_pass_rate": 0.92,
                        "holdout_sandbox_pass_rate": 0.87,
                    }
                }
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            bar = routing.load_quality_bar(path)
            entry = routing.bar_entry(bar, "ollama", "qwen3.8:27b-mlx")
            self.assertTrue(routing.meets_quality_bar(entry))

    def test_bar_entry_accepts_bare_model_key(self) -> None:
        bar = {"models": {"qwen3:8b": {"schema_pass_rate": 0.9, "holdout_sandbox_pass_rate": 0.85}}}
        self.assertIsNotNone(routing.bar_entry(bar, "ollama", "qwen3:8b"))

    def test_meets_quality_bar_rejects_shortfall(self) -> None:
        self.assertFalse(routing.meets_quality_bar({"schema_pass_rate": 0.89, "holdout_sandbox_pass_rate": 0.9}))
        self.assertFalse(routing.meets_quality_bar({"schema_pass_rate": 0.95, "holdout_sandbox_pass_rate": 0.84}))
        self.assertFalse(routing.meets_quality_bar(None))


class DescribeForMetaTest(unittest.TestCase):
    def test_labels_without_quality_bar(self) -> None:
        labels = routing.describe_for_meta("ollama", "qwen3.8:27b-mlx", ["a.py", "b.py"])
        self.assertEqual(labels["tier"], "large")
        self.assertTrue(labels["multi_file"])

    def test_small_model_label_not_qualified(self) -> None:
        labels = routing.describe_for_meta("ollama", "qwen3:8b", ["a.py"])
        self.assertFalse(labels["qualified"])


if __name__ == "__main__":
    unittest.main()
