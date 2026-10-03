"""策略加载与校验：YAML + pydantic 强校验（设计方案第 7 节 / 第 5 节）。

约定：
- 策略以**启动快照**为准，运行期不热修改（设计方案第 5/9 节）；
- 非法配置在启动阶段即抛错，绝不带病启动；
- 已锁定的关键策略（超时驳回、FIFO 排队、不熔断、并发 1）写入校验器，
  任何试图通过改配置绕过锁定策略的行为都会导致启动失败；
- 工作流内不读文件：快照经 ``snapshot_for_workflow()`` 转为纯 JSON dict 传入。
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 可选：自动加载工程根目录 .env（未安装 python-dotenv 时静默跳过，不影响运行）
try:  # pragma: no cover - 取决于本地是否安装 python-dotenv
    from dotenv import load_dotenv

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


class TriageConfig(BaseModel):
    """闸门 1：根因置信度策略。"""

    model_config = ConfigDict(extra="forbid")

    confidence_threshold: float = Field(0.8, ge=0.0, le=1.0)
    max_fix_retries: int = Field(2, ge=0, le=10)


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
    """release_gate 段总纲。"""

    model_config = ConfigDict(extra="forbid")

    triage: TriageConfig = Field(default_factory=TriageConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    notify_window: NotifyWindowConfig = Field(default_factory=NotifyWindowConfig)
    canary: CanaryConfig = Field(default_factory=CanaryConfig)


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

    def summary(self) -> str:
        gate = self.release_gate
        return (
            f"confidence_threshold={gate.triage.confidence_threshold}, "
            f"max_fix_retries={gate.triage.max_fix_retries}, "
            f"approval_timeout={gate.approval.timeout}, "
            f"countdown={gate.notify_window.countdown}, "
            f"canary={gate.canary.traffic_percent}%/observe={gate.canary.observe_duration}, "
            f"auto_rollback={gate.canary.auto_rollback}"
        )


def resolve_policy_path(path: str | os.PathLike | None = None) -> Path:
    """策略文件定位：显式参数 > 环境变量 AIOPS_POLICY_PATH > 默认路径。"""
    if path:
        return Path(path)
    env_path = os.environ.get("AIOPS_POLICY_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_POLICY_PATH


def load_policy(path: str | os.PathLike | None = None) -> Policy:
    """加载并强校验策略（启动时快照）。非法配置在此抛错。"""
    policy_path = resolve_policy_path(path)
    if not policy_path.exists():
        raise FileNotFoundError(f"策略文件不存在: {policy_path}")
    with open(policy_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Policy.model_validate(raw)


def snapshot_for_workflow(policy: Policy) -> dict:
    """转为纯 JSON dict 快照（时长统一转秒），作为工作流启动参数传入。

    工作流内只读该快照，运行期修改磁盘 YAML 不影响进行中的流程（设计方案第 9 节）。
    """
    gate = policy.release_gate
    return {
        "policy_source": str(resolve_policy_path()),
        "triage": {
            "confidence_threshold": gate.triage.confidence_threshold,
            "max_fix_retries": gate.triage.max_fix_retries,
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
    }
