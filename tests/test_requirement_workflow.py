"""需求驱动工作流单测：上下文构造（纯函数）与 signal/query 名称兼容。

兼容性契约：控制台按**名称**绑定 signal/query（temporalio Python SDK 行为），
需求工作流与告警工作流的同名定义使既有审批 / 发布指令交互零改动复用。
"""

from __future__ import annotations

import unittest

from aiops_agent import models
from aiops_agent.models import Patch, RequirementTask
from aiops_agent.workflows import (
    AIOpsFixWorkflow,
    AIOpsRequirementWorkflow,
    _attempt_failure_summary,
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


class AttemptFailureSummaryTest(unittest.TestCase):
    """升级提示具体化：单轮失败摘要按真实原因分类（生成降级 / SAST / 工作区准备 / 真实测试）。"""

    @staticmethod
    def _patch(**overrides) -> Patch:
        base = dict(
            patch_id="p-req-7-r2",
            alert_id="req-7",
            files=["server/query/metrics.ts"],
            diff="",
            description="",
            risk="",
            model_version="qoder",
            confidence=1.0,
        )
        return Patch(**{**base, **overrides})

    def test_degraded_patch_summary_carries_reason(self) -> None:
        """生成环节降级（如 Qoder 服务端故障）：摘要必须区分于「沙箱测试未通过」。"""
        patch = self._patch(
            degraded=True,
            degrade_reason="QoderFixError: Qoder 未对 server/query/metrics.ts 产生改动（exit=1）",
        )
        report = models.TestReport(
            patch_id=patch.patch_id,
            passed=False,
            unit_tests="未执行（补丁生成降级，非真实修复）",
        )
        summary = _attempt_failure_summary(patch, report)
        self.assertIn("补丁生成失败", summary)
        self.assertIn("exit=1", summary)
        self.assertNotIn("沙箱测试未通过", summary)

    def test_sast_report_summary(self) -> None:
        report = models.TestReport(
            patch_id="p1",
            passed=False,
            unit_tests="未执行（SAST 拦截）",
            details="静态扫描失败：eval 执行风险（attempt=0）",
        )
        self.assertIn("静态扫描拦截", _attempt_failure_summary(None, report))

    def test_workspace_prep_report_summary(self) -> None:
        report = models.TestReport(
            patch_id="p1",
            passed=False,
            unit_tests="未执行（工作区准备失败）",
            details="工作区准备失败：补丁无法应用（hunk 冲突，attempt=2）",
        )
        summary = _attempt_failure_summary(None, report)
        self.assertIn("工作区准备失败", summary)
        self.assertIn("hunk 冲突", summary)

    def test_real_test_failure_summary(self) -> None:
        report = models.TestReport(
            patch_id="p1",
            passed=False,
            unit_tests="FAILED tests/test_x.py::test_y - assert 1 == 2",
        )
        summary = _attempt_failure_summary(None, report)
        self.assertIn("沙箱测试未通过", summary)
        self.assertIn("test_y", summary)

    def test_summary_is_single_line_and_clipped(self) -> None:
        """超长 / 含换行的降级原因：摘要压平为单行并截断（事件串与升级文案保持可读）。"""
        patch = self._patch(degraded=True, degrade_reason=("x" * 500) + "\n第二行")
        summary = _attempt_failure_summary(patch, None)
        self.assertNotIn("\n", summary)
        self.assertLess(len(summary), 260)


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
