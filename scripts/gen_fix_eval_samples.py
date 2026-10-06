"""Fix 评测样本生成器（P1-5 评测扩样）：实时采集夹具仓库基线并生成 33 个样本。

夹具仓库：data/fix_eval_repo（demo-app 缺陷并库 2 个 + 9 域 31 个批量注入缺陷）。
本脚本在夹具仓库内实时运行全量单测采集基线（恰 32 个失败用例），生成
data/fix_eval_samples.json 供 eval_fix.py 使用：

- fix_target_tests：每个样本的目标用例（修复后必须转为通过），生成时强校验其存在于
  基线失败集，且恰好被一个样本认领（无重复、无遗漏）；
- baseline_failed_tests = 全部基线失败用例 - 本人目标用例（本人目标修复后应通过；
  其余基线失败为已知状态，不计为新回归）；
- description 不得包含演示分支标记词（fix_agent 命中即走确定性短路）。

用法：
    .venv/bin/python scripts/gen_fix_eval_samples.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR / "data" / "fix_eval_repo"
OUT_PATH = BASE_DIR / "data" / "fix_eval_samples.json"

# 与 aiops_agent.fix_agent._DEMO_STUB_MARKERS 保持同步：命中即走确定性短路，样本必须避开
_DEMO_STUB_MARKERS = ("low-conf", "protected", "test-fail", "test-always-fail", "canary-bad")

_FAIL_LINE_RE = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)
_RAN_RE = re.compile(r"^Ran (\d+) tests", re.MULTILINE)


def collect_baseline_failed_tests(repo_dir: Path) -> tuple[list[str], int]:
    """在夹具仓库内运行全量单测，采集 (失败用例裸名列表, 用例总数)。

    沙箱与评测判定均按裸方法名（如 test_invoice_tier_boundary）解析失败用例，
    因此这里同样抓取 `FAIL: <name>` / `ERROR: <name>` 行的第一个词。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = proc.stdout + "\n" + proc.stderr
    failed = sorted(set(_FAIL_LINE_RE.findall(output)))
    if not failed:
        raise SystemExit(f"基线采集失败：未解析到失败用例（exit={proc.returncode}）\n{output[-2000:]}")
    ran = _RAN_RE.search(output)
    total = int(ran.group(1)) if ran else 0
    return failed, total


def _mk(
    sid: str,
    note: str,
    service: str,
    severity: str,
    description: str,
    error_type: str,
    summary: str,
    expect_file: str,
    targets: list[str],
    *,
    confidence: float = 0.9,
    suspect_files: list[str] | None = None,
) -> dict[str, Any]:
    """构造单个样本；alert_id 固定为 fix-eval-<序号>。"""
    return {
        "id": sid,
        "note": note,
        "alert": {
            "alert_id": f"fix-eval-{sid[1:].lower()}",
            "service": service,
            "severity": severity,
            "description": description,
        },
        "root_cause": {
            "error_type": error_type,
            "suspect_files": suspect_files or [expect_file],
            "confidence": confidence,
            "summary": summary,
        },
        "expect_file": expect_file,
        "fix_target_tests": targets,
    }


