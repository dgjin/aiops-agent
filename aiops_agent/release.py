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

import base64
import contextlib
import contextvars
import copy
import dataclasses
import http.client
import json
import os
import re
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from . import code_rag, sandbox, mode, kill_switch, release_journal as journal
from .release_journal import ReleaseSafetyError
from .fix_agent import (
    apply_unified_diff,
    is_new_file_diff,
    split_unified_diff,
)
from .models import Alert, CanaryResult, Patch, ReleaseResult
from .page_health import page_problem

BASE_DIR = Path(__file__).resolve().parent.parent
ARGOCD_DIR = mode.data_root() / "argocd"

CANARY_IMAGE_PREFIX = os.environ.get("AIOPS_CANARY_IMAGE_PREFIX", "aiops-demo-app")
STABLE_CONTAINER = os.environ.get("AIOPS_STABLE_CONTAINER", "aiops-stable-order")
STABLE_PORT = int(os.environ.get("AIOPS_STABLE_PORT", "18080"))
APP_PORT = 8000  # 容器内服务端口（http_server.py）

# 演示 SLO：金丝雀健康判定阈值（观测窗口内实测）
SLO_MAX_ERROR_RATE = 5.0  # %
SLO_MAX_P99_LATENCY_MS = 500.0  # ms
OBSERVE_INTERVAL_SECONDS = 0.5
READY_TIMEOUT_SECONDS = 15


_CANCEL = contextvars.ContextVar("release_cancel", default=None)
_RECOVERING = contextvars.ContextVar("release_recovering", default=False)


def checkpoint() -> None:
    if _RECOVERING.get():
        return
    cancel = _CANCEL.get()
    if cancel and cancel():
        raise ReleaseSafetyError("发布已取消，停止推进并恢复")
    try:
        kill_switch.require_release_allowed()
    except kill_switch.KillSwitchError as exc:
        raise ReleaseSafetyError("急停激活或状态不可读，停止推进并恢复") from exc


@contextlib.contextmanager
def recovering():
    token = _RECOVERING.set(True)
    try:
        yield
    finally:
        _RECOVERING.reset(token)


def _identity(patch_id, workflow_id, namespace):
    return journal.Journal(workflow_id or "local:" + patch_id,
                           namespace or mode.temporal_namespace(), patch_id)


def _validate_patch(patch):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", patch.patch_id):
        raise ReleaseSafetyError("非法patch标识")
    if patch.degraded:
        raise ReleaseSafetyError("降级补丁禁止发布")


def shadow_release_only() -> bool:
    """当前策略是否仅允许影子档（真实发布禁用）；demo 档恒为 False。

    production 必须显式声明 ``release_gate.shadow_mode``（启动校验）；本闸门按
    当前策略做最终防御：影子档的「只建议不执行」在发布层同样成立（防绕过工作
    流 shadow 分支的旁路调用），全链路档（false）即操作者已确认走真实发布链路。
    策略不可确认（文件缺失/校验失败）时 fail-closed：按影子档处理，禁止推进。
    """
    if not mode.is_production():
        return False
    try:
        from .config import load_policy

        return bool(load_policy().release_gate.shadow_mode)
    except Exception as exc:  # noqa: BLE001 - 策略不可读时禁止推进发布
        raise ReleaseSafetyError(f"生产发布策略不可确认，禁止推进发布：{exc}") from exc


def _production_gate():
    if shadow_release_only():
        raise ReleaseSafetyError(
            "影子档（release_gate.shadow_mode=true）禁止真实发布；"
            "确认交付目标与维护风险后切换全链路档"
        )


def _execute(j, stage, inputs, action, restore, result_type):
    with journal.resource_locks(j.record["resources"]):
        return _execute_locked(j, stage, inputs, action, restore, result_type)


