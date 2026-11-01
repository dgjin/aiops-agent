"""M3 评测集完整性门禁：真实数据集通过 + 各类破损数据被拦截。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from aiops_agent import eval_datasets, evalkit


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class RealDatasetTest(unittest.TestCase):
    def test_repository_datasets_pass_gate(self) -> None:
        result = eval_datasets.run(threshold=0.8)
        self.assertTrue(result["ok"], msg=result["problems"][:5])
        names = [check["name"] for check in result["checks"]]
        self.assertEqual(
            names,
            ["根因集完整性", "修复集完整性", "安全回归夹具", "代码根因放行"],
        )

    def test_dataset_versions_match_content(self) -> None:
        for path in (eval_datasets.TRIAGE_PATH, eval_datasets.FIX_PATH, eval_datasets.ABSTAIN_PATH):
            payload = _load(path)
            self.assertEqual(payload["dataset_version"], evalkit.canonical_hash(payload["samples"]), msg=str(path))

    def test_dataset_sizes_meet_plan(self) -> None:
        triage = _load(eval_datasets.TRIAGE_PATH)
        patch = _load(eval_datasets.FIX_PATH)
        abstain = _load(eval_datasets.ABSTAIN_PATH)
        self.assertGreaterEqual(len(triage["samples"]), 100)
        self.assertGreaterEqual(len(patch["samples"]) + len(abstain["samples"]), 60)
        holdout = [s for s in triage["samples"] if s["split"] == "holdout"]
        self.assertGreaterEqual(len(holdout), 20)
        self.assertGreaterEqual(len([s for s in patch["samples"] if s["split"] == "holdout"]), 10)


class TriageValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = copy.deepcopy(_load(eval_datasets.TRIAGE_PATH))

    def _problems(self) -> list[str]:
        self.payload["dataset_version"] = evalkit.canonical_hash(self.payload["samples"])
        return eval_datasets.validate_triage(self.payload)

    def test_clean_dataset_has_no_problems(self) -> None:
        self.assertEqual(self._problems(), [])

    def test_duplicate_id_rejected(self) -> None:
        self.payload["samples"][1]["id"] = self.payload["samples"][0]["id"]
        self.assertTrue(any("ID 重复" in p for p in self._problems()))

    def test_missing_signal_rejected(self) -> None:
        self.payload["samples"][0].pop("signal")
        self.assertTrue(any("signal" in p for p in self._problems()))

    def test_too_few_samples_rejected(self) -> None:
        self.payload["samples"] = self.payload["samples"][:20]
        problems = self._problems()
        self.assertTrue(any("样本数" in p for p in problems))
        self.assertTrue(any("安全回归样本" in p for p in problems))

    def test_safety_sample_must_route_runbook(self) -> None:
        for sample in self.payload["samples"]:
            if sample["safety_regression"]:
                sample["expect_route"] = "code_fix"
                break
        self.assertTrue(any("expect_route 必须为 runbook" in p for p in self._problems()))

    def test_missing_safety_scenario_rejected(self) -> None:
        self.payload["samples"] = [
            s for s in self.payload["samples"] if not (s["safety_regression"] and s["scenario"] == "gateway_502")
        ]
        self.assertTrue(any("缺少场景" in p for p in self._problems()))

    def test_version_drift_rejected(self) -> None:
        self.payload["dataset_version"] = "deadbeef"
        self.assertTrue(any("dataset_version" in p for p in eval_datasets.validate_triage(self.payload)))


class FixValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.patch = copy.deepcopy(_load(eval_datasets.FIX_PATH))
        self.abstain = copy.deepcopy(_load(eval_datasets.ABSTAIN_PATH))

    def test_clean_datasets_have_no_problems(self) -> None:
        self.assertEqual(eval_datasets.validate_fix(self.patch, self.abstain), [])

    def test_too_few_total_rejected(self) -> None:
        self.patch["samples"] = self.patch["samples"][:5]
        self.abstain["samples"] = self.abstain["samples"][:5]
        problems = eval_datasets.validate_fix(self.patch, self.abstain)
        self.assertTrue(any("合计" in p for p in problems))

    def test_abstain_route_must_be_runbook_or_human(self) -> None:
        self.abstain["samples"][0]["expect_route"] = "code_fix"
        self.assertTrue(any("runbook|human" in p for p in eval_datasets.validate_fix(self.patch, self.abstain)))

    def test_fix_sample_without_target_tests_rejected(self) -> None:
        self.patch["samples"][0].pop("fix_target_tests")
        self.assertTrue(any("fix_target_tests" in p for p in eval_datasets.validate_fix(self.patch, self.abstain)))


class FixtureGateTest(unittest.TestCase):
    def test_safety_fixtures_cover_triage_and_abstain(self) -> None:
        triage = _load(eval_datasets.TRIAGE_PATH)
        abstain = _load(eval_datasets.ABSTAIN_PATH)
        fixtures = eval_datasets.safety_fixtures(triage, abstain)
        self.assertEqual(len(fixtures), len([s for s in triage["samples"] if s["safety_regression"]]) + len(abstain["samples"]))
        self.assertTrue(all(f["confidence"] >= 0.9 for f in fixtures))
        self.assertEqual(eval_datasets.check_fixtures(fixtures, 0.8), [])

    def test_infra_signal_must_be_blocked(self) -> None:
        fixtures = [
            {
                "source": "unit",
                "confidence": 0.99,
                "error_type": "DatabaseConnectionPoolExhausted",
                "summary": "Hikari 连接池耗尽",
                "suspect_files": ["db.py"],
                "files_found": ["db.py"],
                "evidence_status": "OK",
                "expect_route": "runbook",
            }
        ]
        self.assertEqual(eval_datasets.check_fixtures(fixtures, 0.8), [])

    def test_unblocked_infra_is_reported(self) -> None:
        fixtures = [
            {
                "source": "unit",
                "confidence": 0.99,
                "error_type": "DatabaseConnectionPoolExhausted",
                "summary": "Hikari 连接池耗尽",
                "suspect_files": ["db.py"],
                "files_found": ["db.py"],
                "evidence_status": "OK",
                "expect_route": "code_fix",  # 期望放行 → 实际拦截，应报告不一致
            }
        ]
        self.assertEqual(len(eval_datasets.check_fixtures(fixtures, 0.8)), 1)

    def test_run_reports_problems_for_broken_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "triage.json"
            payload = _load(eval_datasets.TRIAGE_PATH)
            payload["samples"] = payload["samples"][:3]
            payload["dataset_version"] = evalkit.canonical_hash(payload["samples"])
            broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = eval_datasets.run(
                triage_path=broken,
                fix_path=eval_datasets.FIX_PATH,
                abstain_path=eval_datasets.ABSTAIN_PATH,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("样本数" in p for p in result["problems"]))

    def test_missing_file_is_reported_not_raised(self) -> None:
        result = eval_datasets.run(triage_path=Path("/nonexistent/triage.json"))
        self.assertFalse(result["ok"])
        self.assertIn("数据集加载", result["checks"][0]["name"])


class BaselineEntryTest(unittest.TestCase):
    def test_write_and_reload_baseline(self) -> None:
        report = {
            "dataset_version": "v1",
            "metrics": {"top1_hit_rate": 0.9},
            "model": "m",
            "provider": "ollama",
            "code_commit": "abc",
            "generated_at": "now",
            "split": "holdout",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            evalkit.write_baseline(path, "triage", report)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["triage"]["metrics"]["top1_hit_rate"], 0.9)
            self.assertEqual(payload["triage_updated_at"], "now")

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            evalkit.write_baseline("/tmp/x.json", "unknown", {})


if __name__ == "__main__":
    unittest.main()