CATALOG: list[dict[str, Any]] = [
    # ---- 并库样本（与 demo-app 基线一致） ----
    _mk(
        "F01",
        "空优惠券解引用（order_service.py：无券码提交抛 TypeError，沙箱用例真实覆盖）",
        "order",
        "critical",
        "订单提交接口错误率突增，日志出现 NullPointerException: coupon is null at OrderService.submit",
        "NullPointerException",
        "payload 未携带 coupon_code 时 CouponClient.fetch 返回 None，随后 coupon['discount'] 下标访问抛异常。",
        "order_service.py",
        ["test_submit_without_coupon_code"],
        confidence=0.92,
        suspect_files=["com/example/service/OrderService.java"],
    ),
    _mk(
        "F02",
        "畸形令牌裸 ValueError（auth/token_service.py：expiry 段未校验格式，int() 抛裸异常；无直接用例覆盖，验收为无回归）",
        "auth",
        "warning",
        "令牌校验网关对畸形 token 返回 500，traceback 为裸 ValueError: invalid literal for int()",
        "ValueError",
        "verify 未校验 expiry 段数字格式，空串或非数字输入在 int(expiry_text) 处抛裸 ValueError，应转为领域异常 TokenError。",
        "auth/token_service.py",
        [],
        confidence=0.88,
    ),
    # ---- billing/invoice.py（A1-A4） ----
    _mk(
        "F03",
        "档位折扣边界（billing/invoice.py：恰好 1000 元未打折，应含边界）",
        "billing",
        "warning",
        "发票结算报表与预期不符：恰好满 1000 元档的订单未享受折扣，存在分级对账误差",
        "BoundaryError",
        "tier_discount 使用严格大于判断档位，金额恰好等于 TIER_THRESHOLD 时视为未达档返回 0.0；规则应为达到阈值（含）即享受折扣。",
        "billing/invoice.py",
        ["test_invoice_tier_boundary"],
        confidence=0.91,
    ),
    _mk(
        "F04",
        "发票金额浮点尾差（billing/invoice.py：未按 2 位小数四舍五入）",
        "billing",
        "warning",
        "对账系统检出发票总额存在浮点尾差（0.30000000000000004 类），分位金额未四舍五入",
        "FloatingPointDrift",
        "invoice_total 直接返回单价×数量，未按分（2 位小数）四舍五入，浮点尾差进入对账数据；应 round(total, 2)。",
        "billing/invoice.py",
        ["test_invoice_total_rounding"],
        confidence=0.9,
    ),
    _mk(
        "F05",
        "除零错误（billing/invoice.py：quantity=0 应抛 InvoiceError 而非 ZeroDivisionError）",
        "billing",
        "critical",
        "对账任务处理零数量发票时崩溃，traceback 为 ZeroDivisionError: division by zero",
        "ZeroDivisionError",
        "unit_price 未校验 quantity，为 0 时直接相除抛 ZeroDivisionError；应按领域约定抛 InvoiceError。",
        "billing/invoice.py",
        ["test_invoice_unit_price_zero_quantity"],
        confidence=0.93,
    ),
    _mk(
        "F06",
        "可选字段缺省崩溃（billing/invoice.py：tax 缺省应返回 0.0）",
        "billing",
        "warning",
        "批量开票任务对未配置税率的记录抛 KeyError: 'tax' 中断",
        "KeyError",
        "tax_amount 直接下标访问 record['tax']，tax 为可选字段，缺省时应按 0.0 返回（record.get('tax', 0.0)）。",
        "billing/invoice.py",
        ["test_invoice_tax_default"],
        confidence=0.9,
    ),
    # ---- inventory/stock.py（B1-B4） ----
    _mk(
        "F07",
        "负库存（inventory/stock.py：出库未校验库存不足）",
        "inventory",
        "warning",
        "库存台账出现负值告警：出库量超过当前库存仍未拦截",
        "InventoryNegative",
        "StockLedger.ship 未校验出库量，qty 超过当前库存时产生负库存；库存不足时应抛 InsufficientStock 且不改变库存。",
        "inventory/stock.py",
        ["test_stock_ship_rejects_insufficient"],
        confidence=0.92,
    ),
    _mk(
        "F08",
        "可变默认参数（inventory/stock.py：tags 跨调用共享）",
        "inventory",
        "warning",
        "库存标签接口返回的历史标签在多次调用间互相串扰，疑似跨请求状态泄漏",
        "SharedMutableState",
        "record 的 tags 参数使用可变默认值 []，多次调用共享同一列表造成标签串扰；应以 None 兜底创建独立列表。",
        "inventory/stock.py",
        ["test_stock_record_no_cross_call_leak"],
        confidence=0.9,
    ),
    _mk(
        "F09",
        "批次漏算（inventory/stock.py：range(len-1) 跳过最后一批）",
        "inventory",
        "warning",
        "月度盘点发现补货入库数量少于到货单：每笔补货的最后一批未计入库存",
        "OffByOneLoop",
        "restock 使用 range(len(lots) - 1) 遍历批次，最后一批未计入库存；应遍历全部批次。",
        "inventory/stock.py",
        ["test_stock_restock_counts_all_lots"],
        confidence=0.91,
    ),
    _mk(
        "F10",
        "环境变量类型错误（inventory/stock.py：阈值字符串与整数比较抛 TypeError）",
        "inventory",
        "critical",
        "货架宽度巡检任务崩溃：TypeError: '>' not supported between instances of 'int' and 'str'",
        "TypeError",
        "over_threshold 读取环境变量 STOCK_WIDTH_THRESHOLD 后未转 int，与整数 width 比较抛 TypeError；应按 int 数值转换（默认 100）。",
        "inventory/stock.py",
        ["test_stock_over_threshold_env_numeric"],
        confidence=0.92,
    ),
    # ---- pricing/discount.py（C1-C4） ----
    _mk(
        "F11",
        "优惠后负数金额（pricing/discount.py：未截 0 元下限）",
        "pricing",
        "warning",
        "营销大额券场景出现负的应付金额，财务流水异常",
        "NegativeAmount",
        "apply_fixed 直接在金额上减券面额，未做 0 元下限截断；优惠超过订单金额时应按 0 元计。",
        "pricing/discount.py",
        ["test_pricing_fixed_no_negative"],
        confidence=0.91,
    ),
    _mk(
        "F12",
        "百分比未截断（pricing/discount.py：percent 上限应为 1.0）",
        "pricing",
        "warning",
        "折扣活动出现负价订单：运营配置了超过 100% 的折扣比例未被截断",
        "PercentOverflow",
        "apply_percent 未将 percent 上限截断到 1.0，超过 100% 的折扣产生负金额；应按 min(percent, 1.0) 截断。",
        "pricing/discount.py",
        ["test_pricing_percent_clamp"],
        confidence=0.9,
    ),
    _mk(
        "F13",
        "空列表崩溃（pricing/discount.py：空档位应返回 0.0）",
        "pricing",
        "critical",
        "新上架商品无可用折扣档位时报价服务崩溃：ValueError: max() arg is an empty sequence",
        "ValueError",
        "best_tier 对空档位列表直接 max() 抛 ValueError；空列表应按 0.0 处理。",
        "pricing/discount.py",
        ["test_pricing_best_tier_empty"],
        confidence=0.92,
    ),
    _mk(
        "F14",
        "叠加顺序颠倒（pricing/discount.py：应先减固定券再打折）",
        "pricing",
        "warning",
        "结算金额与运营预期不符：固定券与百分比折扣叠加时结果偏大",
        "CalculationOrder",
        "stack 应用顺序颠倒：先打折再减固定券；正确规则为先应用固定金额券、再对余额应用百分比折扣。",
        "pricing/discount.py",
        ["test_pricing_stack_order"],
        confidence=0.9,
    ),
    # ---- resilience/retry.py（D1-D3） ----
    _mk(
        "F15",
        "重试次数多一次（resilience/retry.py：range(attempts+1)）",
        "gateway",
        "warning",
        "下游限流统计显示重试工具的总调用次数比配置多 1 次",
        "RetryCountMismatch",
        "run_with_retry 循环为 range(attempts + 1)，总调用次数比配置多 1；应恰为 attempts 次（含首次）。",
        "resilience/retry.py",
        ["test_retry_attempts_is_total_calls"],
        confidence=0.91,
    ),
    _mk(
        "F16",
        "异常掩盖（resilience/retry.py：应重抛原始异常而非 RetryExhausted）",
        "gateway",
        "warning",
        "上游故障排查困难：重试耗尽后收到的都是 RetryExhausted，原始异常链丢失",
        "ExceptionMasking",
        "重试耗尽后抛出 RetryExhausted，掩盖最后一次原始异常；应直接重抛最后一次的原始异常（raise last）。",
        "resilience/retry.py",
        ["test_retry_raises_original_error"],
        confidence=0.9,
    ),
    _mk(
        "F17",
        "退避单位错误（resilience/retry.py：秒误按毫秒缩小 1000 倍）",
        "gateway",
        "warning",
        "重试间隔监控异常：实际退避时间远小于配置的 base_delay（毫秒级）",
        "UnitMismatch",
        "time.sleep(base_delay / 1000) 把秒误按毫秒缩小 1000 倍；退避应等待 base_delay 秒。",
        "resilience/retry.py",
        ["test_retry_backoff_unit_seconds"],
        confidence=0.91,
    ),
    # ---- parsing/parser.py（E1-E4） ----
    _mk(
        "F18",
        "键未去空白（parsing/parser.py：key 应 strip）",
        "config",
        "warning",
        "配置解析结果出现带空格的键名，下游按精确键名查找失败",
        "WhitespaceKey",
        "parse_line 返回的 key 未去除两侧空白（如 ' host ' 保留空格）；应返回 key.strip()。",
        "parsing/parser.py",
        ["test_parser_key_trimmed"],
        confidence=0.9,
    ),
    _mk(
        "F19",
        "分钟换算缺失（parsing/parser.py：m 分支应 ×60）",
        "config",
        "warning",
        "超时配置解析后时长缩短：5m 被解析为 5 秒导致任务提前中断",
        "UnitConversion",
        "parse_duration 的分钟分支返回原始数值未换算；应以 amount * 60 转为秒。",
        "parsing/parser.py",
        ["test_parser_duration_minutes"],
        confidence=0.91,
    ),
    _mk(
        "F20",
        "编码错误（parsing/parser.py：应 utf-8 而非 ascii）",
        "ingest",
        "critical",
        "含中文的报文序列化失败：UnicodeEncodeError: 'ascii' codec can't encode characters",
        "UnicodeEncodeError",
        "dump_bytes 使用 ascii 编码，中文与 emoji 抛 UnicodeEncodeError；应采用 utf-8。",
        "parsing/parser.py",
        ["test_parser_dump_bytes_utf8"],
        confidence=0.92,
    ),
    _mk(
        "F21",
        "贪婪正则（parsing/parser.py：应取第一对方括号 .*?）",
        "ingest",
        "warning",
        '标签提取结果异常："a[host]b[port]" 提取出 "host]b[port" 而非 "host"',
        "RegexGreedy",
        "TAG_RE 为贪婪模式，多组方括号时匹配到最后一组；应使用非贪婪 \\[(.*?)\\] 取第一组。",
        "parsing/parser.py",
        ["test_parser_first_tag_greedy"],
        confidence=0.9,
    ),
    # ---- storage/kv.py（F1-F3） ----
    _mk(
        "F22",
        "写入覆盖全量（storage/kv.py：应合并单键而非重建字典）",
        "config-center",
        "warning",
        "配置中心多次写入后早前配置丢失，重启加载只剩最后一个键",
        "DataLoss",
        "KVStore.put 以 self._data = {key: value} 整体覆盖，历史键全部丢失；应 self._data[key] = value 合并写入。",
        "storage/kv.py",
        ["test_kv_two_writes_persist"],
        confidence=0.92,
    ),
    _mk(
        "F23",
        "类型未转换（storage/kv.py：get_int 应 int() 转换）",
        "config-center",
        "warning",
        '端口配置读取返回字符串 "8080"，下游网络调用抛出类型错误',
        "TypeCoercion",
        "get_int 直接返回原始值，字符串存储的数值未转换；应 int(raw) 后返回。",
        "storage/kv.py",
        ["test_kv_get_int_coercion"],
        confidence=0.9,
    ),
    _mk(
        "F24",
        "空键未拦截（storage/kv.py：key=None 应抛 ValueError）",
        "config-center",
        "warning",
        "上游漏传键名的告警刷屏：put(None) 被静默接受并持久化为 null 键",
        "ArgumentValidation",
        "KVStore.put 未校验 key 为 None 的情况；应抛 ValueError 拒绝写入。",
        "storage/kv.py",
        ["test_kv_none_key_rejected"],
        confidence=0.9,
    ),
    # ---- scheduling/window.py（G1-G3） ----
    _mk(
        "F25",
        "右端闭区间错误（scheduling/window.py：应为开区间）",
        "scheduler",
        "warning",
        "窗口调度边界重复触发：ts 恰好等于窗口右端时刻仍判定命中",
        "IntervalSemantics",
        "in_window 使用闭区间 start <= ts <= end，ts == end 被判定命中；规则为右开区间 [start, end)。",
        "scheduling/window.py",
        ["test_window_right_open_interval"],
        confidence=0.91,
    ),
    _mk(
        "F26",
        "换算系数错误（scheduling/window.py：*100 应 *60）",
        "scheduler",
        "warning",
        "定时任务窗口时长异常：分钟转秒结果比预期大 40 秒/分钟",
        "UnitConversion",
        "minutes_to_seconds 使用 minutes * 100；应为 * 60。",
        "scheduling/window.py",
        ["test_window_minutes_to_seconds"],
        confidence=0.92,
    ),
    _mk(
        "F27",
        "负数未拦截（scheduling/window.py：应 <= 0 均抛 WindowError）",
        "scheduler",
        "warning",
        "批量调度任务收到负时长配置未报错，窗口数量计算出现负数",
        "ArgumentValidation",
        "windows 仅拦截 seconds == 0，负时长未被校验；应 seconds <= 0 抛 WindowError。",
        "scheduling/window.py",
        ["test_window_negative_duration_rejected"],
        confidence=0.9,
    ),
    # ---- stats/aggregate.py（H1-H3） ----
    _mk(
        "F28",
        "空列表除零（stats/aggregate.py：空均值应 0.0）",
        "metrics",
        "critical",
        "监控大盘无数据时段崩溃：avg([]) 抛 ZeroDivisionError",
        "ZeroDivisionError",
        "avg 对空列表执行 sum/len 抛 ZeroDivisionError；空列表应按 0.0 处理。",
        "stats/aggregate.py",
        ["test_stats_avg_empty"],
        confidence=0.92,
    ),
    _mk(
        "F29",
        "偶数中位数错误（stats/aggregate.py：应取中间两值平均）",
        "metrics",
        "warning",
        "延迟统计报表中位数口径不符：偶数样本取的是上中位数而非两值平均",
        "CalculationMismatch",
        "median 对偶数个样本直接取 ordered[len//2]；应取中间两个值的平均。",
        "stats/aggregate.py",
        ["test_stats_median_even_count"],
        confidence=0.9,
    ),
    _mk(
        "F30",
        "百分位越界（stats/aggregate.py：p=100 索引越界）",
        "metrics",
        "critical",
        "SLO 看板 p100 分位计算崩溃：IndexError: list index out of range",
        "IndexError",
        "percentile 在 p=100 时索引等于列表长度导致 IndexError；索引应截断到 len - 1。",
        "stats/aggregate.py",
        ["test_stats_percentile_p100"],
        confidence=0.91,
    ),
    # ---- cache/lru.py（I1-I3） ----
    _mk(
        "F31",
        "None 值误判不存在（cache/lru.py：has 应按键存在性判断）",
        "session",
        "warning",
        "会话缓存中值为 None 的键被误判为不存在，触发重复回源",
        "SemanticsMismatch",
        "has 以 self._data.get(key) is not None 判存在，值为 None 的键被误判不存在；应按 key in self._data 判断。",
        "cache/lru.py",
        ["test_cache_has_none_value"],
        confidence=0.9,
    ),
    _mk(
        "F32",
        "读取未刷新顺序（cache/lru.py：get 应 move_to_end）",
        "session",
        "warning",
        "缓存命中率低于预期：高频读取的键被过早淘汰，访问顺序未刷新",
        "SemanticsMismatch",
        "get 命中后未调用 move_to_end 刷新使用顺序，热点键被当作最旧淘汰；应命中后 move_to_end(key)。",
        "cache/lru.py",
        ["test_cache_get_refreshes_order"],
        confidence=0.91,
    ),
    _mk(
        "F33",
        "过早淘汰（cache/lru.py：恰好等于容量时不得淘汰）",
        "session",
        "warning",
        "缓存容量为 2 时仅写入 2 个键就发生淘汰，命中率下降",
        "OffByOneEviction",
        "put 使用 len >= capacity 判定淘汰，恰好等于容量时错误淘汰最旧键；应为 len > capacity。",
        "cache/lru.py",
        ["test_cache_no_premature_eviction"],
        confidence=0.9,
    ),
]


