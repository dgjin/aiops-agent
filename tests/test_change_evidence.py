"""审批证据契约（approval-evidence-v1）：哈希稳定性、绑定校验、职责分离与过期保护。"""

from __future__ import annotations

import unittest

from aiops_agent import change_evidence as ev


def _report(**overrides) -> dict:
    report = {
        "patch_id": "p-1",
        "passed": True,
        "unit_tests": "24/24 passed",
        "regression_tests": "120/120 passed",
        "sast": "clean",
        "details": "vitest",
        "verification_mode": "sandbox",
    }
    report.update(overrides)
    return report


def _binding(**overrides) -> dict:
    binding = ev.build_binding(
        case_id="alert-1",
        patch=type("P", (), {"patch_id": "p-1", "diff": "--- a\n+++ b\n"})(),
        test_report=_report(),
        policy_sha256="a" * 64,
        run_id="run-1",
    )
    binding.update(overrides)
    return binding


def _envelope(binding: dict, **overrides) -> dict:
    envelope = ev.build_envelope(
        binding=binding,
        gate="approval",
        decision="approve",
        actor="alice",
        channel="console",
        comment="",
        command_id="cmd-1",
        submitted_at="2026-01-01T00:00:00+00:00",
    )
    envelope.update(overrides)
    return envelope


class HashTest(unittest.TestCase):
    def test_diff_hash_is_stable_and_domain_separated(self) -> None:
        self.assertEqual(ev.diff_hash("same"), ev.diff_hash("same"))
        self.assertNotEqual(ev.diff_hash("same"), ev.diff_hash("same\n"))
        # 同一文本在不同领域前缀下不得相同（避免语义碰撞）
        self.assertNotEqual(ev.diff_hash("x"), ev._sha256("other-domain", "x"))
        self.assertEqual(len(ev.diff_hash("x")), 64)

    def test_none_diff_equals_empty_diff(self) -> None:
        self.assertEqual(ev.diff_hash(None), ev.diff_hash(""))

    def test_test_report_hash_detects_field_changes(self) -> None:
        self.assertEqual(ev.test_report_hash(_report()), ev.test_report_hash(_report()))
        self.assertNotEqual(
            ev.test_report_hash(_report()), ev.test_report_hash(_report(sast="finding"))
        )
        # 无关字段不参与哈希：BFF 多透传字段不应改变审批证据
        self.assertEqual(
            ev.test_report_hash(_report()), ev.test_report_hash(_report(extra="ignored"))
        )

    def test_missing_report_differs_from_empty_report(self) -> None:
        self.assertNotEqual(ev.test_report_hash(None), ev.test_report_hash({}))

    def test_dataclass_and_dict_agree(self) -> None:
        class Report:
            patch_id = "p-1"
            passed = True
            unit_tests = "24/24 passed"
            regression_tests = "120/120 passed"
            sast = "clean"
            details = "vitest"
            verification_mode = "sandbox"

        self.assertEqual(ev.test_report_hash(Report()), ev.test_report_hash(_report()))

    def test_canonical_json_sorted_and_compact(self) -> None:
        self.assertEqual(ev.canonical_json({"b": 1, "a": "中文"}), '{"a":"中文","b":1}')


class BindingTest(unittest.TestCase):
    def test_build_binding_normalizes_and_hashes(self) -> None:
        binding = _binding()
        self.assertEqual(binding["contract"], ev.CONTRACT)
        self.assertEqual(binding["diff_hash"], ev.diff_hash("--- a\n+++ b\n"))
        self.assertEqual(binding["test_report_hash"], ev.test_report_hash(_report()))
        self.assertIsNone(ev.binding_problem(binding))

    def test_binding_requires_all_fields(self) -> None:
        for key in ("case_id", "patch_id", "run_id", "policy_sha256"):
            binding = _binding(**{key: ""})
            self.assertIsNotNone(ev.binding_problem(binding), key)

    def test_binding_rejects_bad_hashes(self) -> None:
        for key in ("diff_hash", "test_report_hash", "policy_sha256"):
            self.assertIn("格式非法", ev.binding_problem(_binding(**{key: "nothex"})) or "")

    def test_binding_rejects_contract_mismatch(self) -> None:
        self.assertIsNotNone(ev.binding_problem(_binding(contract="approval-evidence-v0")))
        self.assertEqual(ev.binding_problem(None), "审批证据绑定缺失")

    def test_normalize_binding_drops_extra_keys(self) -> None:
        normalized = ev.normalize_binding(_binding(extra="x"))
        self.assertIsNotNone(normalized)
        self.assertNotIn("extra", normalized or {})
        self.assertIsNone(ev.normalize_binding({"contract": ev.CONTRACT}))

    def test_binding_from_status_requires_contract(self) -> None:
        status = {"approval_contract": ev.CONTRACT, "evidence_binding": _binding()}
        self.assertIsNotNone(ev.binding_from_status(status))
        # 旧流程（无契约）不得派生绑定，BFF 应回落字符串协议
        self.assertIsNone(ev.binding_from_status({"evidence_binding": _binding()}))
        self.assertIsNone(ev.binding_from_status({}))


