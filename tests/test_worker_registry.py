"""Worker 活动注册表防回归测试（P1-2 集成漏洞的守门测试）。

背景：notify_shadow_suggestion 曾漏注册进 worker._ACTIVITY_LIST，导致 shadow 流程
在活动调用时 NotFoundError、重试耗尽后 workflow FAILED。本测试断言 worker 注册表
必须与 activities 模块中全部 @activity.defn 函数一一对应，防止再次漏注册。
"""

from __future__ import annotations

import unittest

from aiops_agent import activities, worker


def _activity_defn_names() -> set[str]:
    names: set[str] = set()
    for name in dir(activities):
        fn = getattr(activities, name)
        if getattr(fn, "__temporal_activity_definition", None) is not None:
            names.add(name)
    return names


class TestWorkerActivityRegistry(unittest.TestCase):
    def test_registry_covers_all_activity_defns(self) -> None:
        """注册表必须覆盖 activities 模块全部 @activity.defn（防漏注册）。"""
        registered = {fn.__name__ for fn in worker._ACTIVITY_LIST}
        self.assertEqual(_activity_defn_names(), registered)

    def test_registry_has_no_duplicates(self) -> None:
        names = [fn.__name__ for fn in worker._ACTIVITY_LIST]
        self.assertEqual(len(names), len(set(names)))


class TestWorkerWorkflowRegistry(unittest.TestCase):
    """工作流注册守门：_WORKFLOW_LIST 必须覆盖 workflows 模块全部 @workflow.defn。

    背景：新增工作流（如 AIOpsRequirementWorkflow）若漏进注册表，worker 收到任务时
    会报 NotFoundError 重试耗尽；本测试同活动注册守门，防止再次漏注册。
    """

    def test_registry_covers_all_workflow_defns(self) -> None:
        from aiops_agent import workflows

        names: set[str] = set()
        for name in dir(workflows):
            obj = getattr(workflows, name)
            if getattr(obj, "__temporal_workflow_definition", None) is not None:
                names.add(name)
        registered = {cls.__name__ for cls in worker._WORKFLOW_LIST}
        self.assertEqual(names, registered)

    def test_registry_has_no_duplicates(self) -> None:
        names = [cls.__name__ for cls in worker._WORKFLOW_LIST]
        self.assertEqual(len(names), len(set(names)))

    def test_requirement_workflow_registered(self) -> None:
        names = {cls.__name__ for cls in worker._WORKFLOW_LIST}
        self.assertIn("AIOpsRequirementWorkflow", names)


if __name__ == "__main__":
    unittest.main()