def build_samples(failed: list[str]) -> list[dict[str, Any]]:
    """校验 catalog 与基线一致性，补齐 baseline_failed_tests 后返回样本列表。"""
    failed_set = set(failed)
    target_owner: dict[str, str] = {}
    samples: list[dict[str, Any]] = []

    ids = [item["id"] for item in CATALOG]
    if len(ids) != len(set(ids)):
        raise SystemExit("catalog 存在重复样本 ID")

    for item in CATALOG:
        sid = item["id"]
        targets = item["fix_target_tests"]
        for name in targets:
            if name not in failed_set:
                raise SystemExit(f"[{sid}] 目标用例不在基线失败集中: {name}")
            if name in target_owner:
                raise SystemExit(
                    f"目标用例被多个样本认领: {name}（{target_owner[name]} / {sid}）"
                )
            target_owner[name] = sid
        desc = item["alert"]["description"].lower()
        hits = [marker for marker in _DEMO_STUB_MARKERS if marker in desc]
        if hits:
            raise SystemExit(f"[{sid}] description 命中演示短路标记词: {hits}")
        samples.append({**item, "baseline_failed_tests": sorted(failed_set - set(targets))})

    unassigned = sorted(failed_set - set(target_owner))
    if unassigned:
        raise SystemExit(f"以下基线失败用例未被任何样本认领: {unassigned}")
    return samples


