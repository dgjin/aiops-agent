"""Code RAG（WP4 交付物）：语义切块 + Ollama 嵌入 + 向量索引 + 余弦检索。

多语言（对齐「被监控应用」接入）：
    - ``.py``       → ``chunk_python_source``：ast 解析，函数/类方法级切块；
    - ``.ts/.tsx/.js/.jsx/.mjs/.cjs`` → ``chunk_ts_source``：声明（function/class/箭头函数）
      正则识别 + 花括号配平近似切块，未识别出声明时回退块切分。
    统一切块入口 ``chunk_source`` 按扩展名分派。

流程（对齐实施计划 WP4 任务 1/2）：
    build_index(repo_dir)
      → chunk_source：按语言语义切块（附行号区间）；
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
import re
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


def app_index_path(service: str) -> Path:
    """被监控应用专属索引路径约定：``data/code_index_<service>.json``。

    控制台登记应用的修复仓库（repo）后，修复检索自动指向该索引；缺失（或登记的
    仓库已变更）时由活动在首次检索前自动构建——无需人工执行 index_codebase.py
    或 export AIOPS_CODE_INDEX（该环境变量仍对**未注册服务**生效，见 search_index 回落链）。
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (service or "").strip().lower()).strip("-")
    return DATA_DIR / f"code_index_{slug or 'app'}.json"


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


# 代码切块支持的语言：Python 走 AST 语义切块；TS/JS 走声明切块（无原生解析器，用声明+花括号配平）
PYTHON_EXTS = (".py",)
TSJS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
CODE_EXTS = PYTHON_EXTS + TSJS_EXTS

# TS/JS 顶层声明：function / class
_TS_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?P<kind>function|class)\s+(?P<name>[A-Za-z_$][\w$]*)"
)
# TS/JS 顶层箭头函数 / 函数表达式：const name = (...) => {...} / const name = async () => ...
_TS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?"
    r"(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
)
# 类方法：缩进后的 name(...) { （排除控制流关键字）
_TS_METHOD = re.compile(
    r"^\s{2,}(?:(?:public|private|protected|static|readonly|async|override|get|set|abstract)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*(?::[^;{]+)?\{"
)
_TS_METHOD_SKIP = {"if", "for", "while", "switch", "catch", "return", "constructor"}


