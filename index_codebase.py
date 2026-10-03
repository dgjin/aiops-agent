"""代码索引 CLI（WP4 交付物）：建库与检索验证。

用法：
    .venv/bin/python index_codebase.py                                   # 索引 demo-app + 历史工单
    .venv/bin/python index_codebase.py --query "NullPointerException coupon" --top-k 3
    .venv/bin/python index_codebase.py --repo demo-app --index data/code_index.json

输出：
    建库模式：文件数 / 块数 / 工单数 / 向量维度 / 索引路径；
    检索模式：top-k 命中（相似度、类型、文件::限定名、行号区间）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aiops_agent import code_rag


def main() -> None:
    parser = argparse.ArgumentParser(description="Code RAG 建索引 / 检索验证")
    parser.add_argument("--repo", default=None, help=f"代码仓库目录（默认 {code_rag.DEFAULT_REPO_DIR}）")
    parser.add_argument("--index", default=None, help=f"索引输出路径（默认 {code_rag.INDEX_PATH}）")
    parser.add_argument("--model", default=None, help=f"嵌入模型（默认 {code_rag.DEFAULT_EMBED_MODEL}）")
    parser.add_argument("--tickets", default=None, help=f"历史工单语料（默认 {code_rag.TICKETS_PATH}；传不存在的路径可排除）")
    parser.add_argument("--query", default=None, help="检索查询（给出则进入检索模式）")
    parser.add_argument("--top-k", type=int, default=5, help="返回条数（默认 5）")
    args = parser.parse_args()

    if args.query:
        hits = code_rag.search_index(
            args.query,
            index_path=Path(args.index) if args.index else None,
            top_k=args.top_k,
            model=args.model,
        )
        print(f"检索：{args.query!r} → {len(hits)} 条命中")
        for rank, hit in enumerate(hits, 1):
            location = f"{hit['file']}::{hit['qualname']}"
            if hit["kind"] == "code":
                location += f" (L{hit['start_line']}-{hit['end_line']})"
            first_line = hit["snippet"].splitlines()[0][:90] if hit["snippet"] else ""
            print(f"  [{rank}] sim={hit['similarity']:.4f} {hit['kind']} {location}")
            print(f"      {first_line}")
        return

    stats = code_rag.build_index(
        repo_dir=Path(args.repo) if args.repo else None,
        index_path=Path(args.index) if args.index else None,
        model=args.model,
        tickets_path=Path(args.tickets) if args.tickets else None,
    )
    print(
        f"索引完成：files={stats['files']} chunks={stats['chunks']} "
        f"tickets={stats['tickets']} dim={stats['dim']} model={stats['model']}"
    )
    print(f"索引文件：{stats['index']}")


if __name__ == "__main__":
    main()
