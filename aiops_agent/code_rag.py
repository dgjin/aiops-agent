"""Code RAG（WP4 交付物）：AST 语义切块 + Ollama 嵌入 + 向量索引 + 余弦检索。

流程（对齐实施计划 WP4 任务 1/2）：
    build_index(repo_dir)
      → chunk_python_source：ast 解析按「函数/方法」语义切块（附行号区间）；
      → 历史工单语料（data/historical_tickets.json）以 kind=ticket 同库索引；
      → embed_texts：Ollama /api/embed 批量嵌入（默认 bge-m3）；
      → 持久化 data/code_index.json（向量 + 元数据）。

    search_index(query)
      → 嵌入查询文本 → 余弦相似度 top-k → 返回代码块 / 相似工单命中。

设计说明（WP4 增强：向量库升级）：
    - 双后端检索：FAISS 精确内积索引（IndexFlatIP + L2 归一化，内积 = 余弦，
      与纯 Python 排序同口径）与纯 Python 余弦；AIOPS_VECTOR_BACKEND 选择后端
      （auto 默认：FAISS 依赖可用即用）。索引持久化 <index>.faiss 与 JSON 并存：
      JSON 保留元数据与向量（可移植），.faiss 供高效检索；两者结果一致（回归测试保障）。
    - 显式 faiss 而依赖缺失 / 索引文件缺失 / 维度或行数不匹配时自动回退纯 Python，
      检索接口不变（生产可平滑替换为 Milvus 等托管向量库）。

环境变量：
    AIOPS_EMBED_MODEL       嵌入模型（默认 bge-m3:latest）
    AIOPS_OLLAMA_URL        Ollama 地址（默认 http://localhost:11434）
    AIOPS_VECTOR_BACKEND    向量后端：auto / faiss / python（默认 auto）
"""

from __future__ import annotations

import ast
import json
import math
import os
import time
import urllib.request
from pathlib import Path

DEFAULT_OLLAMA_URL = os.environ.get("AIOPS_OLLAMA_URL", "http://localhost:11434")
DEFAULT_EMBED_MODEL = os.environ.get("AIOPS_EMBED_MODEL", "bge-m3:latest")
DEFAULT_BACKEND = "auto"
BACKENDS = ("auto", "faiss", "python")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_REPO_DIR = BASE_DIR / "demo-app"
INDEX_PATH = DATA_DIR / "code_index.json"
TICKETS_PATH = DATA_DIR / "historical_tickets.json"


