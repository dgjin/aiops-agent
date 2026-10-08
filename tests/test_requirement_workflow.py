"""需求驱动工作流单测：上下文构造（纯函数）与 signal/query 名称兼容。

兼容性契约：控制台按**名称**绑定 signal/query（temporalio Python SDK 行为），
需求工作流与告警工作流的同名定义使既有审批 / 发布指令交互零改动复用。
"""

from __future__ import annotations

import unittest

from aiops_agent.models import RequirementTask
from aiops_agent.workflows import (
    AIOpsFixWorkflow,
    AIOpsRequirementWorkflow,
    build_requirement_context,
)

_TASK = RequirementTask(
    analysis_id="ra-app-a-7",
    app_id="app-a",
    service="svc-a",
    entry_id="7",
    title="首页增加需求反馈入口",
    requirement="希望在首页顶端增加需求反馈入口链接",
    plan="1. 更新首页模板\n2. 补充跳转测试",
    acceptance=["首页可见入口", "点击可跳转"],
    suspect_files=["demo-app/http_server.py"],
    version=2,
    approved_by="admin1",
)


class BuildRequirementContextTest(unittest.TestCase):
    def test_alert_and_root_cause(self) -> None:
        alert, root_cause = build_requirement_context(_TASK)
        self.assertEqual(alert.alert_id, "req-7")
        self.assertEqual(alert.service, "svc-a")
        self.assertEqual(alert.severity, "requirement")
        self.assertIn("需求反馈入口", alert.description)
        self.assertIn("批准版实现方案（v2）", alert.description)
        self.assertIn("首页可见入口", alert.description)
        self.assertEqual(root_cause.error_type, "REQUIREMENT")
        self.assertEqual(root_cause.confidence, 1.0)
        self.assertEqual(root_cause.suspect_files, ["demo-app/http_server.py"])

    def test_empty_acceptance_marked(self) -> None:
        task = RequirementTask(**{**_TASK.__dict__, "acceptance": []})
        alert, _ = build_requirement_context(task)
        self.assertIn("（未列出）", alert.description)


class SignalCompatTest(unittest.TestCase):
    """控制台按名称绑定 signal/query：需求工作流必须与告警工作流同名。

    注：双下划线属性在类方法内须用 getattr(obj, "__x") 字符串形式访问——
    直接 obj.__x 会被 Python name mangling 改写为 _ClassName__x。
    """

    def test_signal_names_match(self) -> None:
        for name in (
            "submit_approval",
            "submit_second_approval",
            "submit_deploy_command",
            "submit_patch",
        ):
            fix = getattr(getattr(AIOpsFixWorkflow, name), "__temporal_signal_definition").name
            req = getattr(
                getattr(AIOpsRequirementWorkflow, name), "__temporal_signal_definition"
            ).name
            self.assertEqual(fix, req)
            self.assertEqual(req, name)

    def test_query_name_matches(self) -> None:
        fix = getattr(AIOpsFixWorkflow.status, "__temporal_query_definition").name
        req = getattr(AIOpsRequirementWorkflow.status, "__temporal_query_definition").name
        self.assertEqual(fix, "status")
        self.assertEqual(req, "status")

    def test_definition_names(self) -> None:
        self.assertEqual(
            getattr(AIOpsFixWorkflow, "__temporal_workflow_definition").name,
            "AIOpsFixWorkflow",
        )
        self.assertEqual(
            getattr(AIOpsRequirementWorkflow, "__temporal_workflow_definition").name,
            "AIOpsRequirementWorkflow",
        )


if __name__ == "__main__":
    unittest.main()
