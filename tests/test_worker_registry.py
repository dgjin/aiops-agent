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


if __name__ == "__main__":
    unittest.main()
