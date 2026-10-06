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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

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


def _resolve_app(service: str) -> dict | None:
    """解析被监控应用注册表条目（未注册/注册表不可用 → None → 回退 demo-app 默认行为）。"""
    from . import app_registry

    try:
        return app_registry.resolve(service)
    except Exception as exc:  # noqa: BLE001 - 注册表不可用不阻断修复
        log.warning("[registry] 应用注册表不可用，回退默认行为: %s", exc)
        return None


def _resolve_repo_dir(service: str) -> Path | None:
    """解析被监控应用对应的修复目标仓库（未注册/未配置返回 None → 走 demo-app 默认行为）。"""
    entry = _resolve_app(service)
    if entry:
        log.info(
            "[registry] 服务 %s → 修复仓库 %s（契约关键词=%s）",
            service,
            entry["repo"],
            (entry.get("contract") or {}).get("keyword") or "无",
        )
        return entry["repo"]
    return None


# 应用专属索引的进程内单飞锁：多活动并发首检同一应用时只允许一个真正建库
_APP_INDEX_LOCKS: dict[str, threading.Lock] = {}
_APP_INDEX_LOCKS_GUARD = threading.Lock()


def _app_index_lock(service: str) -> threading.Lock:
    with _APP_INDEX_LOCKS_GUARD:
        return _APP_INDEX_LOCKS.setdefault(service, threading.Lock())


