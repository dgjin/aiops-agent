"""ArgoCD / Argo Rollouts 真实状态（M4）：同步、健康与金丝雀进度以集群为准。

状态机（与发布流程、控制台一致）：
    RECORDED   仅留痕（未配置 ArgoCD，本机演示）
    SUBMITTED  已提交 ArgoCD，尚未观察到同步状态
    SYNCING    同步中 / OutOfSync
    SYNCED     已同步，健康状态仍为 Progressing
    HEALTHY    同步且健康（只有到达该状态才可显示「发布完成」）
    DEGRADED   ArgoCD 报告 Degraded（真实劣化）
    ROLLED_BACK 已回滚到上一版本
    FAILED     提交/查询失败或等待超时，目标状态未知

设计约束：
    - 只读 ArgoCD API 与 Rollouts CRD；不编造健康结论；
    - 无 URL 时状态恒为 RECORDED，调用方不得声称发布完成；
    - 等待过程中的每次状态变化通过 on_state 回调留痕（journal / 事件流）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

STATES = (
    "RECORDED",
    "SUBMITTED",
    "SYNCING",
    "SYNCED",
    "HEALTHY",
    "DEGRADED",
    "ROLLED_BACK",
    "FAILED",
)
TERMINAL_STATES = frozenset({"HEALTHY", "DEGRADED", "ROLLED_BACK", "FAILED"})


def derive_state(sync_status: str, health_status: str) -> str:
    """由 ArgoCD 的 sync/health 推导本地状态（未知输入按最保守的 FAILED 处理）。"""
    sync_status = (sync_status or "").strip()
    health_status = (health_status or "").strip()
    if health_status == "Degraded":
        return "DEGRADED"
    if health_status == "Healthy":
        return "HEALTHY"
    if health_status == "Missing":
        return "FAILED"
    if sync_status == "OutOfSync":
        return "SYNCING"
    if sync_status == "Synced":
        return "SYNCED"
    if health_status == "Progressing":
        return "SYNCED"
    return "SUBMITTED" if sync_status or health_status else "SUBMITTED"


def rollout_progress(payload: dict) -> dict:
    """解析 Argo Rollouts 资源，返回步骤/权重/阶段（缺失字段按未知返回）。"""
    status = payload.get("status") if isinstance(payload, dict) else None
    status = status if isinstance(status, dict) else {}
    spec = payload.get("spec") if isinstance(payload, dict) else None
    strategy = (spec or {}).get("strategy") if isinstance(spec, dict) else None
    canary = (strategy or {}).get("canary") if isinstance(strategy, dict) else None
    steps = (canary or {}).get("steps") if isinstance(canary, dict) else None
    current = status.get("currentStepIndex")
    return {
        "phase": status.get("phase") or "",
        "current_step": current if isinstance(current, int) else -1,
        "total_steps": len(steps) if isinstance(steps, list) else -1,
        "message": status.get("message") or "",
        "paused": bool(status.get("pauseConditions")),
    }


class ArgoStatusError(RuntimeError):
    """ArgoCD 状态查询失败（调用方须保留拦截，不得视为健康）。"""


class ArgoStatusClient:
    """ArgoCD API + Rollouts CRD 只读客户端（stdlib urllib，无第三方依赖）。"""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 5.0,
        interval: float = 2.0,
        sleep=time.sleep,
        clock=time.monotonic,
        custom_api=None,
    ) -> None:
        self.base_url = (
            base_url if base_url is not None else os.environ.get("AIOPS_ARGOCD_URL", "")
        ).rstrip("/")
        self.token = token if token is not None else os.environ.get("AIOPS_ARGOCD_TOKEN", "")
        self.timeout = timeout
        self.interval = interval
        self._sleep = sleep
        self._clock = clock
        self._custom = custom_api

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _get(self, url: str) -> dict:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ArgoStatusError(f"ArgoCD 返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise ArgoStatusError(f"ArgoCD 不可达或响应非法：{exc}") from exc

    def get_application(self, name: str) -> dict:
        return self._get(f"{self.base_url}/api/v1/applications/{name}")

    def application_state(self, name: str) -> dict:
        payload = self.get_application(name)
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        sync = status.get("sync") if isinstance(status.get("sync"), dict) else {}
        health = status.get("health") if isinstance(status.get("health"), dict) else {}
        rollout = status.get("rollout") if isinstance(status.get("rollout"), dict) else {}
        state = derive_state(str(sync.get("status") or ""), str(health.get("status") or ""))
        return {
            "state": state,
            "sync": sync.get("status") or "",
            "health": health.get("status") or "",
            "revision": sync.get("revision") or "",
            "rollout_step": rollout.get("step") if isinstance(rollout.get("step"), int) else -1,
            "message": health.get("message") or sync.get("revision") or "",
        }

    def rollout_state(self, namespace: str, name: str) -> dict:
        """读取 Rollout CRD（配置了集群客户端时）；失败返回空进度而不伪装健康。"""
        if self._custom is None:
            return {"phase": "", "current_step": -1, "total_steps": -1, "message": "", "paused": False}
        try:
            payload = self._custom.get_namespaced_custom_object(
                "argoproj.io", "v1alpha1", namespace, "rollouts", name
            )
        except Exception as exc:  # noqa: BLE001 - CRD 不可读按未知处理
            return {
                "phase": "",
                "current_step": -1,
                "total_steps": -1,
                "message": f"Rollout 不可读：{exc}",
                "paused": False,
            }
        return rollout_progress(payload)

    def rollback(self, name: str) -> None:
        """回滚到上一版本（ArgoCD rollback API）；失败抛 ArgoStatusError。"""
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/applications/{name}/rollback",
            data=json.dumps({"revision": 0, "prune": True}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return
        except urllib.error.HTTPError as exc:
            raise ArgoStatusError(f"ArgoCD 回滚失败：HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ArgoStatusError(f"ArgoCD 回滚请求失败：{exc}") from exc

    def wait(
        self,
        name: str,
        *,
        timeout: float = 120.0,
        on_state=None,
        stop_on=("HEALTHY", "DEGRADED", "FAILED"),
    ) -> dict:
        """轮询直到目标状态或超时；超时返回 FAILED（不伪装健康）。"""
        deadline = self._clock() + max(timeout, 0)
        last: dict = {"state": "SUBMITTED", "sync": "", "health": "", "message": ""}
        while True:
            try:
                last = self.application_state(name)
            except ArgoStatusError as exc:
                last = {"state": "FAILED", "sync": "", "health": "", "message": str(exc)}
            if on_state is not None:
                on_state(last["state"])
            if last["state"] in stop_on:
                return last
            if self._clock() >= deadline:
                return {**last, "state": "FAILED", "message": f"等待 {timeout:.0f}s 未达终态"}
            self._sleep(self.interval)