def _execute_locked(j, stage, inputs, action, restore, result_type):
    prior = j.record["stages"].get(stage)
    if prior and prior["input_hash"] != journal.digest(inputs):
        raise ReleaseSafetyError("阶段输入hash冲突")
    if j.record and j.record["status"] == "running":
        _fail_and_restore(j, restore, "进程中断，已核对恢复；发布仍须人工确认")
    saved = j.begin(stage, inputs)
    if saved is not None:
        return result_type(**saved)
    try:
        checkpoint()
        result = action()
        checkpoint()
        j.finish(stage, dataclasses.asdict(result))
        return result
    except Exception as exc:
        _fail_and_restore(j, restore, str(exc) if isinstance(exc, ReleaseSafetyError)
                          else "发布或留痕异常，已停止推进")


def _fail_and_restore(j, restore, reason):
    verified = False
    try:
        with recovering():
            restore()
        verified = True
    except Exception:
        reason = "恢复失败或目标未知，保留证据与占用，必须人工处理"
    try:
        j.manual(verified, reason)
    except Exception:
        reason = "发布记录不可写；保留原占用与恢复材料，必须人工处理"
    raise ReleaseSafetyError(reason)


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
        checkpoint()
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
        checkpoint()
        return subprocess.run(cmd, capture_output=True, timeout=timeout)

    def build_image(self, context_dir: Path, tag: str, *, version: str) -> str:
        proc = self._run(
            [self.docker, "build", "--label", f"aiops.version={version}", "-t", tag, str(context_dir)]
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"镜像构建失败（{tag}）：{tail}")
        return tag

    def image_id(self, tag):
        value = json.loads(self._checked(["image", "inspect", "--format", "{{json .Id}}", tag]))
        if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ReleaseSafetyError("镜像digest不可确认")
        return value

    def start_canary(self, container: str, image: str, *, bad: bool, version: str) -> str:
        """启动金丝雀容器（随机端口）并返回可探活的基址。"""
        checkpoint()
        if self.inspect(container) is not None:
            raise ReleaseSafetyError("金丝雀名称已存在，拒绝删除未知容器")
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
            checkpoint()
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
            checkpoint()
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

    def _checked(self, args):
        proc = self._run([self.docker, *args], timeout=60)
        if proc.returncode:
            raise ReleaseSafetyError("容器操作失败，状态需核对")
        return proc.stdout

    def inspect(self, container):
        proc = self._run([self.docker, "inspect", "--type=container", container], timeout=15)
        if proc.returncode:
            if b"No such" in proc.stderr:
                return None
            raise ReleaseSafetyError("容器状态不可读")
        value = json.loads(proc.stdout)
        if not isinstance(value, list) or len(value) != 1 or not value[0].get("Id"):
            raise ReleaseSafetyError("容器状态不明确")
        return value[0]

    def stop(self, container: str) -> None:
        if self.inspect(container) is not None:
            self._checked(["rm", "-f", container])

    def _create_from_config(self, name, config):
        # Engine API完整传递Config/HostConfig，避免CLI重建丢失端口、挂载、权限和启动参数。
        context = os.environ.get("DOCKER_CONTEXT")
        endpoint = os.environ.get("DOCKER_HOST") if not context else None
        if not endpoint:
            endpoint = json.loads(self._checked(["context", "inspect", *([context] if context else []),
                                                  "--format", "{{json .Endpoints.docker.Host}} "]))
        if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
            raise ReleaseSafetyError("仅支持本机Unix Docker端点")
        checkpoint()
        conn = http.client.HTTPConnection("localhost", timeout=60)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.settimeout(60)
        try:
            conn.sock.connect(endpoint[7:])
            conn.request("POST", "/v1.41/containers/create?name=" + quote(name, safe=""),
                         body=json.dumps(config), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 201:
                raise ReleaseSafetyError("新稳定容器创建失败")
            return json.loads(body)["Id"]
        finally:
            conn.close()

    def promote(self, canary_container: str, image: str, version: str, *, record) -> None:
        checkpoint()
        old = self.inspect(self.stable_container)
        if old is None or not old["State"]["Running"] or old["HostConfig"].get("AutoRemove"):
            raise ReleaseSafetyError("旧稳定容器缺失、未运行或自动删除，不能安全晋级")
        initial = record.record.get("stable_before") or {}
        if any(old.get(k) != initial.get(k) for k in ("Id", "Config", "HostConfig", "Mounts")):
            raise ReleaseSafetyError("稳定容器被第三方修改，禁止晋级")
        if old["HostConfig"].get("NetworkMode") in {"host", "none"}:
            raise ReleaseSafetyError("不支持此网络模式的安全晋级")
        ports = old["HostConfig"].get("PortBindings") or {}
        if not any(str(p.get("HostPort")) == str(self.stable_port)
                   for values in ports.values() for p in (values or [])):
            raise ReleaseSafetyError("稳定容器端口与锁定目标不一致")
        if any(p.get("HostPort") in {"", "0"} for values in ports.values() for p in (values or [])):
            raise ReleaseSafetyError("随机稳定端口无法保证恢复，拒绝晋级")
        backup_name = self.stable_container + "-old-" + record.key[:12]
        if self.inspect(backup_name) is not None:
            raise ReleaseSafetyError("旧容器备份名称已占用")
        config = copy.deepcopy(old["Config"])
        config["Image"] = image
        config["Env"] = [v for v in config.get("Env", [])
                         if not v.startswith(("APP_VERSION=", "BAD_CANARY="))] + [
                             "APP_VERSION=" + version, "BAD_CANARY=0"]
        config["Labels"] = {**(config.get("Labels") or {}), "aiops.release-owner": record.key}
        config["HostConfig"] = copy.deepcopy(old["HostConfig"])
        # 保留实际挂载的卷名称，匿名卷也不能默默更换为空卷。
        if any(m.get("Type") == "volume" and not m.get("Name") for m in old.get("Mounts", [])):
            raise ReleaseSafetyError("卷身份未知")
        mounts = config["HostConfig"].setdefault("Mounts", []) or []
        config["HostConfig"]["Mounts"] = mounts
        bound = {v.split(":")[1] for v in (config["HostConfig"].get("Binds") or [])}
        for m in old.get("Mounts", []):
            if m["Type"] == "volume" and m["Destination"] not in bound and not any(
                    v.get("Target") == m["Destination"] for v in mounts):
                mounts.append({"Type": "volume", "Source": m["Name"],
                               "Target": m["Destination"], "ReadOnly": not m["RW"]})
        config["NetworkingConfig"] = {"EndpointsConfig": {
            name: {key: value for key, value in details.items()
                   if key in {"IPAMConfig", "Links", "Aliases", "DriverOpts"} and value}
            for name, details in old["NetworkSettings"]["Networks"].items()}}
        record.update(before={"container": old, "backup_name": backup_name}, step="promotion-prepared")
        checkpoint()
        self._checked(["stop", old["Id"]])
        self._checked(["rename", old["Id"], backup_name])
        record.update(step="old-retained")
        checkpoint()
        new_id = self._create_from_config(self.stable_container, config)
        record.update(new_id=new_id, step="new-created")
        self._checked(["start", new_id])
        self.wait_ready(f"http://127.0.0.1:{self.stable_port}")
        checkpoint()
        record.update(step="new-healthy")
        # 旧容器保留到finalize结果持久化后；清理失败不能损失回滚材料。

    def restore(self, record):
        old = record.record.get("before", {}).get("container")
        if not old:
            initial = record.record.get("stable_before")
            current = self.inspect(self.stable_container)
            if not initial or not current or any(current.get(k) != initial.get(k)
                    for k in ("Id", "Config", "HostConfig", "Mounts")) or not current["State"]["Running"]:
                raise ReleaseSafetyError("旧稳定容器状态未知")
            return
        current = self.inspect(self.stable_container)
        saved = self.inspect(old["Id"])
        if saved is None:
            raise ReleaseSafetyError("旧容器丢失")
        if any(saved.get(k) != old.get(k) for k in ("Config", "HostConfig", "Mounts")):
            raise ReleaseSafetyError("旧容器配置已被第三方修改")
        if current and current["Id"] != old["Id"]:
            if ((current.get("Config", {}).get("Labels") or {}).get("aiops.release-owner") != record.key
                    or (record.record.get("new_id") and current["Id"] != record.record["new_id"])):
                raise ReleaseSafetyError("稳定容器被第三方替换")
            self.stop(current["Id"])
        if saved["Name"].lstrip("/") != self.stable_container:
            self._checked(["rename", old["Id"], self.stable_container])
        self._checked(["start", old["Id"]])
        self.wait_ready(f"http://127.0.0.1:{self.stable_port}")


def _run_canary(
    alert: Alert,
    patch: Patch,
    traffic_percent: int,
    observe_seconds: int = 10,
    *,
    repo_dir: Path | None = None,
    sandbox_root: Path | None = None,
    runner: DockerRolloutRunner | None = None,
    publisher: ArgoCDPublisher | None = None,
    record=None,
) -> CanaryResult:
    """金丝雀发布编排：工作区 → ArgoCD 发布 → 镜像构建 → 金丝雀运行 → 真实观测。"""
    repo_dir = Path(repo_dir) if repo_dir else code_rag.DEFAULT_REPO_DIR
    sandbox_root = Path(sandbox_root) if sandbox_root else sandbox.default_sandbox_root()
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
    checkpoint()
    publication = (publisher or ArgoCDPublisher()).apply(manifest)
    checkpoint()
    runner.build_image(workspace, image, version=version)
    image = runner.image_id(image)
    record.update(image=image, version=version)

    container = "aiops-canary-" + record.key[:24]
    record.update(canary_container=container, step="canary-starting")
    bad = "canary-bad" in alert.description.lower()
    checkpoint()
    base_url = runner.start_canary(container, image, bad=bad, version=version)
    record.update(canary_id=runner.inspect(container)["Id"], step="observing")
    runner.wait_ready(base_url)
    checkpoint()
    observed = runner.observe(base_url, observe_seconds)
    checkpoint()

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


def _run_finalize(
    canary: CanaryResult, auto_rollback: bool, *, runner: DockerRolloutRunner, record
) -> ReleaseResult:
    """发布终态：达标真实滚动到稳定版；劣化回收金丝雀（稳定版本保持不变）。"""
    runner = runner or DockerRolloutRunner()
    version = record.record["version"]
    container = record.record["canary_container"]
    if canary.healthy:
        checkpoint()
        current = runner.inspect(container)
        if current is None or current["Id"] != record.record.get("canary_id"):
            raise ReleaseSafetyError("金丝雀目标身份未知，禁止晋级")
        runner.promote(container, record.record["image"], version, record=record)
        return ReleaseResult(
            version=version,
            rolled_back=False,
            reason=(
                f"金丝雀达标，已全量发布（{runner.stable_container} 运行于 :{runner.stable_port}，"
                f"镜像 {record.record['image']}）"
            ),
        )
    with recovering():
        _cleanup_canary(runner, record)
    if auto_rollback:
        reason = "金丝雀劣化，自动回滚：金丝雀已回收，稳定版本保持不变"
    else:
        reason = "金丝雀劣化（自动回滚未开启）：金丝雀已回收，等待人工处理"
    return ReleaseResult(version=version, rolled_back=True, reason=reason)


def _cleanup_canary(runner, record):
    name = record.record.get("canary_container")
    if not name:
        return
    current = runner.inspect(name)
    if current is not None:
        if current["Id"] != record.record.get("canary_id"):
            raise ReleaseSafetyError("金丝雀身份未知，保留现场转人工")
        runner.stop(current["Id"])


def _restore_container(runner, record):
    runner.restore(record)
    _cleanup_canary(runner, record)


def run_canary(alert, patch, traffic_percent, observe_seconds=10, *, repo_dir=None,
               sandbox_root=None, runner=None, publisher=None, workflow_id=None,
               namespace=None, cancel_check=None):
    _validate_patch(patch)
    _production_gate()
    runner = runner or DockerRolloutRunner()
    token = _CANCEL.set(cancel_check)
    try:
        with journal.locked():
            j = _identity(patch.patch_id, workflow_id, namespace)
            name, port = runner.stable_container, runner.stable_port
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                raise ReleaseSafetyError("稳定容器名称非法")
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ReleaseSafetyError("稳定端口非法")
            target = {"kind": "container", "name": name, "port": port}
            resources = ["container:" + name, "port:" + str(port)]
            if j.record is None:
                before = runner.inspect(name)
                if not before:
                    raise ReleaseSafetyError("旧稳定容器不存在，禁止无恢复前态发布")
                for bindings in (before["HostConfig"].get("PortBindings") or {}).values():
                    resources.extend("port:" + str(p["HostPort"]) for p in (bindings or []))
            else:
                before = j.record.get("stable_before")
                resources = j.record["resources"]
            j.claim(journal.digest({"diff": patch.diff, "files": patch.files}), target, sorted(set(resources)))
            if "stable_before" not in j.record:
                j.update(stable_before=before)
            def action():
                pub = publisher or ArgoCDPublisher()
                if isinstance(pub, ArgoCDPublisher) and pub.base_url:
                    raise ReleaseSafetyError("本机发布不兼容远程Argo副作用，拒绝双执行器")
                return _run_canary(alert, patch, traffic_percent, observe_seconds,
                                   repo_dir=repo_dir, sandbox_root=sandbox_root,
                                   runner=runner, publisher=pub, record=j)
            return _execute(j, "canary", {"traffic": traffic_percent, "observe": observe_seconds,
                            "alert": dataclasses.asdict(alert), "repo": str(repo_dir)}, action,
                            lambda: _restore_container(runner, j), CanaryResult)
    finally:
        _CANCEL.reset(token)


def run_finalize(canary, auto_rollback, *, runner=None, workflow_id=None, namespace=None,
                 cancel_check=None):
    return _finalize(canary, auto_rollback, runner=runner, workflow_id=workflow_id,
                     namespace=namespace, cancel_check=cancel_check)


def run_finalize_direct(canary, auto_rollback, *, workflow_id=None, namespace=None, cancel_check=None):
    return _finalize(canary, auto_rollback, workflow_id=workflow_id,
                     namespace=namespace, cancel_check=cancel_check)


def _finalize(canary, auto_rollback, *, runner=None, workflow_id=None, namespace=None, cancel_check=None):
    token = _CANCEL.set(cancel_check)
    try:
        with journal.locked():
            j = _identity(canary.patch_id, workflow_id, namespace)
            if j.record is None:
                raise ReleaseSafetyError("缺少canary记录，禁止按默认目标晋级")
            if j.record["stages"].get("canary", {}).get("result") != dataclasses.asdict(canary):
                raise ReleaseSafetyError("canary结果与持久化记录不符")
            target = j.record["target"]
            if target["kind"] != canary.mode:
                raise ReleaseSafetyError("发布类型不符")
            if canary.mode == "direct":
                restore = lambda: _restore_direct(j)
                def action():
                    _production_gate()
                    for rel, item in j.record["before"].items():
                        expected = item["after"] if canary.healthy else item["original"]
                        if _file_state(Path(target["repo"]), rel)[0] != expected:
                            raise ReleaseSafetyError("第三方编辑，禁止确认直连发布")
                    return _run_finalize_direct(canary, auto_rollback)
            else:
                runner = runner or DockerRolloutRunner(stable_container=target["name"], stable_port=target["port"])
                if runner.stable_container != target["name"] or runner.stable_port != target["port"]:
                    raise ReleaseSafetyError("finalize目标变化")
                restore = lambda: _restore_container(runner, j)
                def action():
                    _production_gate()
                    return _run_finalize(canary, auto_rollback, runner=runner, record=j)
            result = _execute(j, "finalize", {"canary": dataclasses.asdict(canary), "rollback": auto_rollback},
                              action, restore, ReleaseResult)
            if canary.mode == "container" and not j.record.get("cleanup_complete"):
                with journal.resource_locks(j.record["resources"]):
                    try:
                        old = j.record.get("before", {}).get("container")
                        if old:
                            checkpoint()
                            current = runner.inspect(target["name"])
                            if (not current or current["Id"] != j.record.get("new_id")
                                    or (current.get("Config", {}).get("Labels") or {}).get(
                                        "aiops.release-owner") != j.key or not current["State"]["Running"]):
                                raise ReleaseSafetyError("新稳定版身份未知，不能清理旧版本")
                            runner.wait_ready(f"http://127.0.0.1:{target['port']}")
                            _cleanup_canary(runner, j)
                            checkpoint()
                            runner.stop(old["Id"])
                        else:
                            with recovering():
                                _restore_container(runner, j)
                        j.update(cleanup_complete=True, status="terminal")
                    except Exception:
                        _fail_and_restore(j, restore, "稳定版确认或回收未完成，已停止推进")
            return result
    finally:
        _CANCEL.reset(token)


# 直连发布页面探针的响应读取上限（达上限视为截断，跳过 HTML 结构判定）
_PAGE_PROBE_READ_LIMIT = 256 * 1024


def _page_probe_once(url: str, keyword: str) -> bool:
    """对真实页面请求一次：HTTP 200 且通过页面判据（page_health：关键字/通用白屏检测）→ 健康。"""
    request = urllib.request.Request(url, headers={"User-Agent": "aiops-canary/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=3) as resp:
            raw = resp.read(_PAGE_PROBE_READ_LIMIT)
            if resp.status != 200:
                return False
            return not page_problem(
                raw.decode("utf-8", "replace"),
                keyword,
                truncated=len(raw) >= _PAGE_PROBE_READ_LIMIT,
            )
    except (OSError, urllib.error.URLError):
        return False


def _rel(raw):
    p = Path(raw)
    if (not raw or p.is_absolute() or ".." in p.parts or "\\" in raw
            or str(p) != raw or raw == "." or "\x00" in raw):
        raise ReleaseSafetyError("补丁路径非法")
    return raw


@contextlib.contextmanager
def _parent(root, rel, create=False):
    journal.plain(root)
    _rel(rel)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(rel).parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, Path(rel).name
    finally:
        os.close(fd)


def _read_at(fd, name):
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None, 0o644
    with os.fdopen(handle, "rb") as stream:
        s = os.fstat(stream.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
            raise ReleaseSafetyError("补丁目标不是单链接普通文件")
        return base64.b64encode(stream.read()).decode(), stat.S_IMODE(s.st_mode)


def _file_state(root, rel):
    journal.plain(root / _rel(rel))
    try:
        with _parent(root, rel) as (fd, name):
            return _read_at(fd, name)
    except FileNotFoundError:
        return None, 0o644


def _replace_file(root, rel, expected, content, permissions):
    journal.plain(root / rel)
    with _parent(root, rel, create=content is not None) as (fd, name):
        if _read_at(fd, name)[0] != expected:
            raise ReleaseSafetyError("第三方编辑，拒绝覆盖并保留前像")
        if content is None:
            os.unlink(name, dir_fd=fd)
        else:
            temp = ".aiops-" + os.urandom(12).hex()
            handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             permissions, dir_fd=fd)
            try:
                with os.fdopen(handle, "wb") as out:
                    out.write(base64.b64decode(content, validate=True))
                    out.flush()
                    os.fchmod(out.fileno(), permissions)
                    os.fsync(out.fileno())
                if _read_at(fd, name)[0] != expected:
                    raise ReleaseSafetyError("第三方编辑，拒绝覆盖并保留前像")
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
        os.fsync(fd)


def _prepare_direct(patch, root):
    sections = split_unified_diff(patch.diff)
    headers = [line[4:].strip() for line in patch.diff.splitlines() if line.startswith("+++ ")]
    if not sections or len(headers) != len(sections):
        raise ReleaseSafetyError("补丁文件头缺失或重复")
    for raw in patch.files:
        journal.plain(root / _rel(raw))
    for line in patch.diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw = line[4:].strip()
            if raw != "/dev/null":
                raw = raw[2:] if raw.startswith(("a/", "b/")) else raw
                journal.plain(root / _rel(raw))
    before = {}
    for rel, diff in sections.items():
        _rel(rel)
        original, permissions = _file_state(root, rel)
        if original is None and not is_new_file_diff(diff):
            raise ReleaseSafetyError("直连发布失败：仓库中目标文件不存在: " + rel)
        text = base64.b64decode(original).decode("utf-8") if original is not None else ""
        new = apply_unified_diff(text, diff)
        if new is None:
            raise ReleaseSafetyError("补丁上下文不匹配")
        if original is None and not new.endswith("\n"):
            new += "\n"
        after = base64.b64encode(new.encode()).decode()
        before[rel] = {"original": original, "after": after, "mode": permissions,
                       "before_hash": journal.digest(original), "after_hash": journal.digest(after)}
    return before


def _restore_direct(j):
    root = Path(j.record["target"]["repo"])
    files = j.record["before"]
    current = {rel: _file_state(root, rel)[0] for rel in files}
    if any(current[rel] not in (item["original"], item["after"]) for rel, item in files.items()):
        raise ReleaseSafetyError("第三方编辑，保留证据并转人工，不覆盖")
    for rel, item in files.items():
        if current[rel] != item["original"]:
            _replace_file(root, rel, current[rel], item["original"], item["mode"])
    if any(_file_state(root, rel)[0] != item["original"] for rel, item in files.items()):
        raise ReleaseSafetyError("文件恢复校验失败")


def run_canary_direct(patch, repo_dir, url, keyword, traffic_percent, observe_seconds=10,
                      *, service="", publisher=None, workflow_id=None, namespace=None,
                      cancel_check=None):
    _validate_patch(patch)
    _production_gate()
    root = journal.plain(Path(repo_dir).absolute())
    if not root.is_dir():
        raise ReleaseSafetyError("直连仓库不存在")
    root = root.resolve(strict=True)
    token = _CANCEL.set(cancel_check)
    try:
        with journal.locked():
            j = _identity(patch.patch_id, workflow_id, namespace)
            j.claim(journal.digest({"diff": patch.diff, "files": patch.files}),
                    {"kind": "direct", "repo": str(root)}, ["repo:" + str(root)])
            def action():
                before = _prepare_direct(patch, root)
                j.update(before=before, step="files-prepared")
                for rel, item in before.items():
                    checkpoint()
                    _replace_file(root, rel, item["original"], item["after"], item["mode"])
                checkpoint()
                pub = publisher or ArgoCDPublisher()
                if isinstance(pub, ArgoCDPublisher) and pub.base_url:
                    raise ReleaseSafetyError("直连发布不允许远程Argo副作用")
                manifest = render_application({"patch_id": patch.patch_id, "alert_id": patch.alert_id,
                    "service": service, "image": "direct:" + patch.patch_id, "traffic_percent": 100,
                    "observe_seconds": observe_seconds, "version": release_version(patch.patch_id)})
                manifest["spec"]["rollout"]["strategy"] = "direct-all-traffic"
                pub.apply(manifest)
                checkpoint()
                _page_probe_once(url, keyword)
                deadline = time.monotonic() + observe_seconds
                total = errors = 0
                latencies = []
                while time.monotonic() < deadline:
                    checkpoint()
                    total += 1
                    started = time.perf_counter()
                    errors += not _page_probe_once(url, keyword)
                    latencies.append((time.perf_counter() - started) * 1000)
                    time.sleep(OBSERVE_INTERVAL_SECONDS)
                checkpoint()
                error_rate, p99 = (errors / total * 100 if total else 100), max(latencies, default=0)
                healthy = error_rate <= SLO_MAX_ERROR_RATE and p99 <= SLO_MAX_P99_LATENCY_MS
                if not healthy:
                    with recovering():
                        _restore_direct(j)
                observation = "直连发布健康（全流量，非百分比金丝雀）" if healthy else "直连发布劣化，已还原补丁前文件"
                return CanaryResult(patch.patch_id, 100, healthy, round(error_rate, 2),
                                    round(p99, 1), observation, "direct")
            return _execute(j, "canary", {"url": url, "keyword": keyword, "traffic": traffic_percent,
                            "observe": observe_seconds, "service": service}, action,
                            lambda: _restore_direct(j), CanaryResult)
    finally:
        _CANCEL.reset(token)


def _run_finalize_direct(canary: CanaryResult, auto_rollback: bool) -> ReleaseResult:
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
