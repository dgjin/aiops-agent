"""策略加载与校验：YAML + pydantic 强校验（设计方案第 7 节 / 第 5 节）。

约定：
- 策略以**启动快照**为准，运行期不热修改（设计方案第 5/9 节）；
- 非法配置在启动阶段即抛错，绝不带病启动；
- 已锁定的关键策略（超时驳回、FIFO 排队、不熔断、并发 1）写入校验器，
  任何试图通过改配置绕过锁定策略的行为都会导致启动失败；
- 工作流内不读文件：快照经 ``snapshot_for_workflow()`` 转为纯 JSON dict 传入。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from . import mode as runtime_mode

# 可选：自动加载工程根目录 .env（未安装 python-dotenv 时静默跳过，不影响运行）
try:  # pragma: no cover - 取决于本地是否安装 python-dotenv
    from dotenv import load_dotenv

    if "tests" not in sys.modules:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # pragma: no cover
    pass

_DURATION_RE = re.compile(r"^(\d+)\s*([smh])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "release-gate-policy.yaml"


def _parse_duration(value: object) -> timedelta:
    """解析 '30m' / '15m' / '5s' 等时长写法（也接受数字秒与 timedelta）。"""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=value)
    match = _DURATION_RE.match(str(value).strip().lower())
    if not match:
        raise ValueError(f"不支持的时间格式 {value!r}，支持如 30s / 15m / 2h")
    return timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])


class CompositeGateConfig(BaseModel):
    """M3 组合放行判据：默认关闭（新策略显式启用，避免改变在途流程行为）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    require_suspect_files: bool = True


class FixRoutingConfig(BaseModel):
    """M3 修复模型路由：生产要求 holdout 质量门槛达标，否则转人工/排队。"""

    model_config = ConfigDict(extra="forbid")

    require_quality_bar: bool = False
    quality_bar_path: str = ""
    prefer_qoder_for_multi_file: bool = True


class TriageConfig(BaseModel):
    """闸门 1：根因置信度策略。"""

    model_config = ConfigDict(extra="forbid")

    confidence_threshold: float = Field(0.8, ge=0.0, le=1.0)
    max_fix_retries: int = Field(2, ge=0, le=10)
    # M1 证据质量预检（确定性规则，不新增模型置信分）：
    evidence_lookback_minutes: int = Field(10, ge=1, le=1440)
    evidence_stale_minutes: int = Field(15, ge=1, le=1440)
    evidence_min_signal_lines: int = Field(1, ge=1, le=100)
    # INSUFFICIENT_SIGNAL 默认直接转人工（证据不足）；保留策略开关便于后续评测调整。
    evidence_insufficient_escalate: bool = True
    # M3 组合放行与模型路由（显式启用；旧快照无这些键时按默认关闭/不要求）
    composite_gate: CompositeGateConfig = Field(default_factory=CompositeGateConfig)
    fix_routing: FixRoutingConfig = Field(default_factory=FixRoutingConfig)


class ApprovalConfig(BaseModel):
    """闸门 2：运维审批策略（超时自动驳回为锁定项）。"""

    model_config = ConfigDict(extra="forbid")

    timeout: timedelta = Field(default=timedelta(minutes=30))
    on_timeout: str = "reject"
    on_timeout_escalation: str = "phone"
    second_approval_dirs: list[str] = Field(
        default_factory=lambda: ["auth/**", "payment/**", "crypto/**"]
    )

    @field_validator("timeout", mode="before")
    @classmethod
    def _duration(cls, value: object) -> timedelta:
        return _parse_duration(value)

    @field_validator("on_timeout")
    @classmethod
    def _lock_timeout_action(cls, value: str) -> str:
        if value != "reject":
            raise ValueError("锁定策略：审批超时仅允许自动驳回（on_timeout 必须为 reject），默认安全侧")
        return value


