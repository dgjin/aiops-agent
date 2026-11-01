"""组合放行判据（M3）：多维组合闸门，替代「仅比较自报 confidence」的单点判断。

设计约束（对齐闭环实施计划 M3）：

- 现有 ``confidence >= confidence_threshold`` 仍是**必要条件**，组合判据只收紧不放松；
- 其余必要条件：证据质量 ``OK``、Schema 有效（非降级）、根因类型适合代码修复、
  嫌疑文件在目标仓库可定位；
- 基础设施 / 配置 / 证书 / DNS 等非代码根因进入人工或 Runbook 路径，
  **不**为它们生成代码补丁（S08 连接池耗尽、S11 网关 502 属于此类高置信误放行样本）；
- 任何不确定分支走安全侧（转人工），并记录每一项通过/拦截理由，供评测与运营分析。

本模块为纯函数：不读环境变量、不访问网络与文件系统；工作流侧通过
``assess_fix_scope`` 活动提供「嫌疑文件是否可定位」与路由信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 根因类型分类
CODE = "code"
INFRASTRUCTURE = "infrastructure"
CONFIG = "config"
UNKNOWN = "unknown"

# 放行路径
ROUTE_CODE_FIX = "code_fix"
ROUTE_RUNBOOK = "runbook"
ROUTE_HUMAN = "human"

# 高精度非代码信号：命中即进入 Runbook/人工，不生成代码补丁。
# 这些信号在日志里通常指向容量、依赖、平台配置或网络，而非应用代码缺陷；
# 宁可按非代码处理（值守人员可一键改判），也不误放行代码补丁。
_CONFIG_SIGNALS = (
    "yaml",
    "parsererror",
    "config parse",
    "配置解析",
    "配置错误",
    "configerror",
    "missing env",
    "环境变量缺失",
    "invalid config",
)

_INFRA_SIGNALS = (
    "连接池",
    "connection pool",
    "hikari",
    "too many connections",
    "max connections",
    "redis",
    "connection refused",
    "connectionrefused",
    "threadpool",
    "rejectedexecution",
    "线程池",
    "circuitbreaker",
    "circuit breaker",
    "熔断",
    "dns",
    "getaddrinfo",
    "name resolution",
    "域名解析",
    "certificate",
    "cert expired",
    "x509",
    "tls handshake",
    "证书",
    "502",
    "bad gateway",
    "badgateway",
    "upstream",
    "no healthy upstream",
    "upstream connect error",
    "网关",
    "429",
    "ratelimit",
    "rate limit",
    "throttl",
    "限流",
    "kafka",
    "rebalance",
    "consumer lag",
    "consumerlag",
    "消费滞后",
    "消费组",
    "再平衡",
    "no space",
    "enospc",
    "磁盘",
    "disk full",
    "eaddrinuse",
    "addressinuse",
    "address already in use",
    "port in use",
    "端口占用",
    "端口被占用",
    "permission denied",
    "permissionerror",
    "errno 13",
    "文件权限",
    "权限不足",
    "缺少目录权限",
)

# 置信度分桶（评测 Brier/分桶准确率共用）
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))

# 拦截原因中文标签（控制台事件流与待办原因复用，避免两处文案漂移）
REASON_LABELS = {
    "schema_invalid": "根因结构无效或模型降级",
    "evidence_not_ok": "证据质量未通过",
    "confidence_below_threshold": "根因置信度不足",
    "non_code_root_cause:infrastructure": "基础设施类根因（应走 Runbook）",
    "non_code_root_cause:config": "配置类根因（应走 Runbook）",
    "root_cause_unknown": "根因类型无法确认",
    "scope_unavailable": "无法解析修复仓库",
    "suspect_files_missing": "根因未给出嫌疑文件",
    "suspect_file_not_found": "嫌疑文件在目标仓库不可定位",
    "model_not_qualified": "修复模型未达质量门槛",
}


def reason_label(code: str) -> str:
    """拦截原因代码 → 中文文案（未知代码原样返回，不丢信息）。"""
    return REASON_LABELS.get(str(code or ""), str(code or ""))


def classify_root_cause(error_type: str, summary: str, suspect_files: list[str] | None = None) -> str:
    """确定性分类：代码缺陷 / 基础设施 / 配置 / 未知。

    只看根因文本与嫌疑文件名，不引入另一个模型置信分。配置信号优先于基础设施信号
    （配置解析失败常同时出现 yaml / 环境等词），其余默认视为代码缺陷。
    """
    text = " ".join([str(error_type or ""), str(summary or ""), *[str(f) for f in suspect_files or []]])
    narrative = " ".join([str(error_type or ""), str(summary or "")]).strip()
    lowered = text.lower()
    if any(signal in lowered for signal in _CONFIG_SIGNALS):
        return CONFIG
    if any(signal in lowered for signal in _INFRA_SIGNALS):
        return INFRASTRUCTURE
    if not narrative:
        return UNKNOWN
    return CODE


def confidence_bucket(confidence: float) -> str:
    """置信度分桶标签（[0.0,0.5) / [0.5,0.7) / [0.7,0.8) / [0.8,0.9) / [0.9,1.0]）。"""
    value = max(0.0, min(1.0, float(confidence)))
    for low, high in BUCKETS:
        if low <= value < high:
            return f"{low:.1f}-{high:.1f}"
    return "0.9-1.0"


@dataclass
class Decision:
    """一次组合判据结论（可 JSON 化，供评测与审计留痕）。"""

    allowed: bool
    route: str
    category: str
    reasons: list[dict] = field(default_factory=list)
    labels: dict = field(default_factory=dict)

    @property
    def blocked_codes(self) -> list[str]:
        return [r["code"] for r in self.reasons if r["severity"] == "block"]

    def events(self, prefix: str = "gate1:composite") -> list[str]:
        """工作流事件：放行一条，拦截每个原因一条（便于聚合分析）。"""
        if self.allowed:
            return [f"{prefix}:pass"]
        return [f"{prefix}:blocked:{code}" for code in self.blocked_codes]

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "route": self.route,
            "category": self.category,
            "reasons": list(self.reasons),
            "labels": dict(self.labels),
        }


def _reason(code: str, detail: str, severity: str = "block") -> dict:
    return {"code": code, "detail": detail, "severity": severity}


def decide(
    *,
    confidence: float,
    threshold: float,
    error_type: str,
    summary: str = "",
    suspect_files: list[str] | None = None,
    evidence_status: str | None = None,
    schema_valid: bool = True,
    files_found: list[str] | None = None,
    files_missing: list[str] | None = None,
    scope_known: bool = True,
    require_suspect_files: bool = True,
    routing: dict | None = None,
) -> Decision:
    """产出组合放行结论（安全侧）：只有全部必要条件通过才允许代码修复。

    参数
    ----
    confidence: 模型自报置信度（仍是必要条件之一）
    threshold: 策略置信度阈值（0.8，不得在评测/实现中放宽）
    evidence_status: 证据质量状态；``None``/空串表示旧流程无该元数据（保持兼容）
    schema_valid: 根因 Schema 是否有效（降级视为无效）
    files_found/files_missing: 嫌疑文件在目标仓库的定位结果
    scope_known: 是否成功解析修复仓库；False 时无法确认文件存在 → 安全侧
    require_suspect_files: 是否要求嫌疑文件必须可定位（策略开关，默认 True）
    routing: 模型路由结论（``model_routing.route_fix``）；route=human 表示模型未达
        质量门槛，禁止静默降级到未达标模型。
    """
    reasons: list[dict] = []
    category = classify_root_cause(error_type, summary, suspect_files)
    labels = {
        "root_cause_category": category,
        "confidence_bucket": confidence_bucket(confidence),
        "evidence_status": evidence_status or "unknown",
        "suspect_files": list(suspect_files or []),
        "files_found": list(files_found or []),
        "files_missing": list(files_missing or []),
        "routing": dict(routing or {}),
    }

    blocked = False

    # 1) Schema/降级：结构非法时不进入任何自动代码修复
    if not schema_valid:
        blocked = True
        reasons.append(_reason("schema_invalid", "根因 Schema 校验失败或模型降级，拒绝自动修复"))
    else:
        reasons.append(_reason("schema_ok", "根因 Schema 有效", "pass"))

    # 2) 证据质量：非 OK（LOKI_UNAVAILABLE/NO_LOGS/STALE_LOGS/UNMAPPED_SERVICE/
    #    INSUFFICIENT_SIGNAL）不得放行；空值代表旧流程无质量元数据（保持兼容）。
    status = str(evidence_status or "")
    if status and status != "OK":
        blocked = True
        reasons.append(_reason("evidence_not_ok", f"证据质量 {status}，拒绝自动修复"))
    else:
        reasons.append(_reason("evidence_ok", "证据质量 OK 或旧流程无质量元数据", "pass"))

    # 3) 置信度：保留现有必要条件
    if float(confidence) < float(threshold):
        blocked = True
        reasons.append(
            _reason("confidence_below_threshold", f"置信度 {confidence:.2f} < 阈值 {threshold}")
        )
    else:
        reasons.append(_reason("confidence_ok", f"置信度 {confidence:.2f} >= 阈值 {threshold}", "pass"))

    # 4) 根因类型：非代码根因进入 Runbook/人工路径
    if category in (INFRASTRUCTURE, CONFIG):
        blocked = True
        reasons.append(
            _reason(
                f"non_code_root_cause:{category}",
                f"根因属于 {category}，应走人工/Runbook，不生成代码补丁",
            )
        )
    elif category == CODE:
        reasons.append(_reason("code_root_cause", "根因类型适合代码修复", "pass"))
    else:
        blocked = True
        reasons.append(_reason("root_cause_unknown", "根因类型无法确认，安全侧转人工"))

    # 5) 嫌疑文件可定位（代码修复的前提）
    found = [f for f in (files_found or []) if f]
    missing = [f for f in (files_missing or []) if f]
    if require_suspect_files:
        if not scope_known:
            blocked = True
            reasons.append(_reason("scope_unavailable", "无法解析修复仓库，不能确认嫌疑文件存在"))
        elif not suspect_files:
            blocked = True
            reasons.append(_reason("suspect_files_missing", "根因未给出嫌疑文件，无法定位修复点"))
        elif missing or not found:
            blocked = True
            reasons.append(
                _reason(
                    "suspect_file_not_found",
                    f"嫌疑文件在目标仓库不可定位: {missing or suspect_files}",
                )
            )
        else:
            reasons.append(_reason("suspect_file_found", "嫌疑文件已定位", "pass"))

    # 6) 模型路由：未达 holdout 质量门槛 → 人工/排队，不静默降级到未达标模型
    if routing and routing.get("route") == ROUTE_HUMAN:
        blocked = True
        reasons.append(
            _reason("model_not_qualified", "修复模型未达 holdout 质量门槛，按策略转人工/排队")
        )
    elif routing:
        reasons.append(
            _reason(
                "model_route_ok",
                f"修复路由 {routing.get('provider', '')}:{routing.get('model', '')}",
                "pass",
            )
        )

    # 路由：非代码根因 → Runbook；其余拦截 → 人工；全部通过 → 代码修复
    if not blocked:
        route = ROUTE_CODE_FIX
    elif category in (INFRASTRUCTURE, CONFIG):
        route = ROUTE_RUNBOOK
    else:
        route = ROUTE_HUMAN
    return Decision(allowed=not blocked, route=route, category=category, reasons=reasons, labels=labels)