def main() -> None:
    failed, total = collect_baseline_failed_tests(REPO_DIR)
    samples = build_samples(failed)
    payload = {
        "description": (
            "P1-5 评测扩样：fix 评测夹具仓库（data/fix_eval_repo）的 33 个真实可修复样本。"
            "description 不得包含演示分支标记词（low-conf/protected/test-fail 等），否则会被确定性短路。"
            "沙箱口径：fix_target_tests 转为通过且不得引入基线之外的失败用例"
            "（baseline_failed_tests = 全部基线失败 - 本人目标）。"
        ),
        "generated_by": "scripts/gen_fix_eval_samples.py",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo_dir": "data/fix_eval_repo",
        "baseline_total_tests": total,
        "baseline_failed_count": len(failed),
        "samples": samples,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    targeted = sum(1 for s in samples if s["fix_target_tests"])
    claimed = sum(len(s["fix_target_tests"]) for s in samples)
    print(f"夹具仓库：{REPO_DIR}")
    print(f"基线：{total} 个用例，{len(failed)} 个失败")
    print(f"样本：{len(samples)} 个（有目标用例 {targeted} 个 + 无目标用例 {len(samples) - targeted} 个）")
    print(f"目标用例认领：{claimed}/{len(failed)}（无重复、无遗漏）")
    print(f"输出：{OUT_PATH}")


if __name__ == "__main__":
    main()
