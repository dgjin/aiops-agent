"""Code RAG 与修复 Agent 单测（WP4）：离线零外部依赖（LLM 调用均 mock）。

覆盖：
    - chunk_python_source：真实文件切块（限定名/行号/docstring）、模块级函数、语法错误；
    - cosine / rank_chunks：纯计算排序与裁剪；
    - apply_unified_diff：应用成功（含围栏剥离）、上下文不匹配、垃圾输入；
    - validate_source：编译校验（对应验收「建议 diff 可编译」）；
    - locate_target_file：OrderService.java → order_service.py 模糊定位；
    - run_fix：LLM 正常路径（validated）/ 垃圾输出（degraded 兜底）/ 演示分支短路；
    - activities 集成：test-fail 短路、RAG 检索降级与命中。

运行：
    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from pydantic import ValidationError

from aiops_agent import activities, code_rag, fix_agent
from aiops_agent.models import Alert, RootCause, TestReport

ROOT_CAUSE = RootCause(
    error_type="NullPointerException",
    suspect_files=["com/example/service/OrderService.java"],
    confidence=0.95,
    summary="OrderService.submit 中 coupon 未被正确初始化或校验。",
)

ALERT_REAL = Alert(alert_id="wp4-t1", service="nl2sql", description="错误日志突增，已触发自动根因分析")


class TestChunkPythonSource(unittest.TestCase):
    def test_chunks_real_file(self) -> None:
        source = (code_rag.DEFAULT_REPO_DIR / "order_service.py").read_text(encoding="utf-8")
        chunks = code_rag.chunk_python_source(source, "order_service.py")
        names = [c["qualname"] for c in chunks]
        self.assertIn("OrderService.submit", names)
        self.assertIn("OrderService.cancel", names)
        submit = next(c for c in chunks if c["qualname"] == "OrderService.submit")
        self.assertGreater(submit["end_line"], submit["start_line"])
        self.assertIn("coupon", submit["snippet"])
        self.assertIn("提交订单", submit["docstring"])

    def test_module_level_functions(self) -> None:
        src = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
        chunks = code_rag.chunk_python_source(src, "m.py")
        self.assertEqual([c["qualname"] for c in chunks], ["foo", "bar"])
        self.assertEqual(chunks[0]["kind"], "function")

    def test_syntax_error_raises(self) -> None:
        with self.assertRaises(SyntaxError):
            code_rag.chunk_python_source("def broken(:\n", "bad.py")


class TestCosineAndRank(unittest.TestCase):
    def test_cosine(self) -> None:
        self.assertAlmostEqual(code_rag.cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(code_rag.cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(code_rag.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_rank_chunks_orders_and_truncates(self) -> None:
        chunks = [
            {"kind": "code", "file": "a", "qualname": "x", "start_line": 1, "end_line": 2, "snippet": "", "vector": [1.0, 0.0]},
            {"kind": "code", "file": "b", "qualname": "y", "start_line": 1, "end_line": 2, "snippet": "", "vector": [0.0, 1.0]},
            {"kind": "code", "file": "c", "qualname": "z", "start_line": 1, "end_line": 2, "snippet": "", "vector": [0.9, 0.1]},
        ]
        top = code_rag.rank_chunks(chunks, [1.0, 0.0], top_k=2)
        self.assertEqual([t["qualname"] for t in top], ["x", "z"])
        self.assertNotIn("vector", top[0])
        self.assertIn("similarity", top[0])


SAMPLE = 'def submit(payload):\n    coupon = fetch(payload)\n    discount = coupon["discount"]\n    return discount\n'
DIFF = (
    "--- a/s.py\n+++ b/s.py\n"
    "@@ -1,4 +1,7 @@\n"
    " def submit(payload):\n"
    "     coupon = fetch(payload)\n"
    "+    if coupon is None:\n"
    '+        raise ValueError("no coupon")\n'
    '     discount = coupon["discount"]\n'
    "     return discount\n"
)


class TestApplyUnifiedDiff(unittest.TestCase):
    def test_apply_success(self) -> None:
        new_text = fix_agent.apply_unified_diff(SAMPLE, DIFF)
        self.assertIsNotNone(new_text)
        self.assertIn("if coupon is None", new_text)
        ok, error = fix_agent.validate_source(new_text, "s.py")
        self.assertTrue(ok, error)

    def test_apply_with_code_fence(self) -> None:
        new_text = fix_agent.apply_unified_diff(SAMPLE, f"```diff\n{DIFF}```")
        self.assertIsNotNone(new_text)

    def test_context_mismatch_returns_none(self) -> None:
        bad = DIFF.replace("    return discount", "    return missing_var")
        self.assertIsNone(fix_agent.apply_unified_diff(SAMPLE, bad))

    def test_tolerates_inline_whitespace_difference(self) -> None:
        """LLM 改写行内对齐空格（注释前 2 空格变 3 空格）时仍可应用；上下文行保留源文本。"""
        src = "a = 1\nb = 2  # note\nc = 3\n"
        diff = (
            "--- a/s.py\n+++ b/s.py\n"
            "@@ -1,3 +1,4 @@\n"
            " a = 1\n"
            "-b = 2   # note\n"
            "+b = 20  # note\n"
            " c = 3\n"
        )
        new_text = fix_agent.apply_unified_diff(src, diff)
        self.assertIsNotNone(new_text)
        self.assertIn("b = 20", new_text)
        self.assertIn("c = 3", new_text)
        # 未修改的上下文行不得被 LLM 的空白改写污染
        self.assertIn("b = 20  # note", new_text)

    def test_rejects_indent_difference(self) -> None:
        """缩进不同的行不得宽松匹配（Python 语义敏感，防错位应用）。"""
        src = "def f():\n    return 1\n"
        diff = (
            "--- a/s.py\n+++ b/s.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def f():\n"
            "-        return 1\n"
            "+    return 2\n"
        )
        self.assertIsNone(fix_agent.apply_unified_diff(src, diff))

    def test_tolerates_skipped_blank_context_line(self) -> None:
        """LLM 省略上下文中的空行时，允许跳过源文件空白行继续匹配。"""
        src = "a = 1\n\nb = 2\nc = 3\n"
        diff = (
            "--- a/s.py\n+++ b/s.py\n"
            "@@ -1,4 +1,5 @@\n"
            " a = 1\n"
            "+x = 0\n"
            " b = 2\n"
            " c = 3\n"
        )
        new_text = fix_agent.apply_unified_diff(src, diff)
        self.assertIsNotNone(new_text)
        self.assertIn("x = 0", new_text)
        self.assertIn("b = 2", new_text)

    def test_tolerates_hunk_header_without_plus_section(self) -> None:
        """LLM 可能输出 `@@ -1,4 @@`（缺 +start,count）的非标准 hunk 头。"""
        diff = (
            "--- a/s.py\n+++ b/s.py\n"
            "@@ -1,4 @@\n"
            " def submit(payload):\n"
            "+    log(payload)\n"
            "     coupon = fetch(payload)\n"
            '     discount = coupon["discount"]\n'
            "     return discount\n"
        )
        new_text = fix_agent.apply_unified_diff(SAMPLE, diff)
        self.assertIsNotNone(new_text)
        self.assertIn("log(payload)", new_text)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(fix_agent.apply_unified_diff(SAMPLE, "not a diff at all"))
        self.assertIsNone(fix_agent.apply_unified_diff(SAMPLE, ""))


class TestValidateSource(unittest.TestCase):
    def test_syntax_error_reported(self) -> None:
        ok, error = fix_agent.validate_source("def broken(:\n", "bad.py")
        self.assertFalse(ok)
        self.assertIn("SyntaxError", error)


class TestPatchSchema(unittest.TestCase):
    def test_valid_payload(self) -> None:
        out = fix_agent.PatchOutput.model_validate(
            {"diff": "@@ -1 +1 @@", "description": "d", "risk": "低"}
        )
        self.assertEqual(out.description, "d")

    def test_missing_diff_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            fix_agent.PatchOutput.model_validate({"description": "d", "risk": "低"})

    def test_extra_fields_ignored(self) -> None:
        out = fix_agent.PatchOutput.model_validate(
            {"diff": "@@", "description": "d", "risk": "低", "extra": 1}
        )
        self.assertEqual(out.risk, "低")


class TestLocateTargetFile(unittest.TestCase):
    def test_java_class_maps_to_python_snake_case(self) -> None:
        target = fix_agent.locate_target_file(["com/example/service/OrderService.java"], code_rag.DEFAULT_REPO_DIR)
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "order_service.py")

    def test_unknown_suspect_returns_none(self) -> None:
        self.assertIsNone(
            fix_agent.locate_target_file(["com/example/PaymentGateway.java"], code_rag.DEFAULT_REPO_DIR)
        )
        self.assertIsNone(fix_agent.locate_target_file([], code_rag.DEFAULT_REPO_DIR))

    def test_prefers_best_token_overlap(self) -> None:
        """回归防护：src/order/service.py 应命中 order_service.py，而非 auth/token_service.py。"""
        target = fix_agent.locate_target_file(["src/order/service.py"], code_rag.DEFAULT_REPO_DIR)
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "order_service.py")

    def test_protected_dir_suspect_locates_auth_module(self) -> None:
        target = fix_agent.locate_target_file(["auth/token_service.py"], code_rag.DEFAULT_REPO_DIR)
        self.assertIsNotNone(target)
        self.assertEqual(str(target.relative_to(code_rag.DEFAULT_REPO_DIR)), "auth/token_service.py")


def _real_diff_for_order_service() -> str:
    """按 demo-app/order_service.py 当前内容动态构造行号正确的 diff（防文件微调破坏测试）。"""
    content = (code_rag.DEFAULT_REPO_DIR / "order_service.py").read_text(encoding="utf-8")
    lines = content.splitlines()
    anchor = next(i for i, line in enumerate(lines) if "discount = coupon" in line)  # 0-based
    hunk = lines[anchor - 3 : anchor + 3]  # 6 行上下文
    start = anchor - 2  # 1-based hunk 起点
    body = "".join(f" {line}\n" for line in hunk[:3])
    body += "+        if coupon is None:\n"
    body += '+            raise ValueError("无效优惠券")\n'
    body += "".join(f" {line}\n" for line in hunk[3:])
    return (
        f"--- a/order_service.py\n+++ b/order_service.py\n"
        f"@@ -{start},6 +{start},9 @@\n{body}"
    )


class TestRunFix(unittest.TestCase):
    def test_llm_validated_patch(self) -> None:
        response = json.dumps(
            {"diff": _real_diff_for_order_service(), "description": "增加 coupon 空值防护", "risk": "低：单点防御"},
            ensure_ascii=False,
        )
        with mock.patch("aiops_agent.fix_agent.call_ollama", return_value=response):
            patch, meta = fix_agent.run_fix(ALERT_REAL, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["validated"])
        self.assertFalse(meta["degraded"])
        self.assertEqual(patch.files, ["order_service.py"])
        self.assertEqual(patch.model_version, fix_agent.DEFAULT_MODEL)
        self.assertIn("if coupon is None", patch.diff)
        self.assertEqual(patch.confidence, ROOT_CAUSE.confidence)

    def test_llm_garbage_degrades_to_stub(self) -> None:
        with mock.patch("aiops_agent.fix_agent.call_ollama", return_value="完全不是 JSON"):
            patch, meta = fix_agent.run_fix(ALERT_REAL, ROOT_CAUSE, references=[], attempt=1)
        self.assertTrue(meta["degraded"])
        self.assertFalse(meta["validated"])
        self.assertEqual(patch.model_version, fix_agent.STUB_MODEL_VERSION)
        self.assertIn("coupon", patch.diff)

    def test_demo_branch_test_fail_stub(self) -> None:
        """演示分支（test-fail）：attempt=0 为沙箱将真实拦截的错误候选；attempt>=1 为修正版。"""
        alert = Alert(alert_id="wp4-t2", service="order", description="test-fail 演示")
        content = (code_rag.DEFAULT_REPO_DIR / "order_service.py").read_text(encoding="utf-8")

        bad, meta = fix_agent.run_fix(alert, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["stub"])
        self.assertEqual(bad.files, ["order_service.py"])
        bad_text = fix_agent.apply_unified_diff(content, bad.diff)
        self.assertIsNotNone(bad_text)  # diff 真实可应用（供沙箱执行）
        # 错误候选：校验被插入到解引用行之后 → 运行时仍抛 TypeError（由真实沙箱拦截）
        self.assertLess(
            bad_text.index('discount = coupon["discount"]'),
            bad_text.index("if coupon is None:"),
        )

        fixed, _ = fix_agent.run_fix(alert, ROOT_CAUSE, references=[], attempt=1)
        fixed_text = fix_agent.apply_unified_diff(content, fixed.diff)
        self.assertIsNotNone(fixed_text)
        self.assertIn('discount = coupon["discount"] if coupon else 0', fixed_text)

    def test_demo_branch_test_always_fail_stays_bad(self) -> None:
        """test-always-fail：所有 attempt 均为错误候选（沙箱持续失败 → 重试超限升级）。"""
        alert = Alert(alert_id="wp4-t4", service="order", description="test-always-fail 演示")
        for attempt in range(3):
            patch, meta = fix_agent.run_fix(alert, ROOT_CAUSE, references=[], attempt=attempt)
            self.assertTrue(meta["stub"])
            self.assertIn("if coupon is None", patch.diff)
            self.assertNotIn("if coupon else 0", patch.diff)

    def test_demo_branch_protected_keeps_protected_dir(self) -> None:
        alert = Alert(alert_id="wp4-t3", service="order", description="protected 演示")
        protected_root = RootCause(
            error_type="NullPointerException",
            suspect_files=["auth/token_service.py"],
            confidence=0.92,
            summary="受保护目录演示。",
        )
        patch, meta = fix_agent.run_fix(alert, protected_root, references=[], attempt=0)
        self.assertTrue(meta["stub"])
        self.assertEqual(patch.files, ["auth/token_service.py"])  # 供闸门 2 二级审批判定


class TestActivitiesWP4(unittest.TestCase):
    def test_mr_labels_cover_alert_model_confidence(self) -> None:
        """验收「标签字段完整」：MR 标签含 alert_id / model_version / confidence。"""
        alert = Alert(alert_id="wp4-t9", service="order", description="标签核查")
        patch = fix_agent.fallback_patch(alert, ROOT_CAUSE, attempt=0, target_rel="order_service.py")
        report = TestReport(patch_id=patch.patch_id, passed=True)
        mr = asyncio.run(activities.create_merge_request(patch, report))
        self.assertEqual(
            mr["labels"],
            [
                "ai-fix:wp4-t9",
                f"model:{patch.model_version}",
                f"confidence:{ROOT_CAUSE.confidence:.2f}",
            ],
        )

    def test_test_fail_shortcut(self) -> None:
        alert = Alert(alert_id="wp4-a1", service="order", description="test-fail 演示")
        root_cause = asyncio.run(activities.analyze_root_cause(alert, {"templates": []}, []))
        self.assertEqual(root_cause.confidence, 0.92)
        self.assertEqual(root_cause.suspect_files, ["src/order/service.py"])

    def test_retrieve_degrades_when_index_missing(self) -> None:
        with mock.patch("aiops_agent.code_rag.search_index", side_effect=FileNotFoundError("no index")):
            refs = asyncio.run(activities.retrieve_similar_fixes(ROOT_CAUSE))
        self.assertEqual(refs, [])

    def test_retrieve_returns_hits(self) -> None:
        hits = [
            {
                "kind": "ticket",
                "file": "data/historical_tickets.json",
                "qualname": "INC-2026-0101",
                "start_line": 0,
                "end_line": 0,
                "snippet": "NullPointerException …",
                "similarity": 0.84,
            }
        ]
        with mock.patch("aiops_agent.code_rag.search_index", return_value=hits):
            refs = asyncio.run(activities.retrieve_similar_fixes(ROOT_CAUSE))
        self.assertEqual(refs, hits)


if __name__ == "__main__":
    unittest.main()
