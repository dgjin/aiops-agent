"""组合放行判据（M3）：根因分类、必要条件、安全侧路由与事件留痕。"""

from __future__ import annotations

import unittest

from aiops_agent import gate_decision as gate


def _decide(**overrides) -> gate.Decision:
    params = {
        "confidence": 0.92,
        "threshold": 0.8,
        "error_type": "NullPointerException",
        "summary": "coupon 为 None 时未做空值防护",
        "suspect_files": ["order_service.py"],
        "evidence_status": "OK",
        "schema_valid": True,
        "files_found": ["order_service.py"],
        "files_missing": [],
        "scope_known": True,
    }
    params.update(overrides)
    return gate.decide(**params)


class ClassifyTest(unittest.TestCase):
    def test_code_root_cause(self) -> None:
        self.assertEqual(
            gate.classify_root_cause("NullPointerException", "coupon 为 None", ["order_service.py"]),
            gate.CODE,
        )

    def test_connection_pool_is_infrastructure(self) -> None:
        # S08 同类样本：高置信 + 基础设施根因，不得进入代码修复
        self.assertEqual(
            gate.classify_root_cause(
                "DatabaseConnectionPoolExhausted",
                "HikariPool-1 连接池耗尽，connection is not available",
                ["src/db/pool.py"],
            ),
            gate.INFRASTRUCTURE,
        )

    def test_gateway_502_is_infrastructure(self) -> None:
        # S11 同类样本：网关 502 上游失联
        self.assertEqual(
            gate.classify_root_cause("BadGateway", "网关 502，上游服务失联", ["gateway.conf"]),
            gate.INFRASTRUCTURE,
        )

    def test_config_parse_is_config(self) -> None:
        self.assertEqual(
            gate.classify_root_cause("ParserError", "YAML 配置解析失败，容器 CrashLoop", ["app.yaml"]),
            gate.CONFIG,
        )

    def test_empty_text_is_unknown(self) -> None:
        self.assertEqual(gate.classify_root_cause("", "", []), gate.UNKNOWN)


class DecideTest(unittest.TestCase):
    def test_all_conditions_pass_allows_code_fix(self) -> None:
        decision = _decide()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.route, gate.ROUTE_CODE_FIX)
        self.assertEqual(decision.events(), ["gate1:composite:pass"])
        codes = {r["code"] for r in decision.reasons}
        self.assertTrue({"schema_ok", "evidence_ok", "confidence_ok", "code_root_cause", "suspect_file_found"} <= codes)
        self.assertEqual(decision.labels["confidence_bucket"], "0.9-1.0")

    def test_low_confidence_blocks(self) -> None:
        decision = _decide(confidence=0.42)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.route, gate.ROUTE_HUMAN)
        self.assertIn("confidence_below_threshold", decision.blocked_codes)
        self.assertIn("gate1:composite:blocked:confidence_below_threshold", decision.events())

    def test_high_confidence_infra_root_cause_goes_runbook(self) -> None:
        decision = _decide(
            error_type="DatabaseConnectionPoolExhausted",
            summary="连接池耗尽，Hikari 等待超时",
            suspect_files=["src/db/pool.py"],
            files_found=["src/db/pool.py"],
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.route, gate.ROUTE_RUNBOOK)
        self.assertEqual(decision.category, gate.INFRASTRUCTURE)
        self.assertIn("non_code_root_cause:infrastructure", decision.blocked_codes)

    def test_config_root_cause_goes_runbook(self) -> None:
        decision = _decide(
            error_type="ParserError",
            summary="YAML 配置解析失败",
            suspect_files=["app.yaml"],
            files_found=["app.yaml"],
        )
        self.assertEqual(decision.route, gate.ROUTE_RUNBOOK)

    def test_missing_suspect_file_blocks(self) -> None:
        decision = _decide(files_found=[], files_missing=["order_service.py"])
        self.assertFalse(decision.allowed)
        self.assertIn("suspect_file_not_found", decision.blocked_codes)
        self.assertEqual(decision.route, gate.ROUTE_HUMAN)

    def test_no_suspect_files_blocks(self) -> None:
        decision = _decide(suspect_files=[], files_found=[], files_missing=[])
        self.assertIn("suspect_files_missing", decision.blocked_codes)

    def test_scope_unavailable_blocks(self) -> None:
        decision = _decide(scope_known=False, files_found=[], files_missing=[])
        self.assertIn("scope_unavailable", decision.blocked_codes)

    def test_schema_invalid_blocks(self) -> None:
        decision = _decide(schema_valid=False)
        self.assertIn("schema_invalid", decision.blocked_codes)

    def test_evidence_not_ok_blocks(self) -> None:
        decision = _decide(evidence_status="STALE_LOGS")
        self.assertIn("evidence_not_ok", decision.blocked_codes)

    def test_legacy_missing_evidence_status_stays_compatible(self) -> None:
        self.assertTrue(_decide(evidence_status=None).allowed)

    def test_unknown_root_cause_blocks(self) -> None:
        decision = _decide(error_type="", summary="", suspect_files=["x.py"], files_found=["x.py"])
        self.assertIn("root_cause_unknown", decision.blocked_codes)

    def test_suspect_file_requirement_can_be_relaxed_by_policy(self) -> None:
        decision = _decide(
            suspect_files=[],
            files_found=[],
            files_missing=[],
            require_suspect_files=False,
        )
        self.assertTrue(decision.allowed)

    def test_decision_roundtrip(self) -> None:
        payload = _decide(confidence=0.1).to_dict()
        self.assertFalse(payload["allowed"])
        self.assertIn("reasons", payload)
        self.assertIn("root_cause_category", payload["labels"])


class BucketTest(unittest.TestCase):
    def test_bucket_boundaries(self) -> None:
        self.assertEqual(gate.confidence_bucket(0.0), "0.0-0.5")
        self.assertEqual(gate.confidence_bucket(0.79), "0.7-0.8")
        self.assertEqual(gate.confidence_bucket(0.8), "0.8-0.9")
        self.assertEqual(gate.confidence_bucket(1.0), "0.9-1.0")


if __name__ == "__main__":
    unittest.main()