class DecisionTest(unittest.TestCase):
    def test_reject_requires_comment(self) -> None:
        self.assertIsNotNone(ev.decision_problem("reject", ""))
        self.assertIsNotNone(ev.decision_problem("reject", "   "))
        self.assertIsNone(ev.decision_problem("reject", "配置项写错"))

    def test_approve_comment_optional(self) -> None:
        self.assertIsNone(ev.decision_problem("approve", ""))

    def test_invalid_decision_and_long_comment(self) -> None:
        self.assertIsNotNone(ev.decision_problem("maybe", ""))
        self.assertIn("过长", ev.decision_problem("reject", "x" * (ev.MAX_COMMENT + 1)) or "")


class EnvelopeStructureTest(unittest.TestCase):
    def test_valid_envelope(self) -> None:
        self.assertIsNone(ev.envelope_problem(_envelope(_binding())))

    def test_missing_envelope_is_explicit(self) -> None:
        # 新流程收到旧字符串协议时必须 fail-closed 并提示升级，而不是静默按旧逻辑执行
        problem = ev.envelope_problem("approve")
        self.assertEqual(problem and problem[0], "missing-envelope")

    def test_contract_mismatch(self) -> None:
        problem = ev.envelope_problem(_envelope(_binding(), contract="v0"))
        self.assertEqual(problem and problem[0], "contract-mismatch")

    def test_structure_errors(self) -> None:
        cases = {
            "invalid-command-id": {"command_id": ""},
            "invalid-gate": {"gate": "deploy"},
            "invalid-decision": {"decision": "ok"},
            "missing-actor": {"actor": " "},
            "invalid-channel": {"channel": "sms"},
            "comment-too-long": {"comment": "x" * (ev.MAX_COMMENT + 1)},
            "invalid-submitted-at": {"submitted_at": "yesterday"},
        }
        for code, overrides in cases.items():
            problem = ev.envelope_problem(_envelope(_binding(), **overrides))
            self.assertEqual(problem and problem[0], code, overrides)

    def test_missing_evidence(self) -> None:
        for key in (
            "case_id",
            "patch_id",
            "run_id",
            "diff_hash",
            "test_report_hash",
            "policy_sha256",
        ):
            problem = ev.envelope_problem(_envelope(_binding(), **{key: ""}))
            self.assertEqual(problem and problem[0], "missing-evidence", key)


