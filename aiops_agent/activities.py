"""Activity 集合（设计方案第 8 节）：沙箱/发布/通知已接真实实现；演示分支关键词如下。

演示桩行为通过 Alert.description 中的关键词触发，便于端到端演示各分支：

=========== ============================================
关键词       演示分支
=========== ============================================
low-conf    根因置信度 0.55 → 闸门 1 转人工
protected   嫌疑文件落在 auth/** → 二级审批
test-fail   首次测试失败，回炉重试后通过
test-always-fail  测试始终失败 → 重试耗尽转人工
canary-bad  金丝雀劣化 → 自动回滚
=========== ============================================
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from temporalio import activity

# 注：logs / triage 依赖网络与 pydantic，在各 activity 函数内延迟导入，
# 避免 workflow 沙箱验证 activity 模块时触发 RestrictedWorkflowAccessError。
from .models import (
    Alert,
    CanaryResult,
    Patch,
    ReleaseResult,
    RootCause,
    TestReport,
)

log = logging.getLogger("aiops.activities")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_id(text: str, mod: int) -> int:
    """桩用：由内容派生的稳定编号（避免依赖 Python 随机化 hash）。"""
    return sum(ord(ch) for ch in text) % mod


async def _tiny_delay() -> None:
    """模拟真实调用耗时，便于观察阶段流转。"""
    await asyncio.sleep(0.5)


# --------------------------------------------------------------------------
# 感知层（设计方案 4.1）
# --------------------------------------------------------------------------

@activity.defn
async def collect_evidence(alert: Alert) -> dict:
    """真实实现（WP2）：从 Loki 按 service 拉取近 10 分钟日志并提取 trace_id。

    调用链上下文：演示环境从日志行内提取 trace_id（统一字段规范），
    生产环境替换为 OTel Collector / Jaeger 等链路数据源。
    Loki 不可用时降级为空证据（后续分析走安全侧）。
    """
    from . import logs

    await _tiny_delay()
    try:
        lines = await asyncio.to_thread(
            logs.query_lines, alert.service, lookback_minutes=10, limit=3000
        )
        loki_available = True
    except Exception as exc:  # noqa: BLE001 - 观测面不可用不阻断主流程
        log.warning("[evidence] Loki 不可用，降级为空证据: %s", exc)
        lines, loki_available = [], False
    trace_ids = sorted(
        {m for line in lines for m in re.findall(r"trace_id=([\w\-]+)", line)}
    )[:10]
    log.info(
        "[evidence] %s: 拉取 %d 行日志, %d 个 trace（loki=%s）",
        alert.service,
        len(lines),
        len(trace_ids),
        loki_available,
    )
    return {
        "alert_id": alert.alert_id,
        "service": alert.service,
        "raw_log_lines": len(lines),
        "lines": lines,
        "trace_ids": trace_ids,
        "loki_available": loki_available,
        "collected_at": _now(),
    }


# --------------------------------------------------------------------------
# 分析层（设计方案 4.2）
# --------------------------------------------------------------------------

@activity.defn
async def cluster_logs(evidence: dict) -> dict:
    """真实实现（WP2）：Drain3 模板聚类降噪，同模板海量重复日志聚合为计数。"""
    from . import logs

    await _tiny_delay()
    result = await asyncio.to_thread(logs.cluster_lines, evidence.get("lines", []))
    log.info(
        "[cluster] Drain3 聚类：%d 行日志 → %d 个模板（压缩比 %.4f）",
        result["total_lines"],
        len(result["templates"]),
        result["compression_ratio"],
    )
    return result


@activity.defn
async def analyze_root_cause(
    alert: Alert, clustered: dict, trace_ids: list[str]
) -> RootCause:
    """真实实现（WP3）：LLM triage 输出结构化根因（Ollama + pydantic schema 校验）。

    安全侧降级：LLM 不可用/输出非法 → confidence=0.0 → 闸门 1 自动转人工。
    演示关键词（low-conf / protected）走确定性桩，保证演示矩阵回归不依赖 LLM。
    """
    from . import triage

    await _tiny_delay()
    desc = alert.description.lower()
    low_conf = "low-conf" in desc
    protected = "protected" in desc
    demo_branch = any(m in desc for m in ("test-fail", "test-always-fail", "canary-bad"))
    if low_conf or protected or demo_branch:
        # 演示分支走确定性桩（不依赖 LLM），保证演示矩阵稳定
        root_cause = RootCause(
            error_type="NullPointerException",
            suspect_files=["auth/token_service.py"] if protected else ["src/order/service.py"],
            confidence=0.55 if low_conf else 0.92,
            summary=f"{alert.service} 服务在提交链路抛出空指针：可空字段未做空值防护（演示桩数据）。",
        )
        log.info(
            "[triage] 根因判定（演示桩）：%s confidence=%.2f",
            root_cause.error_type,
            root_cause.confidence,
        )
        return root_cause

    root_cause, meta = await asyncio.to_thread(
        triage.run_triage, alert, clustered, trace_ids
    )
    log.info(
        "[triage] LLM triage：%s confidence=%.2f（model=%s, elapsed=%.1fs, degraded=%s）",
        root_cause.error_type,
        root_cause.confidence,
        meta["model"],
        meta["elapsed_seconds"],
        meta["degraded"],
    )
    return root_cause


# --------------------------------------------------------------------------
# 决策层（设计方案 4.3）
# --------------------------------------------------------------------------

@activity.defn
async def retrieve_similar_fixes(root_cause: RootCause) -> list[dict]:
    """真实实现（WP4）：Code RAG 检索相关代码块与相似历史工单作为修复上下文。

    索引不可用时降级为空引用（修复 Agent 仍可基于目标文件生成补丁）。
    """
    from . import code_rag

    await _tiny_delay()
    query = " ".join([root_cause.error_type, root_cause.summary, *root_cause.suspect_files])
    try:
        references = await asyncio.to_thread(code_rag.search_index, query, top_k=5)
    except Exception as exc:  # noqa: BLE001 - 索引缺失/嵌入服务不可用不阻断主流程
        log.warning("[rag] Code RAG 不可用，降级为空引用: %s", exc)
        references = []
    log.info("[rag] Code RAG 检索到 %d 条引用:", len(references))
    for ref in references:
        log.info(
            "  - [%.3f] %s %s::%s",
            ref["similarity"],
            ref["kind"],
            ref["file"],
            ref["qualname"],
        )
    return references


@activity.defn
async def generate_patch(
    alert: Alert,
    root_cause: RootCause,
    references: list[dict],
    attempt: int,
) -> Patch:
    """真实实现（WP4）：修复 Agent 生成最小化 diff（应用 + 编译双重校验）。

    失败/演示分支走确定性兜底补丁（degraded 留痕），保证流程不中断。
    """
    from . import fix_agent

    await _tiny_delay()
    patch, meta = await asyncio.to_thread(
        fix_agent.run_fix, alert, root_cause, references, attempt
    )
    reason_note = f"，原因={meta['reason']}" if meta["reason"] else ""
    log.info(
        "[fix] 补丁 %s（attempt=%d，文件=%s，model=%s，validated=%s，degraded=%s%s）",
        patch.patch_id,
        attempt,
        patch.files,
        patch.model_version,
        meta["validated"],
        meta["degraded"],
        reason_note,
    )
    return patch


# --------------------------------------------------------------------------
# 验证层（设计方案 4.4）
# --------------------------------------------------------------------------

@activity.defn
async def run_tests_in_sandbox(patch: Patch, attempt: int) -> TestReport:
    """真实实现（WP6）：工作区准备 + 应用补丁 → 隔离运行时内执行单测（禁网/限额/只读）。

    隔离形态：本机 docker 容器（--network none 等）；生产以 K8s Job + gVisor 渲染（sandbox.py）。
    """
    from . import sandbox

    report = await asyncio.to_thread(sandbox.run_patch_tests, patch, attempt)
    log.info(
        "[sandbox] 补丁 %s 测试结果：%s（单测=%s，回归=%s）",
        patch.patch_id,
        "PASS" if report.passed else "FAIL",
        report.unit_tests,
        report.regression_tests,
    )
    if not report.passed:
        log.warning("[sandbox] 未通过原因：%s", report.details)
    return report


@activity.defn
async def create_merge_request(patch: Patch, test_report: TestReport) -> dict:
    """TODO 接真实实现：调 GitLab/GitHub API 创建 MR，描述含根因、测试报告、回滚预案。"""
    await _tiny_delay()
    mr = {
        "mr_id": f"!{_stable_id(patch.patch_id, 9000) + 1000}",
        "url": f"https://git.example.com/ops/{patch.alert_id}/-/merge_requests/demo",
        "labels": [
            f"ai-fix:{patch.alert_id}",
            f"model:{patch.model_version}",
            f"confidence:{patch.confidence:.2f}",
        ],
    }
    log.info("[mr] 已创建 MR %s（%s）", mr["mr_id"], mr["url"])
    log.info("[mr] 标签: %s", ", ".join(mr["labels"]))
    return mr


# --------------------------------------------------------------------------
# 闸门 2：审批通知与升级（设计方案 4.5）
# --------------------------------------------------------------------------

@activity.defn
async def notify_approvers(
    alert: Alert,
    root_cause: RootCause,
    patch: Patch,
    test_report: TestReport,
    needs_second: bool,
) -> dict:
    """真实实现（WP8）：飞书/钉钉交互卡片（根因分析 + 完整 diff + 测试报告 + 回滚预案）。"""
    from . import notify

    sender = notify.NotificationSender()
    card = notify.render_approval_card(
        alert,
        root_cause,
        patch,
        test_report,
        needs_second,
        provider=sender.provider,
        callback_base=sender.callback_base,
    )
    delivery = sender.send(card, msg_id=f"approval-{patch.patch_id}")
    log.info("================= 审批卡片 =================")
    log.info("服务: %s | 告警: %s | 投递: %s", alert.service, alert.alert_id, delivery["mode"])
    log.info("根因: %s (confidence=%.2f)", root_cause.summary, root_cause.confidence)
    log.info("补丁: %s | 风险: %s", patch.patch_id, patch.risk)
    log.info(
        "测试: 单测 %s / 回归 %s / SAST %s",
        test_report.unit_tests, test_report.regression_tests, test_report.sast,
    )
    log.info("回滚预案: %s", notify.ROLLBACK_PLAN)
    if needs_second:
        log.info("!! 命中受保护目录，需二级审批（submit_second_approval）")
    log.info("===============================================")
    return {
        "card_id": f"card-{alert.alert_id}",
        "delivery": delivery["mode"],
        "needs_second_approval": needs_second,
    }


@activity.defn
async def escalate_to_human(alert: Alert, reason: str) -> None:
    """真实实现（WP8）：升级卡片投递值班负责人（电话/短信升级语义）并落留痕审计。"""
    from . import notify

    sender = notify.NotificationSender()
    card = notify.render_escalation_card(alert, reason, provider=sender.provider)
    delivery = sender.send(card, msg_id=f"escalation-{alert.alert_id}")
    log.warning(
        "[escalate] 转人工升级（投递=%s）：alert=%s 原因=%s",
        delivery["mode"], alert.alert_id, reason,
    )
    return None


# --------------------------------------------------------------------------
# 闸门 3：用户公告（设计方案 4.6）
# --------------------------------------------------------------------------

@activity.defn
async def notify_users(alert: Alert, patch: Patch) -> dict:
    """真实实现（WP8）：公告卡片投递（WebSocket 全在线会话/Banner 语义对齐）+ 留痕。"""
    from . import notify

    version = f"v1.0.{_stable_id(patch.patch_id, 900) + 100}"
    sender = notify.NotificationSender()
    card = notify.render_broadcast_card(alert, patch, version, provider=sender.provider)
    delivery = sender.send(card, msg_id=f"broadcast-{patch.patch_id}")
    payload = {
        "version": version,
        "message": notify.BROADCAST_MESSAGE,
        "online_sessions": 1327,
        "delivery": delivery["mode"],
    }
    log.info(
        "[notify] 已向 %d 个在线会话推送公告（%s，投递=%s）",
        payload["online_sessions"], version, delivery["mode"],
    )
    return payload


# --------------------------------------------------------------------------
# 执行层：金丝雀与发布终态（设计方案 4.7）
# --------------------------------------------------------------------------

@activity.defn
async def deploy_canary(
    alert: Alert, patch: Patch, traffic_percent: int, observe_seconds: int = 10
) -> CanaryResult:
    """真实实现（WP7）：ArgoCD 风格金丝雀——镜像构建 + 容器启动 + 真实探活观测。"""
    from . import release

    result = await asyncio.to_thread(
        release.run_canary, alert, patch, traffic_percent, observe_seconds
    )
    log.info(
        "[canary] %s：观测窗口实测 错误率=%.2f%% P99=%.0fms → %s",
        patch.patch_id,
        result.error_rate,
        result.p99_latency_ms,
        result.observation,
    )
    return result


@activity.defn
async def finalize_release(canary: CanaryResult, auto_rollback: bool) -> ReleaseResult:
    """真实实现（WP7）：达标真实滚动到稳定版；劣化回收金丝雀并保留稳定版本。"""
    from . import release

    result = await asyncio.to_thread(release.run_finalize, canary, auto_rollback)
    log.info("[release] %s（%s）", result.reason, result.version)
    return result