class NewPatchPolicy(BaseModel):
    """窗口内新补丁策略（仅 FIFO 排队，锁定项）。"""

    model_config = ConfigDict(extra="forbid")

    strategy: str = "queue"
    reset_timer: bool = False
    merge_with_current: bool = False
    queue_limit: int = 0
    next_cycle: str = "auto"

    @field_validator("strategy")
    @classmethod
    def _lock_strategy(cls, value: str) -> str:
        if value != "queue":
            raise ValueError("锁定策略：窗口内新补丁仅允许排队（strategy 必须为 queue）")
        return value

    @field_validator("reset_timer")
    @classmethod
    def _lock_reset_timer(cls, value: bool) -> bool:
        if value:
            raise ValueError("锁定策略：窗口内新补丁不得重置倒计时（reset_timer 必须为 false）")
        return value

    @field_validator("merge_with_current")
    @classmethod
    def _lock_merge(cls, value: bool) -> bool:
        if value:
            raise ValueError("锁定策略：窗口内新补丁不得与当前补丁合并（merge_with_current 必须为 false）")
        return value

    @field_validator("queue_limit")
    @classmethod
    def _lock_queue_limit(cls, value: int) -> int:
        if value != 0:
            raise ValueError("锁定策略：排队数量不设上限（queue_limit 必须为 0）")
        return value


