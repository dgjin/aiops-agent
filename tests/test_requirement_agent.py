"""需求分析器单测（requirement_agent）：prompt 构造 / 结构化输出 / 降级兜底。

安全侧原则的守门：分析器**绝不抛异常**——LLM 不可用 / 输出非法一律走 degraded
（confidence=0，不可批准），保证闭环节奏不被外部依赖中断。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from aiops_agent import requirement_agent

_ENTRY = {
    "id": 7,
    "kind": "REQUIREMENT",
    "title": "首页增加需求反馈入口",
    "content": "希望在首页顶端增加需求反馈入口链接",
    "priority": "P1",
    "assessment": "价值明确，纳入基线",
    "submitter": "张三",
    "department": "产品部",
    "baselineVersion": "v1.2",
}

_VALID_OUTPUT = {
    "understanding": "需要在首页顶部新增需求反馈入口，范围仅限前端入口与跳转。",
    "plan": ["在首页模板新增入口链接", "补充跳转测试"],
    "suspect_files": ["demo-app/http_server.py"],
    "acceptance": ["首页可见入口且可跳转"],
    "risk": "低风险，仅新增入口",
    "complexity": "低：单点改动",
    "confidence": 0.86,
}


class BuildPromptTest(unittest.TestCase):
    def test_prompt_carries_entry_feedback_previous_and_references(self) -> None:
        prompt = requirement_agent.build_analysis_prompt(
            _ENTRY,
            app={"name": "演示应用"},
            feedbacks=[
                {
                    "version": 2,
                    "feedback": "必须保持接口兼容",
                    "actor": "admin1",
                    "ts": "2026-10-09T00:00:00+00:00",
                }
            ],
            previous={
                "understanding": "旧理解",
                "plan": ["旧步骤"],
                "risk": "旧风险",
                "complexity": "中",
            },
            references=[
                {
                    "file": "demo-app/http_server.py",
                    "qualname": "handle_requirements",
                    "start_line": 1,
                    "end_line": 9,
                    "similarity": 0.91,
                }
            ],
        )
        self.assertIn("首页增加需求反馈入口", prompt)
        self.assertIn("演示应用", prompt)
        self.assertIn("必须保持接口兼容", prompt)
        self.assertIn("旧步骤", prompt)
        self.assertIn("demo-app/http_server.py::handle_requirements", prompt)
        self.assertIn('"confidence"', prompt)  # 输出 JSON 骨架

    def test_prompt_marks_first_round(self) -> None:
        prompt = requirement_agent.build_analysis_prompt(_ENTRY, app=None)
        self.assertEqual(prompt.count("（无，首轮分析）"), 2)  # 反馈与上一版均为空


class AnalyzeRequirementTest(unittest.TestCase):
    def test_success_returns_payload_and_meta(self) -> None:
        with mock.patch.object(
            requirement_agent,
            "call_ollama",
            return_value=json.dumps(_VALID_OUTPUT, ensure_ascii=False),
        ) as call:
            analysis, meta = requirement_agent.analyze_requirement(
                _ENTRY, app={"name": "演示应用"}, model="m1"
            )
        self.assertFalse(meta["degraded"])
        self.assertFalse(analysis["degraded"])
        self.assertEqual(analysis["understanding"], _VALID_OUTPUT["understanding"])
        self.assertEqual(analysis["confidence"], 0.86)
        self.assertEqual(analysis["plan"], _VALID_OUTPUT["plan"])
        self.assertGreaterEqual(meta["elapsed_seconds"], 0.0)
        self.assertEqual(call.call_args.kwargs["model"], "m1")

    def test_code_fence_stripped(self) -> None:
        fenced = "```json\n" + json.dumps(_VALID_OUTPUT, ensure_ascii=False) + "\n```"
        with mock.patch.object(requirement_agent, "call_ollama", return_value=fenced):
            analysis, meta = requirement_agent.analyze_requirement(_ENTRY)
        self.assertFalse(meta["degraded"])
        self.assertEqual(analysis["confidence"], 0.86)

    def test_llm_failure_degrades_not_raises(self) -> None:
        with mock.patch.object(
            requirement_agent, "call_ollama", side_effect=RuntimeError("ollama down")
        ):
            analysis, meta = requirement_agent.analyze_requirement(_ENTRY)
        self.assertTrue(meta["degraded"])
        self.assertTrue(analysis["degraded"])
        self.assertEqual(analysis["confidence"], 0.0)
        self.assertEqual(analysis["plan"], [])
        self.assertIn("ollama down", meta["reason"])

    def test_invalid_json_degrades(self) -> None:
        with mock.patch.object(requirement_agent, "call_ollama", return_value="not json at all"):
            analysis, meta = requirement_agent.analyze_requirement(_ENTRY)
        self.assertTrue(meta["degraded"])
        self.assertEqual(analysis["confidence"], 0.0)

    def test_missing_required_field_degrades(self) -> None:
        bad = {"plan": ["步骤"], "confidence": 0.5}  # 缺 understanding（必填）
        with mock.patch.object(
            requirement_agent, "call_ollama", return_value=json.dumps(bad)
        ):
            analysis, meta = requirement_agent.analyze_requirement(_ENTRY)
        self.assertTrue(meta["degraded"])

    def test_blank_list_items_filtered(self) -> None:
        payload = dict(_VALID_OUTPUT, plan=["步骤1", "  ", "", "步骤2"])
        with mock.patch.object(
            requirement_agent,
            "call_ollama",
            return_value=json.dumps(payload, ensure_ascii=False),
        ):
            analysis, _ = requirement_agent.analyze_requirement(_ENTRY)
        self.assertEqual(analysis["plan"], ["步骤1", "步骤2"])


if __name__ == "__main__":
    unittest.main()
