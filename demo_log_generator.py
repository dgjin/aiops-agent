"""演示日志生成器（WP2）：模拟应用日志并推送至 Loki（模拟 Filebeat 采集角色）。

生产环境替换为 Filebeat / Fluent Bit 采集真实应用日志；本脚本用于端到端演示：
    - normal 模式：混合正常业务日志（INFO 为主，少量 WARN）；
    - surge  模式：故障爆发时的大量重复 ERROR 日志（触发突增检测 + 聚类降噪演示）。

用法：
    python demo_log_generator.py --service nl2sql --mode normal --count 60
    python demo_log_generator.py --service nl2sql --mode surge  --count 800
"""

from __future__ import annotations

import argparse
import random

from aiops_agent import logs

_NORMAL_TEMPLATES = [
    "INFO  [trace_id={trace}] request completed endpoint=/query status=SUCCESS duration_ms={num}",
    "INFO  [trace_id={trace}] cache hit key=query:{num} ttl_s={num}",
    "INFO  [trace_id={trace}] sql executed tables={num} rows={num} elapsed_ms={num}",
    "WARN  [trace_id={trace}] llm retry attempt={num} engine=ollama",
]

_SURGE_TEMPLATES = [
    "ERROR [trace_id={trace}] java.lang.NullPointerException: coupon is null"
    " at com.example.service.OrderService.submit(OrderService.java:{num}) user_id={num}",
    "ERROR [trace_id={trace}] request failed endpoint=/query code=500"
    " duration_ms={num} retries={num}",
    "ERROR [trace_id={trace}] fallback triggered reason=upstream_error order_id=ORD{num}",
]


def _trace_id(rng: random.Random) -> str:
    return f"trace-{rng.randrange(16**8):08x}-{rng.randrange(1000):03d}"


def generate(service: str, mode: str, count: int, seed: int | None = None) -> list[str]:
    """按模式生成日志行（纯函数，seed 固定时确定性，便于测试）。"""
    rng = random.Random(seed)
    templates = _SURGE_TEMPLATES if mode == "surge" else _NORMAL_TEMPLATES
    lines: list[str] = []
    for _ in range(count):
        template = rng.choice(templates)
        lines.append(template.format(trace=_trace_id(rng), num=rng.randrange(10**4)))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="演示日志生成器（推送至 Loki）")
    parser.add_argument("--service", default="nl2sql", help="服务名（默认 nl2sql）")
    parser.add_argument("--mode", choices=["normal", "surge"], default="normal")
    parser.add_argument("--count", type=int, default=0, help="行数（默认：normal=60 / surge=800）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（复现用）")
    args = parser.parse_args()

    count = args.count or (800 if args.mode == "surge" else 60)
    level = "error" if args.mode == "surge" else "info"
    lines = generate(args.service, args.mode, count, args.seed)
    pushed = logs.push_lines(args.service, level, lines)
    print(
        f"已推送 {pushed} 行日志: service={args.service} mode={args.mode} "
        f"level={level} → {logs.DEFAULT_LOKI_URL}"
    )


if __name__ == "__main__":
    main()