class FeedbackBreakerConfig(BaseModel):
    """用户反馈熔断策略（锁定为关闭）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False

    @field_validator("enabled")
    @classmethod
    def _lock_enabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("锁定策略：用户反馈不设熔断阈值（enabled 必须为 false），是否取消由运维人工决策")
        return value


class ConcurrencyConfig(BaseModel):
    """并发底线（锁定为 1）。"""

    model_config = ConfigDict(extra="forbid")

    max_active_deployments: int = 1

    @field_validator("max_active_deployments")
    @classmethod
    def _lock_concurrency(cls, value: int) -> int:
        if value != 1:
            raise ValueError("并发底线：同一时刻全局仅允许一个发布执行（max_active_deployments 必须为 1）")
        return value


class CapacityConfig(BaseModel):
    """并发与背压（M6）：Worker 并发上限 + 资源信号量。

    - ``worker_activities`` / ``worker_local_activities`` 直接传给 Temporal Worker；
    - ``local_llm`` / ``qoder`` / ``sandbox`` 分别限制本地模型、Qoder CLI 与沙箱的
      同时在执行数；``release`` 锁定为 1（沿用「同一时刻仅一个发布」的底线）；
    - ``queue_timeout`` 默认 0 = 无限等待（排队不设上限），正整数表示等待超时后
      拒绝并留痕，由调用方转人工或重试；
    - ``distributed_limits`` 仅表示是否已接入集中限流：进程内信号量不是跨副本
      全局配额，多 Worker 部署不得依赖本项冒充集中配额。
    """

    model_config = ConfigDict(extra="forbid")

    worker_activities: int = Field(10, ge=1, le=200)
    worker_local_activities: int = Field(4, ge=1, le=200)
    local_llm: int = Field(1, ge=1, le=64)
    qoder: int = Field(1, ge=1, le=64)
    sandbox: int = Field(2, ge=1, le=64)
    release: int = 1
    queue_timeout: timedelta = Field(default_factory=lambda: timedelta(0))
    distributed_limits: bool = False

    @field_validator("release")
    @classmethod
    def _lock_release(cls, value: int) -> int:
        if value != 1:
            raise ValueError("并发底线：同一时刻全局仅允许一个发布执行（capacity.release 必须为 1）")
        return value

    @field_validator("queue_timeout", mode="before")
    @classmethod
    def _duration(cls, value: object) -> timedelta:
        return _parse_duration(value)


class NotifyWindowConfig(BaseModel):
    """闸门 3：用户公告 + 延迟发布窗口。"""

    model_config = ConfigDict(extra="forbid")

    countdown: timedelta = Field(default=timedelta(minutes=15))
    deploy_on_expiry: bool = True
    allow_deploy_now: bool = True
    allow_cancel: bool = True
    new_patch_during_window: NewPatchPolicy = Field(default_factory=NewPatchPolicy)
    feedback_circuit_breaker: FeedbackBreakerConfig = Field(default_factory=FeedbackBreakerConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)

    @field_validator("countdown", mode="before")
    @classmethod
    def _duration(cls, value: object) -> timedelta:
        return _parse_duration(value)

    @model_validator(mode="after")
    def _lock_deploy_on_expiry(self) -> "NotifyWindowConfig":
        if not self.deploy_on_expiry:
            raise ValueError("锁定策略：倒计时到期为默认出口，必须自动进入金丝雀（deploy_on_expiry 必须为 true）")
        return self


class CanaryConfig(BaseModel):
    """金丝雀与自动回滚策略。"""

    model_config = ConfigDict(extra="forbid")

    traffic_percent: int = Field(5, ge=1, le=100)
    observe_duration: timedelta = Field(default=timedelta(minutes=5))
    health_metrics: list[str] = Field(default_factory=lambda: ["error_rate", "p99_latency"])
    auto_rollback: bool = True

    @field_validator("observe_duration", mode="before")
    @classmethod
    def _duration(cls, value: object) -> timedelta:
        return _parse_duration(value)


class ReleaseGatePolicy(BaseModel):
    """release_gate 段总纲。

    shadow_mode：运行档开关——true = 影子档（沙箱测试通过后停止，只生成
    根因/补丁/测试报告建议并留痕通知，不创建 MR、不进入审批与发布）；
    false = 全链路（MR → 审批 → 公告 → 金丝雀发布）。production 档必须
    显式声明该键（validate_runtime_policy 校验），防配置缺失静默改变执行姿势。
    """

    model_config = ConfigDict(extra="forbid")

    shadow_mode: bool = False
    # M4：高风险变更（受保护目录 / 高风险标记）不得仅凭静态验收推进发布
    require_real_tests_for_high_risk: bool = True
    triage: TriageConfig = Field(default_factory=TriageConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    notify_window: NotifyWindowConfig = Field(default_factory=NotifyWindowConfig)
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    # M6：Worker 并发与资源信号量（旧策略缺省时用默认值，行为按串行执行）
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)


class GitConfig(BaseModel):
    """Git 平台接入（优化方案 3.7 / GAP-14）：MR 真实创建的可选配置段。

    缺省不启用（repo_url 为空 → MR 活动返回 ``recorded`` 桩，演示行为不变）；
    真实令牌运行时从环境变量 ``AIOPS_GIT_TOKEN`` 读取（生产经 K8s Secret 注入），
    ``token_secret`` 仅作为部署侧 Secret 名称的引用说明。
    """

    model_config = ConfigDict(extra="forbid")

    repo_url: str = ""
    token_secret: str = ""
    default_branch: str = "main"
    mr_labels: list[str] = Field(default_factory=lambda: ["aiops", "auto-fix"])


class Policy(BaseModel):
    """策略根模型：强校验入口，任何未知键 / 非法值都会在启动阶段抛错。"""

    model_config = ConfigDict(extra="forbid")

    release_gate: ReleaseGatePolicy = Field(default_factory=ReleaseGatePolicy)
    git: GitConfig = Field(default_factory=GitConfig)
    _source: str = PrivateAttr(default="")
    _sha256: str = PrivateAttr(default="")

    def summary(self) -> str:
        gate = self.release_gate
        return (
            f"shadow_mode={gate.shadow_mode}, "
            f"confidence_threshold={gate.triage.confidence_threshold}, "
            f"max_fix_retries={gate.triage.max_fix_retries}, "
            f"approval_timeout={gate.approval.timeout}, "
            f"countdown={gate.notify_window.countdown}, "
            f"canary={gate.canary.traffic_percent}%/observe={gate.canary.observe_duration}, "
            f"auto_rollback={gate.canary.auto_rollback}, "
            f"capacity=llm{gate.capacity.local_llm}/qoder{gate.capacity.qoder}"
            f"/sandbox{gate.capacity.sandbox}/release{gate.capacity.release}"
        )


def resolve_policy_path(path: str | os.PathLike | None = None) -> Path:
    """策略文件定位：显式参数 > 环境变量 AIOPS_POLICY_PATH > 默认路径。"""
    selected = path or os.environ.get("AIOPS_POLICY_PATH")
    if not selected:
        selected = DEFAULT_POLICY_PATH.with_name("demo-policy.yaml") if runtime_mode.is_demo() else DEFAULT_POLICY_PATH
    selected = Path(selected).expanduser()
    if not selected.is_absolute():
        selected = DEFAULT_POLICY_PATH.parent / selected
    return selected.resolve()


def load_policy(path: str | os.PathLike | None = None) -> Policy:
    """加载并强校验策略（启动时快照）。非法配置在此抛错。"""
    policy_path = resolve_policy_path(path)
    if not policy_path.exists():
        raise FileNotFoundError(f"策略文件不存在: {policy_path}")
    content = policy_path.read_bytes()
    policy = Policy.model_validate(yaml.safe_load(content))
    policy._source = str(policy_path)
    policy._sha256 = hashlib.sha256(content).hexdigest()
    validate_runtime_policy(policy)
    expected = os.environ.get("AIOPS_POLICY_SHA256")
    if expected and expected != policy._sha256:
        raise runtime_mode.ModeConfigError("策略 hash 与启动快照不符；要求受控 restart")
    return policy


def validate_runtime_policy(policy: Policy) -> None:
    """production 必须显式声明运行档（shadow_mode 键必填，true/false 均可）。

    不依赖默认值，防配置缺失静默切换执行姿势；不更改正在执行的历史流程。
    """
    if runtime_mode.is_production() and "shadow_mode" not in policy.release_gate.model_fields_set:
        raise runtime_mode.ModeConfigError(
            "production 必须显式配置 release_gate.shadow_mode（true=影子档只建议 / false=全链路执行）"
        )


def startup_snapshot(root: Path | None = None) -> dict:
    """纯配置预检，不建目录、不连接数据库；可安全用于启动摘要/readiness。"""
    from sqlalchemy.engine import make_url

    root = (root or DEFAULT_POLICY_PATH.parent).resolve()
    production = runtime_mode.is_production()
    python = root / ".venv/bin/python"
    if production and (not python.is_file() or not os.access(python, os.X_OK)):
        raise runtime_mode.ModeConfigError("production 必须使用仓库 .venv/bin/python")
    explicit = os.environ.get("AIOPS_DATA_ROOT", "")
    if explicit and not Path(explicit).expanduser().is_absolute():
        raise runtime_mode.ModeConfigError("AIOPS_DATA_ROOT 必须是绝对路径")
    data = runtime_mode.data_root()
    if production and data in (root, root / "data"):
        raise runtime_mode.ModeConfigError("production 数据根不得复用 demo 数据根")
    anchor = data
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    if not anchor.is_dir() or not os.access(anchor, os.R_OK | os.W_OK | os.X_OK):
        raise runtime_mode.ModeConfigError("数据根不可访问/写入；未创建任何目录")
    database = runtime_mode.db_url()
    try:
        db = make_url(database) if database else None
        if production and (db.get_backend_name() != "mysql" or not db.host or not db.database or not db.username):
            raise ValueError
    except Exception:
        raise runtime_mode.ModeConfigError("production 需要完整 MySQL URL（详情已隐藏）") from None
    if production and not os.environ.get("AIOPS_REDIS_URL", "").startswith(("redis://", "rediss://")):
        raise runtime_mode.ModeConfigError("production 必须配置 AIOPS_REDIS_URL")
    # M5：追踪生产门禁——声明 required 就必须配置端点；真实 Collector 联通仍属外部验收
    from . import tracing

    if production and tracing.required() and not tracing.enabled():
        raise runtime_mode.ModeConfigError(
            "AIOPS_OTEL_REQUIRED=true 时必须配置 AIOPS_OTEL_ENDPOINT（OTLP HTTP 端点）"
        )
    from urllib.parse import urlsplit
    try:
        endpoint = urlsplit("//" + os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
        if not endpoint.hostname or not endpoint.port or endpoint.path or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError
    except ValueError:
        raise runtime_mode.ModeConfigError("TEMPORAL_ADDRESS 必须为 host:port（无凭证）") from None
    listeners = {}
    for name, default_port in (("BFF", 8600), ("WEBHOOK", 8099), ("METRICS", 9091 if production else 9090)):
        host = os.environ.get(f"AIOPS_{name}_HOST", "127.0.0.1")
        try:
            address = ipaddress.ip_address(host)
            port = int(os.environ.get(f"AIOPS_{name}_PORT", str(default_port)))
            if not 1 <= port <= 65535 or (production and not address.is_loopback):
                raise ValueError
        except ValueError:
            raise runtime_mode.ModeConfigError(f"{name} 监听地址/端口非法（production 要求 loopback）") from None
        listeners[name.lower()] = {"host": host, "port": port}
    if len({v["port"] for v in listeners.values()}) != len(listeners):
        raise runtime_mode.ModeConfigError("组件监听端口不能重复")
    policy = load_policy()
    return {"mode": runtime_mode.mode(), "repository": str(root), "python": str(python),
            "policy_path": policy._source, "policy_sha256": policy._sha256,
            "shadow_mode": policy.release_gate.shadow_mode, "data_root": str(data),
            "database": "mysql (configured)" if db else "file (demo)",
            "temporal_address": endpoint.netloc,
            "namespace": runtime_mode.temporal_namespace(), "task_queue": runtime_mode.temporal_task_queue(),
            "listeners": listeners,
            "capacity": snapshot_for_workflow(policy)["capacity"]}


def snapshot_for_workflow(policy: Policy) -> dict:
    """转为纯 JSON dict 快照（时长统一转秒），作为工作流启动参数传入。

    工作流内只读该快照，运行期修改磁盘 YAML 不影响进行中的流程（设计方案第 9 节）。
    """
    validate_runtime_policy(policy)
    gate = policy.release_gate
    return {
        "policy_source": policy._source or str(resolve_policy_path()),
        "policy_sha256": policy._sha256,
        "runtime_mode": runtime_mode.mode(),
        "temporal_namespace": runtime_mode.temporal_namespace(),
        "temporal_task_queue": runtime_mode.temporal_task_queue(),
        # P1-2：shadow 档透传（工作流读取用 .get 兜底，旧快照无此键时行为不变）
        "shadow_mode": gate.shadow_mode,
        # M4：静态验收不足以支撑高风险发布的开关（旧快照缺省时按开启处理）
        "sandbox": {
            "require_real_tests_for_high_risk": gate.require_real_tests_for_high_risk,
        },
        "triage": {
            "confidence_threshold": gate.triage.confidence_threshold,
            "max_fix_retries": gate.triage.max_fix_retries,
            "evidence_lookback_minutes": gate.triage.evidence_lookback_minutes,
            "evidence_stale_minutes": gate.triage.evidence_stale_minutes,
            "evidence_min_signal_lines": gate.triage.evidence_min_signal_lines,
            "evidence_insufficient_escalate": gate.triage.evidence_insufficient_escalate,
            # M3：组合放行判据与模型路由（旧快照缺省时工作流按 .get 兜底）
            "composite_gate": {
                "enabled": gate.triage.composite_gate.enabled,
                "require_suspect_files": gate.triage.composite_gate.require_suspect_files,
            },
            "fix_routing": {
                "require_quality_bar": gate.triage.fix_routing.require_quality_bar,
                "quality_bar_path": gate.triage.fix_routing.quality_bar_path,
                "prefer_qoder_for_multi_file": gate.triage.fix_routing.prefer_qoder_for_multi_file,
            },
        },
        "approval": {
            "timeout_seconds": int(gate.approval.timeout.total_seconds()),
            "on_timeout": gate.approval.on_timeout,
            "on_timeout_escalation": gate.approval.on_timeout_escalation,
            "second_approval_dirs": list(gate.approval.second_approval_dirs),
        },
        "notify_window": {
            "countdown_seconds": int(gate.notify_window.countdown.total_seconds()),
            "deploy_on_expiry": gate.notify_window.deploy_on_expiry,
            "allow_deploy_now": gate.notify_window.allow_deploy_now,
            "allow_cancel": gate.notify_window.allow_cancel,
        },
        "canary": {
            "traffic_percent": gate.canary.traffic_percent,
            "observe_seconds": int(gate.canary.observe_duration.total_seconds()),
            "auto_rollback": gate.canary.auto_rollback,
        },
        # M6：并发与背压（工作流不消费该段，仅随快照留痕，供排障对照启动配置）
        "capacity": {
            "worker_activities": gate.capacity.worker_activities,
            "worker_local_activities": gate.capacity.worker_local_activities,
            "local_llm": gate.capacity.local_llm,
            "qoder": gate.capacity.qoder,
            "sandbox": gate.capacity.sandbox,
            "release": gate.capacity.release,
            "queue_timeout_seconds": int(gate.capacity.queue_timeout.total_seconds()),
            "distributed_limits": gate.capacity.distributed_limits,
        },
    }
