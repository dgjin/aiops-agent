"""WP4 增强单测：FAISS 向量后端（一致性 / 回退）+ 修复模型选择（AIOPS_FIX_MODEL）。

覆盖：
    - build_index 在 FAISS 可用时产出 .faiss 并标记 backend=faiss；
    - FAISS 检索与纯 Python 余弦 top-k 一致（同口径回归，防双后端漂移）；
    - AIOPS_VECTOR_BACKEND=python 强制纯 Python（不产出 .faiss，结果不变）；
    - FAISS 依赖缺失 / 陈旧索引（行数不匹配）时自动回退，检索不中断；
    - run_fix 模型优先级：显式 model > DEFAULT_FIX_MODEL；timeout 透传。

运行：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import code_rag, fix_agent
from aiops_agent.models import Alert, RootCause

FAISS_AVAILABLE = code_rag._load_faiss() is not None

FAKE_QUERY_VEC = [0.9, 0.1, 0.0]
FAKE_CHUNK_VECTORS = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _fake_embed(texts: list[str], model: str | None = None, ollama_url: str | None = None):
    """确定性嵌入 mock：建库按块序给正交向量，检索给固定查询向量（离线零依赖）。"""
    if len(texts) == 1:
        return [FAKE_QUERY_VEC]
    return FAKE_CHUNK_VECTORS[: len(texts)]


class TestVectorBackendBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wp4-vec-"))
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / "a.py").write_text(
            "def a1():\n    return 1\n\n\ndef a2():\n    return 2\n", encoding="utf-8"
        )
        (repo / "b.py").write_text("def b1():\n    return 3\n", encoding="utf-8")
        self.repo = repo
        self.index_path = self.tmp / "idx" / "code_index.json"
        self.tickets_path = self.tmp / "no_tickets.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, backend: str | None = None) -> dict:
        env_patch = (
            mock.patch.dict(os.environ, {"AIOPS_VECTOR_BACKEND": backend})
            if backend
            else mock.patch.dict(os.environ, {})
        )
        with mock.patch.object(code_rag, "embed_texts", side_effect=_fake_embed), env_patch:
            return code_rag.build_index(
                repo_dir=self.repo, index_path=self.index_path, tickets_path=self.tickets_path
            )

    def _search(self, top_k: int = 3) -> list[dict]:
        with mock.patch.object(code_rag, "embed_texts", side_effect=_fake_embed):
            return code_rag.search_index("coupon 空值", index_path=self.index_path, top_k=top_k)


@unittest.skipUnless(FAISS_AVAILABLE, "FAISS 未安装（pip install faiss-cpu）")
class TestFaissBackend(TestVectorBackendBase):
    def test_build_creates_faiss_and_search_matches_python(self) -> None:
        stats = self._build()
        self.assertEqual(stats["backend"], "faiss")
        faiss_file = Path(stats["faiss_index"])
        self.assertTrue(faiss_file.is_file())
        self.assertEqual(faiss_file, self.index_path.with_suffix(".faiss"))

        hits = self._search(top_k=2)
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        expected = code_rag.rank_chunks(index["chunks"], FAKE_QUERY_VEC, top_k=2)
        self.assertEqual([h["qualname"] for h in hits], [e["qualname"] for e in expected])
        self.assertEqual(hits[0]["qualname"], "a1")  # 与查询向量最接近的块
        for hit, exp in zip(hits, expected):
            self.assertAlmostEqual(hit["similarity"], exp["similarity"], delta=1e-3)
            self.assertNotIn("vector", hit)

    def test_explicit_python_backend_skips_faiss_file(self) -> None:
        stats = self._build(backend="python")
        self.assertEqual(stats["backend"], "python")
        self.assertEqual(stats["faiss_index"], "")
        self.assertFalse(self.index_path.with_suffix(".faiss").exists())
        hits = self._search(top_k=1)
        self.assertEqual(hits[0]["qualname"], "a1")

    def test_python_and_faiss_search_agree(self) -> None:
        self._build()  # faiss 端
        hits_faiss = self._search(top_k=3)
        with mock.patch.dict(os.environ, {"AIOPS_VECTOR_BACKEND": "python"}):
            hits_python = self._search(top_k=3)
        self.assertEqual(
            [(h["qualname"], h["kind"]) for h in hits_faiss],
            [(h["qualname"], h["kind"]) for h in hits_python],
        )

    def test_stale_faiss_index_falls_back(self) -> None:
        """JSON 重建后 .faiss 行数不匹配：自动回退纯 Python，结果仍正确。"""
        self._build()
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        index["chunks"].append(
            {
                "kind": "code",
                "file": "c.py",
                "qualname": "c1",
                "start_line": 1,
                "end_line": 1,
                "snippet": "x",
                "vector": [0.0, 0.0, 1.0],
            }
        )
        self.index_path.write_text(json.dumps(index), encoding="utf-8")
        hits = self._search(top_k=1)
        self.assertEqual(hits[0]["qualname"], "a1")


class TestBackendFallback(TestVectorBackendBase):
    def test_missing_faiss_dependency_falls_back_to_python(self) -> None:
        with mock.patch.object(code_rag, "_load_faiss", return_value=None):
            stats = self._build()
        self.assertEqual(stats["backend"], "python")
        hits = self._search(top_k=1)
        self.assertEqual(hits[0]["qualname"], "a1")

    def test_search_without_faiss_file_falls_back(self) -> None:
        """索引为纯 Python 形态（无 .faiss）时，auto 后端检索仍可用。"""
        self._build(backend="python")
        hits = self._search(top_k=2)  # 默认 auto
        self.assertEqual(hits[0]["qualname"], "a1")
        self.assertEqual(len(hits), 2)


ALERT = Alert(alert_id="wp4-vec-1", service="order", description="错误率突增（模型选择单测）")
ROOT_CAUSE = RootCause(
    error_type="NullPointerException",
    suspect_files=["com/example/service/OrderService.java"],
    confidence=0.9,
    summary="coupon 空值解引用。",
)


class TestFixModelSelection(unittest.TestCase):
    """模型选择单测：**必须钉死 Ollama 提供者**。

    环境若配置 AIOPS_FIX_PROVIDER=qoder（.env 会被自动加载），run_fix 会走 Qoder 分支，
    绕过被 mock 的 call_ollama —— 既断言失败，又会真的调用 Qoder CLI（慢且消耗额度）。
    """

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"AIOPS_FIX_PROVIDER": "ollama"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_fix_model_used_when_no_explicit_model(self) -> None:
        with mock.patch.object(fix_agent, "DEFAULT_FIX_MODEL", "big-model"), mock.patch(
            "aiops_agent.fix_agent.call_ollama", return_value="not-json"
        ) as ollama:
            _, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertEqual(meta["model"], "big-model")
        self.assertEqual(ollama.call_args.kwargs["model"], "big-model")

    def test_explicit_model_and_timeout_override(self) -> None:
        with mock.patch.object(fix_agent, "DEFAULT_FIX_MODEL", "big-model"), mock.patch(
            "aiops_agent.fix_agent.call_ollama", return_value="not-json"
        ) as ollama:
            _, meta = fix_agent.run_fix(
                ALERT, ROOT_CAUSE, references=[], attempt=0, model="explicit-model", timeout=120
            )
        self.assertEqual(meta["model"], "explicit-model")
        self.assertEqual(ollama.call_args.kwargs["model"], "explicit-model")
        self.assertEqual(ollama.call_args.kwargs["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