class WorkflowValidationTest(unittest.TestCase):
    def test_accepts_matching_envelope(self) -> None:
        binding = _binding()
        self.assertIsNone(
            ev.validate_envelope(_envelope(binding), gate="approval", binding=binding)
        )

    def test_wrong_gate(self) -> None:
        binding = _binding()
        problem = ev.validate_envelope(
            _envelope(binding), gate="second_approval", binding=binding
        )
        self.assertEqual(problem and problem[0], "wrong-gate")

    def test_invalid_workflow_binding(self) -> None:
        problem = ev.validate_envelope(
            _envelope(_binding()), gate="approval", binding={"contract": ev.CONTRACT}
        )
        self.assertEqual(problem and problem[0], "invalid-binding")

    def test_already_decided(self) -> None:
        binding = _binding()
        problem = ev.validate_envelope(
            _envelope(binding), gate="approval", binding=binding, already_decided=True
        )
        self.assertEqual(problem and problem[0], "already-decided")

    def test_second_approval_preconditions(self) -> None:
        binding = _binding()
        envelope = _envelope(binding, gate="second_approval")
        self.assertEqual(
            ev.validate_envelope(
                envelope, gate="second_approval", binding=binding, needs_second=False
            )[0],
            "second-not-required",
        )
        self.assertEqual(
            ev.validate_envelope(
                envelope, gate="second_approval", binding=binding, needs_second=True
            )[0],
            "first-not-approved",
        )
        self.assertEqual(
            ev.validate_envelope(
                envelope,
                gate="second_approval",
                binding=binding,
                needs_second=True,
                first_approval={"decision": "approve", "actor": "alice"},
                second_decided=True,
            )[0],
            "already-decided",
        )
        self.assertIsNone(
            ev.validate_envelope(
                envelope,
                gate="second_approval",
                binding=binding,
                needs_second=True,
                # 一级审批人不同于二级审批人（职责分离）
                first_approval={"decision": "approve", "actor": "bob"},
            )
        )

    def test_stale_evidence_rejected(self) -> None:
        binding = _binding()
        stale = {
            "run_id": ("run-2", "run-mismatch"),
            "case_id": ("alert-2", "case-mismatch"),
            "patch_id": ("p-2", "patch-mismatch"),
            "diff_hash": ("b" * 64, "diff-mismatch"),
            "test_report_hash": ("c" * 64, "test-mismatch"),
            "policy_sha256": ("d" * 64, "policy-mismatch"),
        }
        for key, (value, code) in stale.items():
            # 旧 diff：客户端基于过期补丁提交，必须拒绝
            envelope = _envelope(binding, **{key: value})
            problem = ev.validate_envelope(envelope, gate="approval", binding=binding)
            self.assertEqual(problem and problem[0], code, key)

    def test_self_approval_rejected(self) -> None:
        binding = _binding()
        problem = ev.validate_envelope(
            _envelope(binding, actor="alice"),
            gate="approval",
            binding=binding,
            requested_by="alice",
        )
        self.assertEqual(problem and problem[0], "self-approval")

    def test_same_approver_on_second_gate_rejected(self) -> None:
        binding = _binding()
        envelope = _envelope(binding, gate="second_approval", actor="alice")
        problem = ev.validate_envelope(
            envelope,
            gate="second_approval",
            binding=binding,
            requested_by="bob",
            first_approval={"decision": "approve", "actor": "alice"},
            needs_second=True,
        )
        self.assertEqual(problem and problem[0], "same-approver")

    def test_different_requested_by_is_allowed(self) -> None:
        binding = _binding()
        self.assertIsNone(
            ev.validate_envelope(
                _envelope(binding, actor="alice"),
                gate="approval",
                binding=binding,
                requested_by="bob",
            )
        )


class RecordTest(unittest.TestCase):
    def test_approval_record_carries_full_provenance(self) -> None:
        binding = _binding()
        envelope = _envelope(binding, comment="可以发布")
        record = ev.approval_record(envelope, received_at="2026-01-01T00:00:05+00:00")
        for key in (
            "gate",
            "decision",
            "actor",
            "channel",
            "comment",
            "command_id",
            "submitted_at",
            "received_at",
            "contract",
            "case_id",
            "patch_id",
            "diff_hash",
            "test_report_hash",
            "policy_sha256",
            "run_id",
        ):
            self.assertIn(key, record)
        self.assertEqual(record["comment"], "可以发布")
        self.assertEqual(record["run_id"], binding["run_id"])

    def test_envelope_digest_detects_tampering(self) -> None:
        binding = _binding()
        base = _envelope(binding)
        self.assertEqual(ev.envelope_digest(base), ev.envelope_digest(_envelope(binding)))
        self.assertNotEqual(
            ev.envelope_digest(base), ev.envelope_digest(_envelope(binding, actor="mallory"))
        )


class ExpectedEvidenceTest(unittest.TestCase):
    def test_matching_expectations_pass(self) -> None:
        binding = _binding()
        self.assertIsNone(
            ev.expected_evidence_problem(
                binding,
                expected_diff_hash=binding["diff_hash"],
                expected_run_id=binding["run_id"],
            )
        )
        # 旧客户端不传期望值时不阻断（绑定仍由工作流二次校验）
        self.assertIsNone(ev.expected_evidence_problem(binding))

    def test_stale_page_rejected(self) -> None:
        binding = _binding()
        self.assertIn(
            "刷新", ev.expected_evidence_problem(binding, expected_diff_hash="f" * 64) or ""
        )
        self.assertIn(
            "刷新", ev.expected_evidence_problem(binding, expected_run_id="run-2") or ""
        )
        self.assertIn(
            "案件", ev.expected_evidence_problem(binding, expected_case_id="alert-2") or ""
        )

    def test_unusable_binding_rejected(self) -> None:
        self.assertIsNotNone(ev.expected_evidence_problem({}))


class HelperTest(unittest.TestCase):
    def test_command_id_format_and_uniqueness(self) -> None:
        first = ev.new_command_id()
        self.assertTrue(first.startswith("cmd-"))
        self.assertEqual(len(first), 4 + 32)
        self.assertNotEqual(first, ev.new_command_id())

    def test_utc_now_iso_parses(self) -> None:
        self.assertFalse(ev._iso_problem(ev.utc_now_iso()))
        self.assertTrue(ev._iso_problem("yesterday"))


if __name__ == "__main__":
    unittest.main()