def chunk_python_source(source: str, file_path: str) -> list[dict]:
    """AST 语义切块：顶层函数与类方法各成一块（附行号区间），模块级赋值不入库。

    语法错误时抛 SyntaxError，由调用方决定跳过。
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    chunks: list[dict] = []

    def add_chunk(node: ast.AST, qualname: str, kind: str) -> None:
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        chunks.append(
            {
                "kind": kind,
                "file": file_path,
                "qualname": qualname,
                "start_line": start,
                "end_line": end,
                "docstring": ast.get_docstring(node) or "",
                "snippet": "\n".join(lines[start - 1 : end]),
            }
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_chunk(node, node.name, "function")
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_chunk(sub, f"{node.name}.{sub.name}", "method")
    return chunks


def embed_texts(
    texts: list[str],
    model: str | None = None,
    ollama_url: str | None = None,
) -> list[list[float]]:
    """Ollama /api/embed 批量嵌入（首次调用会触发模型加载，超时放宽）。"""
    payload = {"model": model or DEFAULT_EMBED_MODEL, "input": texts}
    req = urllib.request.Request(
        f"{(ollama_url or DEFAULT_OLLAMA_URL).rstrip('/')}/api/embed",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    embeddings = data.get("embeddings") or []
    if len(embeddings) != len(texts):
        raise RuntimeError(f"嵌入数量不匹配: 期望 {len(texts)} 实际 {len(embeddings)}")
    return embeddings


def _load_faiss():
    """惰性导入 FAISS（未安装返回 None；import 有系统级缓存，重复调用开销可忽略）。"""
    try:
        import faiss  # type: ignore[import-not-found]

        return faiss
    except ImportError:
        return None


def resolve_backend() -> str:
    """解析生效向量后端：python 强制纯 Python；auto/faiss 下 FAISS 依赖可用即 faiss，否则回退。"""
    requested = os.environ.get("AIOPS_VECTOR_BACKEND", DEFAULT_BACKEND).strip().lower()
    if requested == "python":
        return "python"
    return "faiss" if _load_faiss() is not None else "python"


def _build_faiss_index(vectors: list[list[float]]):
    """构建 FAISS 精确内积索引：L2 归一化后内积 = 余弦（与纯 Python 排序同口径）。"""
    import numpy as np

    faiss_lib = _load_faiss()
    matrix = np.asarray(vectors, dtype="float32")
    faiss_lib.normalize_L2(matrix)
    index = faiss_lib.IndexFlatIP(matrix.shape[1])
    index.add(matrix)
    return index


def _faiss_search(
    faiss_path: Path, index: dict, query_vec: list[float], top_k: int
) -> list[dict] | None:
    """FAISS 检索：归一化查询向量 → 内积 top-k（结果结构与 rank_chunks 一致）。

    任何不可用情形（依赖缺失 / 索引文件缺失 / 维度或行数不匹配 / 读取异常）返回 None，
    由调用方回退纯 Python 余弦，保证检索始终可用。
    """
    faiss_lib = _load_faiss()
    if faiss_lib is None or not faiss_path.is_file():
        return None
    try:
        import numpy as np

        faiss_index = faiss_lib.read_index(str(faiss_path))
        if faiss_index.d != len(query_vec) or faiss_index.ntotal != len(index["chunks"]):
            return None  # 陈旧索引（JSON 已重建或维度变更）→ 回退
        query = np.asarray([query_vec], dtype="float32")
        faiss_lib.normalize_L2(query)
        scores, positions = faiss_index.search(query, top_k)
    except Exception:  # noqa: BLE001 - 检索不可用时回退纯 Python，绝不中断
        return None
    results: list[dict] = []
    for similarity, position in zip(scores[0], positions[0]):
        pos = int(position)
        if pos < 0 or pos >= len(index["chunks"]):
            continue
        item = {key: value for key, value in index["chunks"][pos].items() if key != "vector"}
        item["similarity"] = round(float(similarity), 4)
        results.append(item)
    return results


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 Python 实现，规模小无需 numpy）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def rank_chunks(chunks: list[dict], query_vec: list[float], top_k: int = 5) -> list[dict]:
    """按余弦相似度排序取 top-k（纯计算，返回去掉向量的元数据 + similarity）。"""
    scored = [(cosine(query_vec, chunk["vector"]), chunk) for chunk in chunks]
    scored.sort(key=lambda item: item[0], reverse=True)
    results: list[dict] = []
    for similarity, chunk in scored[:top_k]:
        item = {key: value for key, value in chunk.items() if key != "vector"}
        item["similarity"] = round(similarity, 4)
        results.append(item)
    return results


def _ticket_chunk(ticket: dict) -> dict:
    snippet = (
        f"{ticket['error_type']}：{ticket['symptoms']} "
        f"根因：{ticket['root_cause']} 修复：{ticket['resolution']}"
    )
    return {
        "kind": "ticket",
        "file": "data/historical_tickets.json",
        "qualname": ticket["ticket_id"],
        "start_line": 0,
        "end_line": 0,
        "snippet": snippet,
    }


def build_index(
    repo_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    model: str | None = None,
    tickets_path: Path | str | None = None,
) -> dict:
    """建向量索引：代码 AST 切块 + 历史工单 → 批量嵌入 → 持久化 JSON。"""
    repo_dir = Path(repo_dir) if repo_dir else DEFAULT_REPO_DIR
    index_path = Path(index_path) if index_path else INDEX_PATH
    tickets_path = Path(tickets_path) if tickets_path else TICKETS_PATH

    chunks: list[dict] = []
    file_count = 0
    for py_file in sorted(repo_dir.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            file_chunks = chunk_python_source(
                py_file.read_text(encoding="utf-8"), str(py_file.relative_to(repo_dir))
            )
        except SyntaxError:
            continue
        chunks.extend(file_chunks)
        file_count += 1

    ticket_count = 0
    if tickets_path.exists():
        for ticket in json.loads(tickets_path.read_text(encoding="utf-8")):
            chunks.append(_ticket_chunk(ticket))
            ticket_count += 1

    if not chunks:
        raise RuntimeError(f"未找到可索引内容: {repo_dir}")

    texts = [
        f"{c['file']} :: {c['qualname']}\n{c.get('docstring', '')}\n{c['snippet']}" for c in chunks
    ]
    vectors = embed_texts(texts, model=model)
    used_model = model or DEFAULT_EMBED_MODEL

    indexed = [{**chunk, "vector": vector} for chunk, vector in zip(chunks, vectors)]
    index_path.parent.mkdir(parents=True, exist_ok=True)
    backend = resolve_backend()
    faiss_path: Path | None = None
    if backend == "faiss":
        try:
            faiss_path = index_path.with_suffix(".faiss")
            _load_faiss().write_index(_build_faiss_index(vectors), str(faiss_path))
        except Exception:  # noqa: BLE001 - 索引持久化失败回退纯 Python，不中断建库
            backend, faiss_path = "python", None

    index = {
        "model": used_model,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dim": len(vectors[0]),
        "repo": str(repo_dir),
        "backend": backend,
        "chunks": indexed,
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return {
        "files": file_count,
        "chunks": len(chunks),
        "tickets": ticket_count,
        "dim": index["dim"],
        "model": used_model,
        "backend": backend,
        "index": str(index_path),
        "faiss_index": str(faiss_path) if faiss_path else "",
    }


def search_index(
    query: str,
    index_path: Path | str | None = None,
    top_k: int = 5,
    model: str | None = None,
) -> list[dict]:
    """检索：查询文本嵌入后走生效后端（FAISS 可用且索引存在时）做 top-k，
    否则回退纯 Python 余弦。索引不存在时抛 FileNotFoundError。"""
    path = Path(index_path) if index_path else INDEX_PATH
    index = json.loads(path.read_text(encoding="utf-8"))
    query_vec = embed_texts([query], model=model or index.get("model"))[0]
    if resolve_backend() == "faiss":
        hits = _faiss_search(path.with_suffix(".faiss"), index, query_vec, top_k)
        if hits is not None:
            return hits
    return rank_chunks(index["chunks"], query_vec, top_k=top_k)
