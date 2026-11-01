"""PR 确定性评测门禁（M3）：不调用模型，可在每次 PR 执行。

检查项：
  1. 根因集完整性：≥100 条、ID 唯一、dev/holdout 覆盖、安全回归场景齐全、
     dataset_version 与内容哈希一致；
  2. 修复集完整性：修复 + 弃权合计 ≥60 条、弃权路由与字段齐全；
  3. 安全回归夹具：安全样本的「高置信模型输出」必须被组合判据拦截（route=runbook）；
  4. 代码根因放行：证据充分且文件可定位的代码根因不得被过度拦截。

任一检查失败以退出码 1 结束，便于 CI 直接阻断合并。
完整模型评测见 eval_triage.py / eval_fix.py（每日任务）。

用法：
    .venv/bin/python scripts/eval_gate.py
    .venv/bin/python scripts/eval_gate.py --threshold 0.8 --json /tmp/eval-gate.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from aiops_agent import eval_datasets  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="PR 确定性评测门禁（不调用模型）")
    parser.add_argument("--threshold", type=float, default=0.8, help="闸门 1 置信度阈值（默认 0.8）")
    parser.add_argument("--json", default="", help="可选：门禁结果写入 JSON 文件")
    args = parser.parse_args()

    result = eval_datasets.run(threshold=args.threshold)
    print(f"确定性评测门禁：阈值={args.threshold}")
    for check in result["checks"]:
        mark = "✓" if check["ok"] else "✗"
        print(f"  [{mark}] {check['name']}：{check['detail']}")
    if result["ok"]:
        print("结论：PASS")
    else:
        print(f"结论：FAIL（{len(result['problems'])} 项问题）")
        for problem in result["problems"]:
            print(f"  - {problem}")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"门禁结果已写入：{args.json}")
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
