"""确定性风险策略（M5）：只依据可核对输入给出等级，不采信 LLM 自述。

背景：``Patch.risk`` 是修复模型生成的自由文本，历史上被直接用于「是否高风险」
判定（字符串包含「高 / high」），既可被提示注入影响，也无法解释。本模块改为
按受保护目录、敏感路径、数据迁移、影响范围、降级标记与真实测试情况输出可复现
的 ``LOW / MEDIUM / HIGH`` 与逐条原因；``Patch.risk`` 仅作为展示字段保留。

设计约束（确定性，可 replay）：

- 纯函数：只读入参，不访问时钟、文件系统、环境变量或网络；
- 规则顺序固定：同一输入永远得到同一等级与同一 ``reasons`` 顺序；
- 未知不冒充安全：缺失测试模式按 ``UNKNOWN`` 处理，归入「非真实测试」；
- 等级只由权重与硬触发决定，不允许调用方覆盖。

等级语义：

- ``HIGH``：需要真实测试（UNIT_TEST）且需要双人审批；
- ``MEDIUM``：可通过静态验收，但单人审批；
- ``LOW``：常规变更。

本模块不判断「是否允许发布」——是否阻断由工作流按等级与测试模式决定。
"""

from __future__ import annotations

from typing import Mapping, Sequence

RULE_VERSION = "risk-policy-v1"

LEVELS = ("LOW", "MEDIUM", "HIGH")
LEVEL_LABELS = {"LOW": "低风险", "MEDIUM": "中风险", "HIGH": "高风险"}

# 敏感路径：认证 / 支付 / 权限 / 密钥 / 数据迁移。命中即 HIGH（硬触发）。
_SENSITIVE_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "auth",
        "认证与会话",
        (
            "auth/",
            "/auth",
            "login",
            "logout",
            "session",
            "password",
            "passwd",
            "credential",
            "oauth",
            "sso",
            "jwt",
            "token",
        ),
    ),
    (
        "payment",
        "支付与计费",
        ("payment", "billing", "checkout", "invoice", "refund", "settlement"),
    ),
    (
        "permission",
        "权限与访问控制",
        ("rbac", "permission", "acl", "role/", "roles/", "policy/", "access_control"),
    ),
    (
        "crypto",
        "密钥与证书",
        ("crypto/", "secret", "keyring", "private_key", "certificate", "tls/", "keystore"),
    ),
    (
        "migration",
        "数据迁移与库表结构",
        (
            "migration",
            "alembic/",
            "schema.sql",
            ".sql",
            "ddl",
            "flyway",
            "liquibase",
        ),
    ),
)

# 配置/基础设施：命中计 MEDIUM（可能改变运行行为但通常不触及业务数据）。
_CONFIG_MARKERS = (
    "config/",
    "settings/",
    "dockerfile",
    "docker-compose",
    "k8s/",
    "deploy/",
    "helm/",
    "terraform/",
    ".env",
    "requirements",
    "pyproject.toml",
    "package.json",
    "vite.config",
    "tsconfig",
)

# 影响面阈值
_WIDE_FILE_COUNT = 5
_MULTI_SERVICE_COUNT = 2

# 权重：HIGH 硬触发 4 分；MEDIUM 2 分。总分 ≥4 视为 HIGH，≥2 视为 MEDIUM。
_WEIGHT_HIGH = 4
_WEIGHT_MEDIUM = 2
_HIGH_THRESHOLD = 4
_MEDIUM_THRESHOLD = 2

# 真实测试口径：只有 UNIT_TEST 计为真实执行；空串=历史/未知，一律按非真实处理。
_REAL_TEST_MODES = frozenset({"UNIT_TEST"})


def _normalize(path: str) -> str:
    """路径归一化：统一小写与分隔符，避免 ``Auth\\Login.py`` 绕过匹配。"""
    return str(path or "").replace("\\", "/").lower()


def _prefix_match(path: str, pattern: str) -> bool:
    """受保护目录匹配（``auth/**`` 前缀语义，与工作流既有判定一致）。"""
    prefix = pattern[:-3] if pattern.endswith("/**") else pattern
    prefix = prefix.rstrip("/").lower()
    if not prefix:
        return False
    return path == prefix or path.startswith(prefix + "/")


def _sensitive_hits(files: Sequence[str]) -> list[dict]:
    """敏感路径命中：同一类别只报一次，附首个命中文件便于审计定位。"""
    hits: list[dict] = []
    for code, label, markers in _SENSITIVE_RULES:
        for raw in files:
            path = _normalize(raw)
            if not path:
                continue
            if any(marker in path for marker in markers):
                hits.append(
                    {
                        "code": f"sensitive:{code}",
                        "label": f"触及{label}",
                        "weight": _WEIGHT_HIGH,
                        "detail": raw,
                    }
                )
                break
    return hits


def _config_hits(files: Sequence[str]) -> list[dict]:
    hits: list[dict] = []
    for raw in files:
        path = _normalize(raw)
        if not path:
            continue
        if any(marker in path for marker in _CONFIG_MARKERS):
            hits.append(
                {
                    "code": "config-change",
                    "label": "修改配置或发布基础设施",
                    "weight": _WEIGHT_MEDIUM,
                    "detail": raw,
                }
            )
            break
    return hits