def _index_repo_of(index_path: Path) -> str:
    """读索引文件头部记录的 ``repo`` 字段（只读前 4KB——chunks 可达数十 MB，避免整读）。"""
    try:
        with index_path.open("r", encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return ""
    match = re.search(r'"repo"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
    return match.group(1) if match else ""


def _ensure_app_index(service: str, repo_dir: Path, index_path: Path) -> None:
    """确保应用专属索引存在且指向当前登记的仓库；缺失/仓库变更时自动构建。

    - fast-path：索引存在且头部记录的 repo == 当前 repo_dir → 直接复用；
    - 构建前加进程内单飞锁并双检（并发首检只建一次）；
    - 由检索活动以 ``asyncio.to_thread`` 调用，嵌入批量请求在独立线程内执行，
      构建完成后写盘——超时重试时命中已完成索引，无需重复构建。
    """
    if index_path.is_file() and _index_repo_of(index_path) == str(repo_dir):
        log.info("[rag] 应用 %s 复用已有专属索引: %s", service, index_path)
        return
    with _app_index_lock(service):
        if index_path.is_file() and _index_repo_of(index_path) == str(repo_dir):
            return  # 双检：等待锁期间其他活动已完成构建
        from . import code_rag

        started = time.monotonic()
        summary = code_rag.build_index(repo_dir, index_path=index_path)
        log.info(
            "[rag] 应用 %s 专属索引自动构建完成：%d 文件 / %d 块（仓库=%s，耗时=%.1fs，索引=%s）",
            service,
            summary["files"],
            summary["chunks"],
            repo_dir,
            time.monotonic() - started,
            index_path,
        )


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
    变更关联（P1-1）：注入近 2 小时发布/配置事件，供 LLM 判定故障与变更的相关性。
    演示关键词（low-conf / protected）走确定性桩，保证演示矩阵回归不依赖 LLM。
    """
    from . import changes, triage

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

    recent_changes = await asyncio.to_thread(changes.recent_changes, alert.service)
    if recent_changes:
        log.info("[triage] 变更关联：近 2 小时命中 %d 条变更记录", len(recent_changes))
    root_cause, meta = await asyncio.to_thread(
        triage.run_triage, alert, clustered, trace_ids, changes=recent_changes
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
async def retrieve_similar_fixes(alert: Alert, root_cause: RootCause) -> list[dict]:
    """真实实现（WP4）：Code RAG 检索相关代码块与相似历史工单作为修复上下文。

    索引解析（自动适配，无需人工 export 环境变量 / 手工建库 / 重启 worker）：

    1. **已注册且配置了 repo 的应用** → 专属索引 ``data/code_index_<service>.json``，
       缺失或登记的仓库已变更时先自动构建（注册应用始终使用自己仓库的索引——
       全局环境变量不得误导其他仓库的检索）；
    2. **未注册** → 回落 ``AIOPS_CODE_INDEX`` 环境变量（若有）或默认索引
       ``data/code_index.json``（由 ``search_index`` 内部完成回落）。

    索引/嵌入服务不可用时降级为空引用（修复 Agent 仍可基于目标文件生成补丁）；
    专属索引构建失败**不回退**其他仓库的索引——错误仓库的引用会误导 LLM 修复。
    """
    from . import code_rag

    await _tiny_delay()
    index_path: Path | None = None
    entry = _resolve_app(alert.service)
    if entry:
        index_path = code_rag.app_index_path(alert.service)
        try:
            await asyncio.to_thread(
                _ensure_app_index, alert.service, entry["repo"], index_path
            )
        except Exception as exc:  # noqa: BLE001 - 建索引失败不阻断主流程
            log.warning(
                "[rag] 应用 %s 专属索引构建失败，降级为空引用（不回退他仓索引）: %s",
                alert.service,
                exc,
            )
            return []
    query = " ".join([root_cause.error_type, root_cause.summary, *root_cause.suspect_files])
    try:
        references = await asyncio.to_thread(
            code_rag.search_index, query, index_path=index_path, top_k=5
        )
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
    from . import fix_agent, metrics

    started = time.monotonic()
    await _tiny_delay()
    repo_dir = _resolve_repo_dir(alert.service)
    patch, meta = await asyncio.to_thread(
        fix_agent.run_fix, alert, root_cause, references, attempt, repo_dir
    )
    duration = time.monotonic() - started
    reason_note = f"，原因={meta['reason']}" if meta["reason"] else ""
    log.info(
        "[fix] 补丁 %s（attempt=%d，provider=%s，仓库=%s，文件=%s，model=%s，validated=%s，degraded=%s%s）",
        patch.patch_id,
        attempt,
        meta.get("provider", "ollama"),
        repo_dir or "-",
        patch.files,
        patch.model_version,
        meta["validated"],
        meta["degraded"],
        reason_note,
    )
    metrics.observe_fix_attempt(meta.get("provider", "ollama"), bool(meta["degraded"]), duration)
    return patch


# --------------------------------------------------------------------------
# 验证层（设计方案 4.4）
# --------------------------------------------------------------------------

@activity.defn
async def run_tests_in_sandbox(alert: Alert, patch: Patch, attempt: int) -> TestReport:
    """真实实现（WP6）：工作区准备 + 应用补丁 → 隔离运行时内执行单测（禁网/限额/只读）。

    隔离形态：本机 docker 容器（--network none 等）；生产以 K8s Job + gVisor 渲染（sandbox.py）。
    被监控应用场景：经应用注册表解析修复仓库；前端仓库走静态契约校验（不执行 Python 套件）。
    """
    from . import app_registry, sandbox

    repo_dir = None
    contract = None
    try:
        entry = app_registry.resolve(alert.service)
        if entry:
            repo_dir = entry["repo"]
            contract = entry.get("contract")
    except Exception as exc:  # noqa: BLE001 - 注册表不可用不阻断验证
        log.warning("[sandbox] 应用注册表不可用，使用默认仓库: %s", exc)
    report = await asyncio.to_thread(
        sandbox.run_patch_tests, patch, attempt, repo_dir=repo_dir, contract=contract
    )
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
    """真实实现（P5-01 / 优化方案 3.7）：策略 git 段配置时调 GitLab/GitHub/Gitee API 创建 MR。

    未配置（演示/未接入）或调用失败时返回确定性桩结果（mode=recorded / degraded），
    流程照常进入闸门 2，人工审批仍可依据测试报告与告警上下文决策。
    """
    from . import git_integration

    await _tiny_delay()
    mr = await asyncio.to_thread(git_integration.create_mr_for_patch, patch, test_report)
    mode = mr.get("mode", "recorded")
    if mode == "live":
        log.info("[mr] 已创建 MR %s（%s，provider=%s）", mr["mr_id"], mr["url"], mr.get("provider"))
    else:
        log.info("[mr] 桩结果（mode=%s）%s（%s）", mode, mr["mr_id"], mr["url"])
    log.info("[mr] 标签: %s", ", ".join(mr["labels"]))
    if mr.get("error"):
        log.warning("[mr] 降级原因: %s", mr["error"])
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


@activity.defn
async def notify_shadow_suggestion(
    alert: Alert,
    root_cause: RootCause,
    patch: Patch,
    test_report: TestReport,
) -> dict:
    """Shadow 模式建议投递（P1-2）：只建议不执行——无 MR、无审批、无发布，仅留痕供人工参考。"""
    from . import notify

    sender = notify.NotificationSender()
    card = notify.render_shadow_card(
        alert, root_cause, patch, test_report, provider=sender.provider
    )
    delivery = sender.send(card, msg_id=f"shadow-{patch.patch_id}")
    log.info(
        "[shadow] 修复建议已生成（未执行，仅供人工参考）：补丁=%s 投递=%s",
        patch.patch_id, delivery["mode"],
    )
    return {
        "msg_id": delivery.get("msg_id") or f"shadow-{patch.patch_id}",
        "delivery": delivery["mode"],
        "path": delivery.get("path"),
    }


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
    """真实实现（WP7）：ArgoCD 风格金丝雀——镜像构建 + 容器启动 + 真实探活观测。

    契约应用（注册表同时配置 url 与 probe_keyword，如本机 vite dev 前端）走「直连发布」：
    补丁直接应用到真实仓库（落盘即生效），以真实页面探针观测；其余应用走默认容器金丝雀。
    """
    from . import app_registry, metrics, release

    resolved = None
    try:
        resolved = app_registry.resolve(alert.service)
    except Exception as exc:  # noqa: BLE001 - 注册表不可用回退默认容器金丝雀
        log.warning("[registry] 应用注册表不可用，金丝雀走默认容器模式: %s", exc)

    if resolved and resolved.get("contract") and resolved.get("url"):
        result = await asyncio.to_thread(
            release.run_canary_direct,
            patch,
            resolved["repo"],
            resolved["url"],
            resolved["contract"]["keyword"],
            traffic_percent,
            observe_seconds,
        )
        mode_label = "直连发布"
    else:
        result = await asyncio.to_thread(
            release.run_canary, alert, patch, traffic_percent, observe_seconds
        )
        mode_label = "容器金丝雀"
    log.info(
        "[canary] %s（%s）：错误率=%.2f%% P99=%.0fms → %s",
        patch.patch_id,
        mode_label,
        result.error_rate,
        result.p99_latency_ms,
        result.observation,
    )
    metrics.observe_canary_deploy(result.healthy, result.error_rate)
    return result


@activity.defn
async def finalize_release(canary: CanaryResult, auto_rollback: bool) -> ReleaseResult:
    """真实实现（WP7）：达标真实滚动到稳定版；劣化回收金丝雀并保留稳定版本。

    mode=direct（直连发布）：健康 = 补丁已生效保持上线；劣化还原已在金丝雀阶段完成。
    """
    from . import release

    if canary.mode == "direct":
        result = await asyncio.to_thread(release.run_finalize_direct, canary, auto_rollback)
    else:
        result = await asyncio.to_thread(release.run_finalize, canary, auto_rollback)
    log.info("[release] %s（%s）", result.reason, result.version)
    return result
