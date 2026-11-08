"""M5 确定性风险策略：等级、原因、摘要与发布准入文案（纯函数，无外部依赖）。

关键口径：
    - HIGH 只由硬触发（受保护目录 / 敏感路径 / 降级补丁）或权重累计决定，
      与 LLM 生成的 ``Patch.risk`` 文本无关；
    - 非真实测试（STATIC_ONLY/UNKNOWN）单独计 MEDIUM，但会保留原因；
    - 只有 HIGH 才要求真实测试与双人审批，MEDIUM/LOW 行为不变。
"""

from __future__ import annotations

import unittest

from aiops_agent import risk_policy


def _codes(assessment: dict) -> list[str]:
    return [item["code"] for item in assessment["reasons"]]


class AssessLevelTest(unittest.TestCase):
    def test_low_for_small_change_with_real_tests(self) -> None:
        result = risk_policy.assess(
            files=["src/app.py"],
            services=["order"],
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(result["level"], "LOW")
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["reasons"], [])
        self.assertFalse(result["requires_real_tests"])
        self.assertFalse(result["requires_second_approval"])

    def test_sensitive_path_forces_high_even_with_real_tests(self) -> None:
        for path, code in (
            ("demo-app/auth/login.py", "sensitive:auth"),
            ("demo-app/payment/refund.py", "sensitive:payment"),
            ("demo-app/rbac/roles.py", "sensitive:permission"),
            ("demo-app/crypto/private_key.pem", "sensitive:crypto"),
            ("migrations/0009_add_index.sql", "sensitive:migration"),
        ):
            with self.subTest(path=path):
                result = risk_policy.assess(
                    files=[path], services=["order"], verification_mode="UNIT_TEST"
                )
                self.assertEqual(result["level"], "HIGH")
                self.assertIn(code, _codes(result))
                self.assertTrue(result["requires_real_tests"])
                self.assertTrue(result["requires_second_approval"])

    def test_protected_pattern_is_high(self) -> None:
        result = risk_policy.assess(
            files=["demo-app/order_service.py"],
            services=["order"],
            protected_patterns=["demo-app/**"],
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(result["level"], "HIGH")
        self.assertEqual(_codes(result), ["protected-path"])
        self.assertIn("demo-app", result["reasons"][0]["detail"])

    def test_degraded_patch_is_high(self) -> None:
        result = risk_policy.assess(
            files=["src/app.py"],
            services=["order"],
            verification_mode="UNIT_TEST",
            degraded=True,
        )
        self.assertEqual(result["level"], "HIGH")
        self.assertEqual(_codes(result), ["degraded-patch"])

    def test_config_change_is_medium(self) -> None:
        result = risk_policy.assess(
            files=["deploy/compose.yaml"],
            services=["order"],
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(result["level"], "MEDIUM")
        self.assertEqual(result["score"], 2)
        self.assertIn("config-change", _codes(result))
        self.assertFalse(result["requires_real_tests"])

    def test_static_only_is_medium_with_reason(self) -> None:
        result = risk_policy.assess(
            files=["src/app.py"],
            services=["order"],
            verification_mode="STATIC_ONLY",
        )
        self.assertEqual(result["level"], "MEDIUM")
        self.assertIn("test-not-real", _codes(result))
        self.assertEqual(result["inputs"]["verification_mode"], "STATIC_ONLY")

    def test_missing_mode_is_unknown_not_safe(self) -> None:
        result = risk_policy.assess(files=["src/app.py"], services=["order"])
        self.assertEqual(result["level"], "MEDIUM")
        self.assertIn("test-not-real", _codes(result))
        self.assertEqual(result["inputs"]["verification_mode"], "UNKNOWN")

    def test_scope_weights_accumulate_to_high(self) -> None:
        result = risk_policy.assess(
            files=[f"src/module_{index}.py" for index in range(5)],
            services=["order", "pay"],
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(result["level"], "HIGH")
        self.assertEqual(result["score"], 4)
        self.assertEqual(_codes(result), ["multi-service", "wide-change"])

    def test_path_normalization_blocks_case_and_windows_bypass(self) -> None:
        result = risk_policy.assess(
            files=["Demo-App\\Auth\\Login.py"],
            services=["order"],
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(result["level"], "HIGH")
        self.assertIn("sensitive:auth", _codes(result))

    def test_extra_is_recorded_but_not_scored(self) -> None:
        baseline = risk_policy.assess(
            files=["src/app.py"], services=["order"], verification_mode="UNIT_TEST"
        )
        extended = risk_policy.assess(
            files=["src/app.py"],
            services=["order"],
            verification_mode="UNIT_TEST",
            extra={"note": "人工备注", "score": 99},
        )
        self.assertEqual(extended["level"], baseline["level"])
        self.assertEqual(extended["score"], baseline["score"])
        self.assertEqual(extended["inputs"]["extra"]["note"], "人工备注")

    def test_same_input_same_reasons_order(self) -> None:
        kwargs = dict(
            files=["demo-app/auth/login.py", "deploy/compose.yaml"],
            services=["order", "pay"],
            protected_patterns=["demo-app/**"],
            verification_mode="STATIC_ONLY",
        )
        first = risk_policy.assess(**kwargs)
        second = risk_policy.assess(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(
            _codes(first),
            [
                "protected-path",
                "sensitive:auth",
                "multi-service",
                "config-change",
                "test-not-real",
            ],
        )
        self.assertEqual(first["rule_version"], risk_policy.RULE_VERSION)

    def test_assess_does_not_mutate_inputs(self) -> None:
        files = ["demo-app/auth/login.py"]
        services = ["order"]
        patterns = ["demo-app/**"]
        risk_policy.assess(
            files=files, services=services, protected_patterns=patterns,
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(files, ["demo-app/auth/login.py"])
        self.assertEqual(services, ["order"])
        self.assertEqual(patterns, ["demo-app/**"])


class SummarizeTest(unittest.TestCase):
    def test_summary_contains_level_and_reason_codes(self) -> None:
        assessment = risk_policy.assess(
            files=["demo-app/auth/login.py", "deploy/compose.yaml"],
            services=["order"],
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(
            risk_policy.summarize(assessment), "HIGH:sensitive:auth,config-change"
        )

    def test_low_summary_without_codes(self) -> None:
        assessment = risk_policy.assess(
            files=["src/app.py"], services=["order"], verification_mode="UNIT_TEST"
        )
        self.assertEqual(risk_policy.summarize(assessment), "LOW")

    def test_unknown_or_invalid_returns_empty(self) -> None:
        self.assertEqual(risk_policy.summarize(None), "")
        self.assertEqual(risk_policy.summarize({}), "")
        self.assertEqual(risk_policy.summarize({"level": "CRITICAL"}), "")


class VerificationBlockTest(unittest.TestCase):
    def _high(self) -> dict:
        return risk_policy.assess(
            files=["demo-app/auth/login.py"],
            services=["order"],
            verification_mode="STATIC_ONLY",
        )

    def test_high_with_static_only_blocks(self) -> None:
        reason = risk_policy.verification_block(self._high(), "STATIC_ONLY")
        self.assertIn("发布准入", reason)
        self.assertIn("HIGH", reason)
        self.assertIn("STATIC_ONLY", reason)
        self.assertIn("转人工复核", reason)

    def test_high_with_unit_test_passes(self) -> None:
        self.assertEqual(risk_policy.verification_block(self._high(), "UNIT_TEST"), "")

    def test_high_with_missing_mode_blocks_as_unknown(self) -> None:
        reason = risk_policy.verification_block(self._high(), "")
        self.assertIn("UNKNOWN", reason)

    def test_medium_or_missing_assessment_passes(self) -> None:
        # config-change 单独 2 分；若再叠加 test-not-real（2 分）会累计为 HIGH，
        # 因此 MEDIUM 样本必须使用真实测试。
        medium = risk_policy.assess(
            files=["deploy/compose.yaml"],
            services=["order"],
            verification_mode="UNIT_TEST",
        )
        self.assertEqual(medium["level"], "MEDIUM")
        self.assertEqual(risk_policy.verification_block(medium, "STATIC_ONLY"), "")
        self.assertEqual(risk_policy.verification_block(None, "STATIC_ONLY"), "")


if __name__ == "__main__":
    unittest.main()