def _protected_hits(files: Sequence[str], patterns: Sequence[str]) -> list[dict]:
    hits: list[dict] = []
    for raw in files:
        path = _normalize(raw)
        for pattern in patterns or ():
            if _prefix_match(path, pattern):
                hits.append(
                    {
                        "code": "protected-path",
                        "label": "命中受保护目录（需双人审批）",
                        "weight": _WEIGHT_HIGH,
                        "detail": f"{raw}（规则 {pattern}）",
                    }
                )
                return hits
    return hits


def _test_hit(verification_mode: str) -> dict | None:
    """非真实测试计入 MEDIUM：风险等级不因「未执行真实测试」被抬高到 HIGH，
    但会作为原因保留，供审批人判断；HIGH 的阻断由工作流按等级+测试模式执行。"""
    mode = str(verification_mode or "").strip().upper()
    if mode in _REAL_TEST_MODES:
        return None
    return {
        "code": "test-not-real",
        "label": "验证方式不是真实单元测试",
        "weight": _WEIGHT_MEDIUM,
        "detail": mode or "UNKNOWN",
    }


def _scope_hits(files: Sequence[str], services: Sequence[str]) -> list[dict]:
    hits: list[dict] = []
    if len([name for name in services if str(name).strip()]) >= _MULTI_SERVICE_COUNT:
        hits.append(
            {
                "code": "multi-service",
                "label": "影响多个服务",
                "weight": _WEIGHT_MEDIUM,
                "detail": ", ".join(str(name) for name in services if str(name).strip()),
            }
        )
    if len(files) >= _WIDE_FILE_COUNT:
        hits.append(
            {
                "code": "wide-change",
                "label": "影响文件数量较多",
                "weight": _WEIGHT_MEDIUM,
                "detail": f"{len(files)} 个文件",
            }
        )
    return hits


def _degraded_hit(degraded: bool) -> dict | None:
    if not degraded:
        return None
    return {
        "code": "degraded-patch",
        "label": "补丁为生成降级兜底产物",
        "weight": _WEIGHT_HIGH,
        "detail": "degraded=True",
    }


def assess(
    *,
    files: Sequence[str] = (),
    services: Sequence[str] = (),
    protected_patterns: Sequence[str] = (),
    verification_mode: str = "",
    degraded: bool = False,
    extra: Mapping[str, object] | None = None,
) -> dict:
    """确定性风险判定（纯函数）。

    ``extra`` 仅用于审计留痕（例如调用方补充影响面说明），不参与打分，
    防止通过扩展字段绕开固定规则。
    """
    paths = [str(item) for item in files or () if str(item).strip()]
    services_list = [str(item) for item in services or () if str(item).strip()]
    mode = str(verification_mode or "").strip().upper()

    reasons: list[dict] = []
    reasons.extend(_protected_hits(paths, protected_patterns))
    reasons.extend(_sensitive_hits(paths))
    degraded_reason = _degraded_hit(degraded)
    if degraded_reason:
        reasons.append(degraded_reason)
    reasons.extend(_scope_hits(paths, services_list))
    reasons.extend(_config_hits(paths))
    test_reason = _test_hit(mode)
    if test_reason:
        reasons.append(test_reason)

    score = sum(int(item["weight"]) for item in reasons)
    hard_trigger = any(int(item["weight"]) >= _WEIGHT_HIGH for item in reasons)
    if hard_trigger or score >= _HIGH_THRESHOLD:
        level = "HIGH"
    elif score >= _MEDIUM_THRESHOLD:
        level = "MEDIUM"
    else:
        level = "LOW"

    return {
        "rule_version": RULE_VERSION,
        "level": level,
        "label": LEVEL_LABELS[level],
        "score": score,
        "reasons": reasons,
        "requires_real_tests": level == "HIGH",
        "requires_second_approval": level == "HIGH",
        "inputs": {
            "files": paths,
            "services": services_list,
            "protected_patterns": [str(item) for item in protected_patterns or ()],
            "verification_mode": mode or "UNKNOWN",
            "degraded": bool(degraded),
            "extra": dict(extra or {}),
        },
    }


def summarize(assessment: Mapping[str, object] | None) -> str:
    """单行摘要（gate_events / 审计文案用）；未知返回空串，不伪造等级。"""
    if not assessment:
        return ""
    level = str(assessment.get("level") or "")
    if level not in LEVEL_LABELS:
        return ""
    codes = [
        str(item.get("code"))
        for item in assessment.get("reasons") or []
        if isinstance(item, dict) and item.get("code")
    ]
    suffix = ":" + ",".join(codes) if codes else ""
    return f"{level}{suffix}"


def verification_block(
    assessment: Mapping[str, object] | None, verification_mode: str
) -> str:
    """高风险但未执行真实测试时的转人工原因；空串表示放行。

    只对 ``requires_real_tests`` 生效——MEDIUM/LOW 的历史行为不变。
    """
    if not assessment or not assessment.get("requires_real_tests"):
        return ""
    mode = str(verification_mode or "").strip().upper()
    if mode in _REAL_TEST_MODES:
        return ""
    reasons = [
        str(item.get("label") or item.get("code") or "")
        for item in assessment.get("reasons") or []
        if isinstance(item, dict) and int(item.get("weight") or 0) >= _WEIGHT_HIGH
    ]
    detail = "；".join(item for item in reasons if item) or "规则命中"
    return (
        f"发布准入：确定性风险等级 {assessment.get('level')}（{detail}），"
        f"本次验证为 {mode or 'UNKNOWN'}，不构成高风险变更的真实测试证据；"
        "已停止推进并转人工复核"
    )
