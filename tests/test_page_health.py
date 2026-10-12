"""page_health 页面判据单测：通用白屏检测（空响应/半截/空壳/挂载点缺失）与关键字精确模式。"""

from __future__ import annotations

import unittest

from aiops_agent.page_health import page_problem


class KeywordModeTest(unittest.TestCase):
    """配置关键字时精确包含（向后兼容，行为不变）。"""

    def test_hit_passes(self):
        self.assertEqual(page_problem('<div id="root">', keyword='<div id="root"'), "")

    def test_miss_reports_keyword(self):
        problem = page_problem('<div id="rroot">', keyword='<div id="root"')
        self.assertIn("缺少关键字", problem)

    def test_blank_keyword_falls_back_to_generic(self):
        # 纯空白关键字按未配置处理（通用判据）
        self.assertEqual(page_problem("<html><body><div>x</div></body></html>", keyword="   "), "")


class GenericModeTest(unittest.TestCase):
    """未配置关键字时的通用白屏判据（不依赖具体框架挂载点）。"""

    def test_empty_body_marks_white_screen(self):
        self.assertIn("为空", page_problem(""))
        self.assertIn("为空", page_problem("   "))

    def test_json_and_text_bodies_pass(self):
        self.assertEqual(page_problem('{"status": "ok"}'), "")
        self.assertEqual(page_problem("OK"), "")

    def test_normal_spa_page_passes(self):
        page = (
            "<!DOCTYPE html><html><head><title>t</title></head>"
            '<body><div id="root"></div>'
            '<script type="module" src="/src/main.tsx"></script></body></html>'
        )
        self.assertEqual(page_problem(page), "")

    def test_incomplete_html_marks(self):
        self.assertIn("未完整闭合", page_problem('<html><body><div id="root"></div>'))

    def test_empty_body_shell_marks(self):
        self.assertIn("为空", page_problem("<html><body></body></html>"))
        self.assertIn("为空", page_problem("<html><body> <!-- placeholder --> </body></html>"))

    def test_script_only_body_marks_mount_missing(self):
        page = (
            "<!DOCTYPE html><html><body>"
            '<script type="module" src="/src/main.tsx"></script></body></html>'
        )
        self.assertIn("无可见内容", page_problem(page))

    def test_head_only_page_marks(self):
        # 有 </html> 闭合但无 body 段、全文只有 title/head（均被排除）→ 判白屏
        self.assertIn("无可见内容", page_problem("<html><head><title>x</title></head></html>"))

    def test_truncated_skips_structure(self):
        # 读取被截断（大响应）：跳过结构判定，避免误报
        page = "<html><body><script>" + "x" * 65536
        self.assertEqual(page_problem(page, truncated=True), "")

    def test_doctype_uppercase_passes(self):
        page = "<!DOCTYPE HTML><HTML><BODY><DIV>x</DIV></BODY></HTML>"
        self.assertEqual(page_problem(page), "")


if __name__ == "__main__":
    unittest.main()
