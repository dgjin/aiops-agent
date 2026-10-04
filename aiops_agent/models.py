"""工作流与活动间流转的数据模型（设计方案第 8 节）。

全部使用 dataclass，保证 Temporal 序列化兼容（JSON 可编码）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Alert:
    """标准告警事件——整个流程的唯一入口（设计方案 4.1）。"""

    alert_id: str
    service: str
    severity: str = "critical"
    description: str = ""


@dataclass
class RootCause:
    """LLM 结构化根因（设计方案 4.2）。"""

    error_type: str
    suspect_files: list[str]
    confidence: float
    summary: str


@dataclass
class Patch:
    """最小化补丁（设计方案 4.3），元数据可追溯。"""

    patch_id: str
    alert_id: str
    files: list[str]
    diff: str
    description: str
    risk: str
    model_version: str
    confidence: float


@dataclass
class TestReport:
    """沙箱测试报告（设计方案 4.4）。"""

    patch_id: str
    passed: bool
    unit_tests: str = ""
    regression_tests: str = ""
    sast: str = ""
    details: str = ""


@dataclass
class CanaryResult:
    """金丝雀观察结果（设计方案 4.7）。"""

    patch_id: str
    traffic_percent: int
    healthy: bool
    error_rate: float
    p99_latency_ms: float
    observation: str = ""
    # 发布模式：container=镜像+容器滚动（demo-app）；direct=补丁直连真实仓库（契约应用）
    mode: str = "container"


@dataclass
class ReleaseResult:
    """发布终态（全量晋级或自动回滚）。"""

    version: str
    rolled_back: bool
    reason: str = ""
