"""执行层（WP7 交付物）：ArgoCD 风格金丝雀发布 + 本机 Docker 真实滚动执行。

流程（对齐实施计划 WP7）：
    run_canary(alert, patch, traffic_percent, observe_seconds)
      → 复用沙箱工作区（已应用补丁的 demo-app 源码）作为制品构建上下文；
      → render_application：渲染 ArgoCD Application manifest（含金丝雀 steps 摘要）；
      → ArgoCDPublisher.apply：配置 AIOPS_ARGOCD_URL 时真实 POST 提交；
        未配置（本机演示）时留痕 data/argocd/<app>.json（recorded 模式）；
      → docker build 真实构建镜像（tag = aiops-demo-app:<patch_id>）；
      → 启动金丝雀容器（随机端口，BAD_CANARY=1 注入劣化）；
      → observe_seconds 内真实探活（POST /order）实测错误率与 P99 延迟；
      → 按 SLO 判定 healthy → CanaryResult。

    run_finalize(canary, auto_rollback)
      → 达标：真实滚动——停旧稳定容器 → 以金丝雀镜像启动新稳定容器 → 回收金丝雀；
      → 劣化：回收金丝雀容器，稳定版本保持不变（auto_rollback 仅影响留痕文案）。

生产对照：manifest 由 ArgoCD 调谐到 K8s（金丝雀由 Argo Rollouts 按 setWeight steps 分流）；
本机以独立端口的金丝雀容器 + 真实 HTTP 探测对齐同一语义。

环境变量：
    AIOPS_ARGOCD_URL / AIOPS_ARGOCD_TOKEN   ArgoCD API 地址与令牌（未设置则留痕模式）
    AIOPS_CANARY_IMAGE_PREFIX               镜像名前缀（默认 aiops-demo-app）
    AIOPS_STABLE_CONTAINER / AIOPS_STABLE_PORT  稳定容器名与端口（默认 aiops-stable-order:18080）
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import code_rag, sandbox
from .fix_agent import _normalize_rel, apply_unified_diff, split_unified_diff
from .models import Alert, CanaryResult, Patch, ReleaseResult

BASE_DIR = Path(__file__).resolve().parent.parent
ARGOCD_DIR = BASE_DIR / "data" / "argocd"

CANARY_IMAGE_PREFIX = os.environ.get("AIOPS_CANARY_IMAGE_PREFIX", "aiops-demo-app")
STABLE_CONTAINER = os.environ.get("AIOPS_STABLE_CONTAINER", "aiops-stable-order")
STABLE_PORT = int(os.environ.get("AIOPS_STABLE_PORT", "18080"))
APP_PORT = 8000  # 容器内服务端口（http_server.py）

# 演示 SLO：金丝雀健康判定阈值（观测窗口内实测）
SLO_MAX_ERROR_RATE = 5.0  # %
SLO_MAX_P99_LATENCY_MS = 500.0  # ms
OBSERVE_INTERVAL_SECONDS = 0.5
READY_TIMEOUT_SECONDS = 15


def _stable_id(text: str, mod: int) -> int:
    """与 activities 同款稳定编号（版本号公式保持一致）。"""
    return sum(ord(ch) for ch in text) % mod


def release_version(patch_id: str) -> str:
    """发布版本号：与用户公告（notify_users）使用同一公式。"""
    return f"v1.0.{_stable_id(patch_id, 900) + 100}"


def render_application(plan: dict) -> dict:
    """渲染 ArgoCD Application manifest（含金丝雀发布 steps 摘要）。"""
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": f"demo-app-{plan['patch_id']}",
            "namespace": "argocd",
            "labels": {
                "aiops.patch-id": plan["patch_id"],
                "aiops.alert-id": plan["alert_id"],
                "aiops.service": plan["service"],
            },
            "annotations": {"aiops.version": plan["version"]},
        },
        "spec": {
            "project": "aiops",
            "source": {
                "repoURL": "https://git.example.com/ops/demo-app.git",
                "targetRevision": f"refs/tags/{plan['patch_id']}",
                "path": "demo-app",
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "demo"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True}},
            "rollout": {
                "strategy": "canary",
                "image": plan["image"],
                "steps": [
                    {"setWeight": plan["traffic_percent"]},
                    {"pause": {"durationSeconds": plan["observe_seconds"]}},
                    {"setWeight": 100},
                ],
            },
        },
    }


class ArgoCDPublisher:
    """ArgoCD API 客户端：配置地址时真实提交；否则留痕（recorded 模式）。"""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (
            base_url if base_url is not None else os.environ.get("AIOPS_ARGOCD_URL", "")
        ).rstrip("/")
        self.token = token if token is not None else os.environ.get("AIOPS_ARGOCD_TOKEN", "")

    def apply(self, manifest: dict) -> dict:
        ARGOCD_DIR.mkdir(parents=True, exist_ok=True)
        record = ARGOCD_DIR / f"{manifest['metadata']['name']}.json"
        record.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        application = manifest["metadata"]["name"]
        if not self.base_url:
            return {"mode": "recorded", "application": application, "path": str(record)}
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/applications",
            data=json.dumps(manifest).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return {"mode": "live", "application": application, "status_code": resp.status}
        except (urllib.error.HTTPError, OSError) as exc:
            raise RuntimeError(f"ArgoCD 提交失败: {exc}") from exc


def _http_json(
    url: str, *, method: str = "GET", payload: dict | None = None, timeout: float = 5.0
) -> tuple[int, dict]:
    """极简 JSON HTTP 客户端（真实请求；HTTP 错误码作为正常返回）。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, _loads(body)
    except urllib.error.HTTPError as exc:
        return exc.code, _loads(exc.read().decode("utf-8", "replace"))