def _brace_block_end(lines: list[str], start: int) -> int:
    """从 start 行起做花括号配平，返回块结束行（0-based）；未配平则到文件末。

    简化实现：把字符串/注释内的花括号也计入（切块用途可接受，宁可块大不可漏块）。
    """
    depth = 0
    opened = False
    for i in range(start, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return i
    return len(lines) - 1


def _jsdoc_above(lines: list[str], start: int) -> str:
    """取紧邻声明上方的 JSDoc 注释（/** ... */）作为 docstring。"""
    i = start - 1
    if i < 0 or not lines[i].strip().endswith("*/"):
        return ""
    end = i
    while i >= 0 and "/**" not in lines[i]:
        i -= 1
    if i < 0:
        return ""
    block = "\n".join(lines[i : end + 1]).replace("/**", "").replace("*/", "")
    parts = [ln.strip().lstrip("*").strip() for ln in block.splitlines()]
    return "\n".join(p for p in parts if p).strip()


def chunk_ts_source(source: str, file_path: str, *, fallback_window: int = 40) -> list[dict]:
    """TS/JS 语义切块：顶层函数/类/箭头函数各成一块，类方法另行切块。

    无原生解析器，采用「声明正则 + 花括号配平」近似；若一个文件未识别出任何声明，
    回退为空白分隔的块切分（再退化到定长窗口），保证文件不整体丢失。
    """
    lines = source.splitlines()
    chunks: list[dict] = []

    def add(kind: str, qualname: str, start0: int, end0: int) -> None:
        chunks.append(
            {
                "kind": kind,
                "file": file_path,
                "qualname": qualname,
                "start_line": start0 + 1,
                "end_line": end0 + 1,
                "docstring": _jsdoc_above(lines, start0),
                "snippet": "\n".join(lines[start0 : end0 + 1]),
            }
        )

    class_names: list[str] = []
    for idx, line in enumerate(lines):
        decl = _TS_DECL.match(line)
        arrow = _TS_ARROW.match(line) if not decl else None
        if decl:
            name, block_kind = decl.group("name"), decl.group("kind")
            end = _brace_block_end(lines, idx)
            add("class" if block_kind == "class" else "function", name, idx, end)
            if block_kind == "class":
                class_names.append(name)
                for sub in range(idx + 1, end):
                    method = _TS_METHOD.match(lines[sub])
                    if method and method.group("name") not in _TS_METHOD_SKIP:
                        add(
                            "method",
                            f"{name}.{method.group('name')}",
                            sub,
                            _brace_block_end(lines, sub),
                        )
        elif arrow:
            add("function", arrow.group("name"), idx, _brace_block_end(lines, idx))

    if chunks:
        return chunks

    # 回退：按空行分块；整文件无空行时按定长窗口切
    block_start = 0
    for idx in range(len(lines) + 1):
        at_end = idx == len(lines)
        if at_end or not lines[idx].strip():
            if idx - block_start >= 5:
                add("block", f"block@{block_start + 1}", block_start, idx - 1)
            block_start = idx + 1
    if chunks:
        return chunks
    for start in range(0, len(lines), fallback_window):
        add("block", f"block@{start + 1}", start, min(start + fallback_window, len(lines)) - 1)
    return chunks


def chunk_source(source: str, file_path: str) -> list[dict]:
    """按扩展名分派代码切块；不支持的语言返回空列表。"""
    ext = Path(file_path).suffix.lower()
    if ext in PYTHON_EXTS:
        return chunk_python_source(source, file_path)
    if ext in TSJS_EXTS:
        return chunk_ts_source(source, file_path)
    return []


def embed_texts(
    texts: list[str],
    model: str | None = None,
    ollama_url: str | None = None,
) -> list[list[float]]:
    """Ollama /api/embed 批量嵌入（**分批请求**，首次调用会触发模型加载，超时放宽）。

    分批的必要性：接入真实仓库后块数可达数千，单次请求会过大且易超时。
    批大小由 ``AIOPS_EMBED_BATCH_SIZE`` 控制（默认 64）。
    """
    if not texts:
        return []
    size = int(os.environ.get("AIOPS_EMBED_BATCH_SIZE", "64"))
    url = f"{(ollama_url or DEFAULT_OLLAMA_URL).rstrip('/')}/api/embed"
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), size):
        batch = texts[start : start + size]
        req = urllib.request.Request(
            url,
            data=json.dumps({"model": model or DEFAULT_EMBED_MODEL, "input": batch}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
        part = data.get("embeddings") or []
        if len(part) != len(batch):
            raise RuntimeError(f"嵌入数量不匹配: 期望 {len(batch)} 实际 {len(part)}")
        embeddings.extend(part)
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
    skip_dirs = {"__pycache__", "node_modules", "dist", "build", ".venv", "coverage", "backups", "vendor"}
    for code_file in sorted(p for p in repo_dir.rglob("*") if p.suffix.lower() in CODE_EXTS):
        # 只按「相对仓库根」的路径判断，跳过依赖/产物/工具与 agent 配置目录（.agents/.claude/.qoder 等）
        rel_parts = code_file.relative_to(repo_dir).parts
        if any(part.startswith(".") or part in skip_dirs for part in rel_parts):
            continue
        try:
            source = code_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(code_file.relative_to(repo_dir))
        try:
            file_chunks = chunk_source(source, rel)
        except SyntaxError:  # Python 解析失败（文件损坏）→ 跳过
            continue
        if not file_chunks:
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
    否则回退纯 Python 余弦。索引不存在时抛 FileNotFoundError。

    索引路径优先级：显式参数 > 环境变量 ``AIOPS_CODE_INDEX`` > 默认 INDEX_PATH
    （便于把检索指向「被监控应用」的独立索引）。
    """
    env_index = os.environ.get("AIOPS_CODE_INDEX")
    path = Path(index_path) if index_path else Path(env_index) if env_index else INDEX_PATH
    index = json.loads(path.read_text(encoding="utf-8"))
    query_vec = embed_texts([query], model=model or index.get("model"))[0]
    if resolve_backend() == "faiss":
        hits = _faiss_search(path.with_suffix(".faiss"), index, query_vec, top_k)
        if hits is not None:
            return hits
    return rank_chunks(index["chunks"], query_vec, top_k=top_k)
