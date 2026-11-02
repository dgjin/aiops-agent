"""并发与背压（M6）：资源信号量、排队观测与拒绝口径。

设计边界：

- 只约束**当前进程**的并发。多副本部署必须接入集中限流（策略
  ``capacity.distributed_limits=true`` 仅表示该前提已满足），不得把本模块的
  进程内信号量当作跨 Worker 全局配额；
- 默认 ``queue_timeout=0`` 表示无限等待（不拒绝），与既有「排队不设上限」的
  锁定策略一致；配置正整数超时后，超时任务抛 :class:`CapacityError`，由调用方
  转人工或重试，不静默丢单；
- 发布的全局互斥仍由策略 ``concurrency.max_active_deployments=1`` 锁定，
  本模块的 ``release`` 信号量只做执行期串行化，不替代发布闸门；
- 所有观测走 :mod:`aiops_agent.metrics` 旁路，异常不影响主流程。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

log = logging.getLogger("aiops.capacity")


def _metrics():
    """延迟导入 metrics：本模块会被 activities 顶层导入，避免把依赖带进
    Temporal workflow 沙箱的模块校验路径。"""
    from . import metrics as module

    return module


RESOURCES = ("local_llm", "qoder", "sandbox", "release")
DEFAULT_LIMITS = {"local_llm": 1, "qoder": 1, "sandbox": 2, "release": 1}
_SAMPLE_SIZE = 512


class CapacityError(RuntimeError):
    """排队超时或资源名非法（带明确拒绝原因，不静默丢单）。"""

    def __init__(self, resource: str, reason: str, wait_seconds: float = 0.0) -> None:
        super().__init__(f"资源 {resource} 拒绝执行：{reason}（等待 {wait_seconds:.1f}s）")
        self.resource = resource
        self.reason = reason
        self.wait_seconds = wait_seconds


def _p95(values: deque[float]) -> float | None:
    """最近秩法 P95；样本为空返回 None（不能用 0 冒充瞬时）。"""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return round(ordered[index], 3)


class ResourceLimiter:
    """按资源名隔离的 asyncio 信号量 + 排队/执行观测。"""

    def __init__(
        self,
        limits: dict[str, int] | None = None,
        *,
        queue_timeout: float = 0.0,
        distributed: bool = False,
    ) -> None:
        resolved = {
            name: int((limits or {}).get(name, DEFAULT_LIMITS[name])) for name in RESOURCES
        }
        for name, value in resolved.items():
            if value < 1:
                raise ValueError(f"资源配额必须 ≥1：{name}={value}")
        self.limits = resolved
        self.queue_timeout = max(0.0, float(queue_timeout))
        self.distributed = bool(distributed)
        self._semaphores = {name: asyncio.Semaphore(value) for name, value in resolved.items()}
        self._state = {
            name: {
                "in_use": 0,
                "peak_in_use": 0,
                "waiting": 0,
                "peak_waiting": 0,
                "completed": 0,
                "waits": deque(maxlen=_SAMPLE_SIZE),
                "runs": deque(maxlen=_SAMPLE_SIZE),
                "rejected": {},
            }
            for name in RESOURCES
        }
        self._publish()

    @asynccontextmanager
    async def limit(self, resource: str) -> AsyncIterator[None]:
        if resource not in self._semaphores:
            raise CapacityError(resource, "unknown_resource")
        state = self._state[resource]
        semaphore = self._semaphores[resource]
        state["waiting"] += 1
        state["peak_waiting"] = max(state["peak_waiting"], state["waiting"])
        self._publish(resource)
        started = time.monotonic()
        try:
            if self.queue_timeout > 0:
                await asyncio.wait_for(semaphore.acquire(), timeout=self.queue_timeout)
            else:
                await semaphore.acquire()
        except TimeoutError:
            waited = time.monotonic() - started
            state["waiting"] -= 1
            self._reject(resource, "queue_timeout")
            raise CapacityError(resource, "queue_timeout", waited) from None
        except asyncio.CancelledError:
            state["waiting"] -= 1
            self._reject(resource, "cancelled")
            raise
        waited = time.monotonic() - started
        state["waiting"] -= 1
        state["waits"].append(waited)
        _metrics().observe_capacity_wait(resource, waited)
        state["in_use"] += 1
        state["peak_in_use"] = max(state["peak_in_use"], state["in_use"])
        self._publish(resource)
        run_started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - run_started
            state["in_use"] -= 1
            state["completed"] += 1
            state["runs"].append(elapsed)
            _metrics().observe_capacity_run(resource, elapsed)
            semaphore.release()
            self._publish(resource)

    def _reject(self, resource: str, reason: str) -> None:
        rejected = self._state[resource]["rejected"]
        rejected[reason] = rejected.get(reason, 0) + 1
        _metrics().observe_capacity_rejected(resource, reason)
        self._publish(resource)

    def _publish(self, resource: str | None = None) -> None:
        for name in (resource,) if resource else RESOURCES:
            state = self._state[name]
            _metrics().set_capacity_state(name, state["in_use"], state["waiting"])

    def snapshot(self) -> dict:
        return {
            "limits": dict(self.limits),
            "queue_timeout_seconds": self.queue_timeout,
            "distributed_limits": self.distributed,
            "resources": {
                name: {
                    "in_use": state["in_use"],
                    "peak_in_use": state["peak_in_use"],
                    "waiting": state["waiting"],
                    "peak_waiting": state["peak_waiting"],
                    "completed": state["completed"],
                    "wait_p95_seconds": _p95(state["waits"]),
                    "run_p95_seconds": _p95(state["runs"]),
                    "rejected": dict(state["rejected"]),
                }
                for name, state in self._state.items()
            },
        }

    def reset(self) -> None:
        """重建信号量并清空观测（测试与压测复用同一进程时使用）。"""
        self._semaphores = {name: asyncio.Semaphore(value) for name, value in self.limits.items()}
        for state in self._state.values():
            state["in_use"] = 0
            state["peak_in_use"] = 0
            state["waiting"] = 0
            state["peak_waiting"] = 0
            state["completed"] = 0
            state["waits"].clear()
            state["runs"].clear()
            state["rejected"].clear()
        self._publish()


_limiter: ResourceLimiter | None = None


def configure_from_policy(policy=None) -> ResourceLimiter:
    """按策略重建进程限流器；策略不可读时回落默认配额并显式告警。"""
    global _limiter
    if policy is None:
        from .config import CapacityConfig, load_policy

        try:
            capacity = load_policy().release_gate.capacity
        except Exception as exc:  # noqa: BLE001 - 限流是旁路约束，不因策略读取失败阻断业务
            log.warning("容量策略不可读，回落默认配额：%s", exc)
            capacity = CapacityConfig()
    else:
        capacity = policy.release_gate.capacity
    _limiter = ResourceLimiter(
        {
            "local_llm": capacity.local_llm,
            "qoder": capacity.qoder,
            "sandbox": capacity.sandbox,
            "release": capacity.release,
        },
        queue_timeout=capacity.queue_timeout.total_seconds(),
        distributed=capacity.distributed_limits,
    )
    if not capacity.distributed_limits:
        log.info("容量配额为本进程信号量（distributed_limits=false）：多副本必须接入集中限流")
    return _limiter


def get_limiter() -> ResourceLimiter:
    global _limiter
    if _limiter is None:
        configure_from_policy()
    return _limiter


def set_limiter(limiter: ResourceLimiter | None) -> None:
    """测试/压测注入；传 None 表示下次访问时按策略重建。"""
    global _limiter
    _limiter = limiter


async def _run(resource: str, fn: Callable, args: tuple, kwargs: dict):
    async with get_limiter().limit(resource):
        return await fn(*args, **kwargs)


def limited(resource: str) -> Callable:
    """给活动函数套上资源信号量（保留原名与签名，供 Temporal 注册）。"""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await _run(resource, fn, args, kwargs)

        return wrapper

    return decorator