def _loads(body: str) -> dict:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


class DockerRolloutRunner:
    """本机真实滚动执行：镜像构建 / 金丝雀容器 / 探活观测 / 晋级与回收。"""

    name = "docker-rollout"

    def __init__(
        self,
        docker: str = "docker",
        *,
        stable_container: str | None = None,
        stable_port: int | None = None,
    ) -> None:
        self.docker = docker
        # 稳定容器名/端口可注入：**测试必须使用独立命名与端口**，否则会停掉/删除在跑的真实发布
        # （曾出现：跑全量单测时把已发布的 aiops-stable-order 容器 rm -f 掉）。
        self.stable_container = stable_container or STABLE_CONTAINER
        self.stable_port = stable_port if stable_port is not None else STABLE_PORT

    def _run(self, cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)

    def build_image(self, context_dir: Path, tag: str, *, version: str) -> str:
        proc = self._run(
            [self.docker, "build", "--label", f"aiops.version={version}", "-t", tag, str(context_dir)]
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"镜像构建失败（{tag}）：{tail}")
        return tag

    def start_canary(self, container: str, image: str, *, bad: bool, version: str) -> str:
        """启动金丝雀容器（随机端口）并返回可探活的基址。"""
        self.stop(container)  # 幂等防重：清理同名残留
        proc = self._run(
            [
                self.docker, "run", "-d", "--rm", "--name", container,
                "-e", f"APP_VERSION={version}",
                "-e", f"BAD_CANARY={'1' if bad else '0'}",
                "-p", f"127.0.0.1:0:{APP_PORT}",
                image,
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"金丝雀容器启动失败（{container}）：{tail}")
        return self._port_of(container)

    def _port_of(self, container: str) -> str:
        proc = self._run([self.docker, "port", container, str(APP_PORT)], timeout=15)
        lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        host_port = lines[0].rsplit(":", 1)[-1] if lines and ":" in lines[0] else ""
        if not host_port:
            raise RuntimeError(f"无法解析金丝雀端口: {container}")
        return f"http://127.0.0.1:{host_port}"

    def wait_ready(self, base_url: str) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                code, _ = _http_json(f"{base_url}/health", timeout=2)
                if code == 200:
                    return
            except OSError:
                pass
            time.sleep(0.3)
        raise RuntimeError(f"金丝雀未就绪（{READY_TIMEOUT_SECONDS}s 内 /health 未返回 200）: {base_url}")

    def observe(self, base_url: str, duration_seconds: int) -> dict:
        """真实观测窗口：周期 POST /order，统计错误率与延迟（P99 取窗口内最大值，小样本近似）。"""
        deadline = time.monotonic() + duration_seconds
        total = errors = 0
        latencies: list[float] = []
        while time.monotonic() < deadline:
            total += 1
            started = time.perf_counter()
            try:
                code, _ = _http_json(
                    f"{base_url}/order", method="POST", payload={"amount": 1000}, timeout=3
                )
                if code != 200:
                    errors += 1
            except OSError:
                errors += 1
            latencies.append((time.perf_counter() - started) * 1000)
            time.sleep(OBSERVE_INTERVAL_SECONDS)
        latencies.sort()
        p99 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))] if latencies else 0.0
        return {
            "total": total,
            "errors": errors,
            "error_rate": (errors / total * 100) if total else 100.0,
            "p99_latency_ms": p99,
        }

    def stop(self, container: str) -> None:
        subprocess.run(
            [self.docker, "rm", "-f", container], capture_output=True, timeout=30
        )

    def promote(self, canary_container: str, image: str, version: str) -> None:
        """全量晋级：停旧稳定 → 新稳定启动（真实滚动）→ 就绪校验 → 回收金丝雀。

        就绪校验通过前金丝雀保持运行（作为可用回退）；新稳定未就绪则清理并抛错（交由重试）。
        """
        self.stop(self.stable_container)
        proc = self._run(
            [
                self.docker, "run", "-d", "--name", self.stable_container,
                "-e", f"APP_VERSION={version}",
                "-p", f"127.0.0.1:{self.stable_port}:{APP_PORT}",
                image,
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"稳定版滚动失败：{tail}")
        try:
            self.wait_ready(f"http://127.0.0.1:{self.stable_port}")
        except Exception:
            self.stop(self.stable_container)
            raise
        self.stop(canary_container)


def run_canary(
    alert: Alert,
    patch: Patch,
    traffic_percent: int,
    observe_seconds: int = 10,
    *,
    repo_dir: Path | None = None,
    sandbox_root: Path | None = None,
    runner: DockerRolloutRunner | None = None,
    publisher: ArgoCDPublisher | None = None,
) -> CanaryResult:
    """金丝雀发布编排：工作区 → ArgoCD 发布 → 镜像构建 → 金丝雀运行 → 真实观测。"""
    repo_dir = Path(repo_dir) if repo_dir else code_rag.DEFAULT_REPO_DIR
    sandbox_root = Path(sandbox_root) if sandbox_root else sandbox.DEFAULT_SANDBOX_ROOT
    runner = runner or DockerRolloutRunner()
    version = release_version(patch.patch_id)

    workspace = sandbox_root / patch.patch_id
    if not (workspace / "order_service.py").is_file():
        workspace, prep_error = sandbox.prepare_workspace(patch, repo_dir, sandbox_root)
        if prep_error:
            raise RuntimeError(f"发布工作区准备失败: {prep_error}")

    image = f"{CANARY_IMAGE_PREFIX}:{patch.patch_id}"
    manifest = render_application(
        {
            "patch_id": patch.patch_id,
            "alert_id": patch.alert_id,
            "service": alert.service,
            "image": image,
            "traffic_percent": traffic_percent,
            "observe_seconds": observe_seconds,
            "version": version,
        }
    )
    publication = (publisher or ArgoCDPublisher()).apply(manifest)
    runner.build_image(workspace, image, version=version)

    container = f"aiops-canary-{patch.patch_id}"
    bad = "canary-bad" in alert.description.lower()
    base_url = runner.start_canary(container, image, bad=bad, version=version)
    try:
        runner.wait_ready(base_url)
        observed = runner.observe(base_url, observe_seconds)
    except Exception:
        runner.stop(container)  # 失败不留残留
        raise

    healthy = (
        observed["error_rate"] <= SLO_MAX_ERROR_RATE
        and observed["p99_latency_ms"] <= SLO_MAX_P99_LATENCY_MS
    )
    if healthy:
        observation = (
            f"金丝雀健康：错误率 {observed['error_rate']:.1f}%、P99 {observed['p99_latency_ms']:.0f}ms"
            f"（{observed['total']} 次真实请求，ArgoCD={publication['mode']}）"
        )
    else:
        reasons: list[str] = []
        if observed["error_rate"] > SLO_MAX_ERROR_RATE:
            reasons.append(f"错误率 {observed['error_rate']:.1f}% > SLO {SLO_MAX_ERROR_RATE:.0f}%")
        if observed["p99_latency_ms"] > SLO_MAX_P99_LATENCY_MS:
            reasons.append(
                f"P99 {observed['p99_latency_ms']:.0f}ms > SLO {SLO_MAX_P99_LATENCY_MS:.0f}ms"
            )
        observation = f"金丝雀劣化：{'；'.join(reasons)}（{observed['total']} 次真实请求）"

    return CanaryResult(
        patch_id=patch.patch_id,
        traffic_percent=traffic_percent,
        healthy=healthy,
        error_rate=round(observed["error_rate"], 2),
        p99_latency_ms=round(observed["p99_latency_ms"], 1),
        observation=observation,
    )


def run_finalize(
    canary: CanaryResult, auto_rollback: bool, *, runner: DockerRolloutRunner | None = None
) -> ReleaseResult:
    """发布终态：达标真实滚动到稳定版；劣化回收金丝雀（稳定版本保持不变）。"""
    runner = runner or DockerRolloutRunner()
    version = release_version(canary.patch_id)
    container = f"aiops-canary-{canary.patch_id}"
    if canary.healthy:
        runner.promote(container, f"{CANARY_IMAGE_PREFIX}:{canary.patch_id}", version)
        return ReleaseResult(
            version=version,
            rolled_back=False,
            reason=(
                f"金丝雀达标，已全量发布（{runner.stable_container} 运行于 :{runner.stable_port}，"
                f"镜像 {CANARY_IMAGE_PREFIX}:{canary.patch_id}）"
            ),
        )
    runner.stop(container)
    if auto_rollback:
        reason = "金丝雀劣化，自动回滚：金丝雀已回收，稳定版本保持不变"
    else:
        reason = "金丝雀劣化（自动回滚未开启）：金丝雀已回收，等待人工处理"
    return ReleaseResult(version=version, rolled_back=True, reason=reason)


def _page_probe_once(url: str, keyword: str) -> bool:
    """对真实页面请求一次：HTTP 200 且响应体包含契约关键词 → 健康。"""
    request = urllib.request.Request(url, headers={"User-Agent": "aiops-canary/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=3) as resp:
            body = resp.read(256 * 1024).decode("utf-8", "replace")
            return resp.status == 200 and (not keyword or keyword in body)
    except (OSError, urllib.error.URLError):
        return False


def run_canary_direct(
    patch: Patch,
    repo_dir: Path | str,
    url: str,
    keyword: str,
    traffic_percent: int,
    observe_seconds: int = 10,
) -> CanaryResult:
    """直连发布金丝雀（契约应用专用）：补丁直接应用到真实仓库，以真实页面探针观测。

    适用于本机开发态前端（vite dev 落盘即生效）等无容器交付面的应用：
    备份补丁涉及文件 → 按 diff 改写真实仓库 → 探针 URL（HTTP 200 且含契约关键词）
    → 健康：保持生效（=已全量发布）；劣化：还原补丁前文件（=真实回滚）。
    应用阶段任一文件失败：还原已写文件后抛错（不留半成品，交由 Temporal 重试）。
    """
    repo_dir = Path(repo_dir)
    backups: dict[Path, str] = {}
    try:
        sections = split_unified_diff(patch.diff)
        if len(sections) > 1:
            items = [(_normalize_rel(rel), file_diff) for rel, file_diff in sections.items()]
        else:
            items = [(_normalize_rel(patch.files[0]) if patch.files else "", patch.diff)]
        for rel, file_diff in items:
            target = repo_dir / rel
            if not target.is_file():
                raise RuntimeError(f"直连发布失败：仓库中目标文件不存在: {rel}")
            original = target.read_text(encoding="utf-8")
            new_text = apply_unified_diff(original, file_diff)
            if new_text is None:
                raise RuntimeError(f"直连发布失败：补丁 diff 无法应用（上下文不匹配）: {rel}")
            backups[target] = original
            target.write_text(new_text, encoding="utf-8")
    except Exception:
        for path, content in backups.items():
            path.write_text(content, encoding="utf-8")
        raise

    _page_probe_once(url, keyword)  # 预热一次（不计入统计）：热更新场景首个请求可能仍在重编译

    deadline = time.monotonic() + observe_seconds
    total = errors = 0
    latencies: list[float] = []
    while time.monotonic() < deadline:
        total += 1
        started = time.perf_counter()
        if not _page_probe_once(url, keyword):
            errors += 1
        latencies.append((time.perf_counter() - started) * 1000)
        time.sleep(OBSERVE_INTERVAL_SECONDS)
    latencies.sort()
    p99 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))] if latencies else 0.0
    error_rate = (errors / total * 100) if total else 100.0

    healthy = error_rate <= SLO_MAX_ERROR_RATE and p99 <= SLO_MAX_P99_LATENCY_MS
    if healthy:
        observation = (
            f"直连发布健康：错误率 {error_rate:.1f}%、P99 {p99:.0f}ms"
            f"（{total} 次真实页面探针，补丁已应用至真实仓库并生效）"
        )
    else:
        reasons: list[str] = []
        if error_rate > SLO_MAX_ERROR_RATE:
            reasons.append(f"错误率 {error_rate:.1f}% > SLO {SLO_MAX_ERROR_RATE:.0f}%")
        if p99 > SLO_MAX_P99_LATENCY_MS:
            reasons.append(f"P99 {p99:.0f}ms > SLO {SLO_MAX_P99_LATENCY_MS:.0f}ms")
        observation = (
            f"直连发布劣化：{'；'.join(reasons)}（{total} 次真实页面探针，已还原补丁前文件）"
        )
        for path, content in backups.items():
            path.write_text(content, encoding="utf-8")
    return CanaryResult(
        patch_id=patch.patch_id,
        traffic_percent=traffic_percent,
        healthy=healthy,
        error_rate=round(error_rate, 2),
        p99_latency_ms=round(p99, 1),
        observation=observation,
        mode="direct",
    )


def run_finalize_direct(canary: CanaryResult, auto_rollback: bool) -> ReleaseResult:
    """直连发布终态：健康 = 补丁已生效保持上线；劣化还原已由金丝雀阶段完成。"""
    version = release_version(canary.patch_id)
    if canary.healthy:
        return ReleaseResult(
            version=version,
            rolled_back=False,
            reason=f"直连发布达标，已全量发布（补丁已应用至真实仓库并生效: {canary.observation}）",
        )
    reason = (
        "直连发布劣化，自动回滚：补丁前文件已还原"
        if auto_rollback
        else "直连发布劣化（自动回滚未开启）：等待人工处理"
    )
    return ReleaseResult(version=version, rolled_back=True, reason=reason)
