#!/usr/bin/env python3
"""单主机启动控制器：唯一组件表、配置快照、可验证进程身份与 fail-closed 守护。

up/restart 不播种、不自动切换基础设施。down 保留 launchd 注册与持久维护标志；
只有 uninstall 删除注册。所有网络/进程操作均在显式入口执行，import 无副作用。
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import plistlib
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv/bin/python"
LOCK_PATH = ROOT / "data/aiops-watchdog.lock"
DEFAULT_INTERVAL = 30.0
DEFAULT_SURGE_ARGS = "--min-lines 5 --factor 2 --cooldown 300"
DEFAULT_RSS_LIMIT_MB = 1024.0
LABEL = "com.aiops.watchdog"
CONTROLLED_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOGGER = logging.getLogger("aiops.watchdog")


class StartupError(RuntimeError):
    """可公开的脱敏启动错误。"""


@dataclass(frozen=True)
class Component:
    name: str
    pattern: str
    cmd: tuple[str, ...]
    log_path: Path


def _read_env_mode() -> str | None:
    from dotenv import dotenv_values
    return dotenv_values(ROOT / ".env").get("AIOPS_MODE")


def _effective_mode() -> tuple[str, str]:
    value = os.environ.get("AIOPS_MODE")
    source = "环境变量"
    if not value:
        value, source = _read_env_mode(), ".env"
    value, source = (value, source) if value else ("production", "默认值")
    value = value.strip().lower()
    if value not in ("demo", "production"):
        raise StartupError("AIOPS_MODE 仅支持 demo / production")
    return value, source


def load_environment() -> None:
    # shell 不 source .env，避免把配置当代码执行；测试调用前必须 import tests。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if "tests" not in sys.modules:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)


def _component_env() -> dict[str, str]:
    env = dict(os.environ)
    env["AIOPS_MODE"] = _effective_mode()[0]
    env["PATH"] = str(ROOT / ".venv/bin") + ":" + CONTROLLED_PATH
    env["PYTHONUNBUFFERED"] = "1"
    return env


def build_components() -> tuple[Component, ...]:
    production = _effective_mode()[0] == "production"
    python = ROOT / ".venv/bin/python"
    if not os.access(python, os.X_OK):
        if production:
            raise StartupError("production 缺少仓库 .venv/bin/python，拒绝系统 Python 回退")
        python = Path(sys.executable)
    surge = shlex.split(os.environ.get("AIOPS_WATCHDOG_SURGE_ARGS") or (
        "--min-lines 100 --factor 3 --cooldown 300" if production else DEFAULT_SURGE_ARGS))
    def script(name: str, *args: str) -> tuple[str, ...]:
        return (str(python), str(ROOT / name), *args)
    return (
        Component("worker", "aiops_agent.worker", (str(python), "-m", "aiops_agent.worker"), ROOT / "worker.log"),
        Component("bff", "uvicorn bff.app:app", (str(python), "-m", "uvicorn", "bff.app:app",
                  "--host", os.environ.get("AIOPS_BFF_HOST", "127.0.0.1"),
                  "--port", os.environ.get("AIOPS_BFF_PORT", "8600")), ROOT / "bff.log"),
        Component("alert_webhook", "alert_webhook.py", script("alert_webhook.py", "--host",
                  os.environ.get("AIOPS_WEBHOOK_HOST", "127.0.0.1"), "--port",
                  os.environ.get("AIOPS_WEBHOOK_PORT", "8099")), ROOT / "alert_webhook.log"),
        Component("app_prober", "app_prober.py", script("app_prober.py"), ROOT / "app_prober.log"),
        Component("ship_app_logs", "ship_app_logs.py", script("ship_app_logs.py", "--follow"), ROOT / "ship_app_logs.log"),
        Component("log_surge_detector", "log_surge_detector.py", script("log_surge_detector.py", *surge), ROOT / "log_surge_detector.log"),
    )


def maintenance_path() -> Path:
    return ROOT / "data/aiops-maintenance"


def maintenance_active() -> bool:
    path = maintenance_path()
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _safe_path(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise StartupError("运行控制文件不允许符号链接")
    if path.exists() and path.stat().st_uid != os.getuid():
        raise StartupError("运行控制文件所有者不符")


def _atomic_json(path: Path, value: dict) -> None:
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".watchdog-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def set_maintenance(reason: str) -> None:
    _atomic_json(maintenance_path(), {"repository": str(ROOT), "reason": reason})


def clear_maintenance() -> None:
    _safe_path(maintenance_path())
    maintenance_path().unlink(missing_ok=True)


def acquire_lock(logger: logging.Logger, path: Path | None = None) -> object | None:
    path = path or LOCK_PATH
    try:
        _safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        handle = os.fdopen(fd, "a+")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            handle.close()
            raise StartupError("锁文件必须是普通文件")
    except OSError:
        raise StartupError("单实例锁不可创建；拒绝无锁运行") from None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None
        raise StartupError("单实例锁不可用；拒绝无锁运行") from None
    return handle


@contextmanager
def control_lock():
    handle = acquire_lock(LOGGER, ROOT / "data/aiops-control.lock")
    if handle is None:
        raise StartupError("已有启动/停止/守护操作正在执行，请稍后重试")
    try:
        yield
    finally:
        handle.close()


def read_state() -> dict:
    path = ROOT / "data/aiops-processes.json"
    _safe_path(path)
    try:
        state = json.loads(path.read_text())
        if state.get("repository") != str(ROOT) or not isinstance(state.get("processes"), dict):
            raise ValueError
        return state
    except FileNotFoundError:
        return {"repository": str(ROOT), "processes": {}}
    except (OSError, ValueError, TypeError):
        raise StartupError("进程登记不可读或仓库不符；禁止接管") from None


def save_state(state: dict) -> None:
    _atomic_json(ROOT / "data/aiops-processes.json", state)


def _run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "PATH": CONTROLLED_PATH, "LC_ALL": "C"}, cwd=ROOT)


def _cwd(pid: int) -> str:
    if sys.platform == "darwin":
        result = _run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        names = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
        if result.returncode or len(names) != 1:
            raise StartupError("进程工作目录不可验证")
        return str(Path(names[0]).resolve())
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve(strict=True))
    except OSError:
        raise StartupError("进程工作目录不可验证") from None


def _process_gone(pid: int) -> bool:
    """进程已退出或僵尸：区分『检查间隙的正常消亡』与真正的身份不可验证。"""
    result = _run(["ps", "-ww", "-p", str(pid), "-o", "stat="])
    status = result.stdout.strip()
    return result.returncode != 0 or not status or status.startswith("Z")


def process_identity(pid: int) -> dict | None:
    if not isinstance(pid, int) or pid <= 1:
        raise StartupError("非法 PID")
    result = _run(["ps", "-ww", "-p", str(pid), "-o", "uid=", "-o", "lstart=", "-o", "stat=", "-o", "command="])
    if result.returncode == 1 and not result.stdout.strip():
        return None
    parts = result.stdout.strip().split(None, 7)
    if result.returncode or len(parts) != 8:
        raise StartupError("进程身份不可验证")
    if parts[6].startswith("Z"):
        return None
    try:
        cwd = _cwd(pid)
    except StartupError:
        # ps 与 lsof 之间进程可能已退出（停止/退出竞态）；确认确已消亡则视为无进程。
        if _process_gone(pid):
            return None
        raise
    return {"pid": pid, "uid": int(parts[0]), "started": " ".join(parts[1:6]),
            "command": parts[7], "cwd": cwd, "repository": str(ROOT)}


def verify_identity(record: dict) -> bool:
    if record.get("repository") != str(ROOT) or record.get("cwd") != str(ROOT) or record.get("uid") != os.getuid():
        raise StartupError("进程仓库或所有者不符；拒绝操作")
    current = process_identity(record["pid"])
    if current is None:
        return False
    if current != record:
        raise StartupError("PID 已复用或启动身份不符；拒绝操作")
    return True


def _matches(command: str, pattern: str) -> bool:
    return re.search(r"(?:^|[\s/])" + re.escape(pattern) + r"(?:\s|$)", command) is not None


def reject_unregistered(state: dict, components: tuple[Component, ...]) -> None:
    known = {r["pid"] for r in state["processes"].values()}
    if state.get("watchdog"):
        known.add(state["watchdog"]["pid"])
    result = _run(["ps", "-ww", "-axo", "pid=,command="])
    if result.returncode:
        raise StartupError("无法盘点已有进程，拒绝启动")
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[1]
        if pid in known or pid == os.getpid():
            continue
        # 启动控制器等待守护确认期间仍然存活，不是待接管的常驻守护。
        if re.search(r"aiops-watchdog\.py\s+(?:up|restart|install|resume|demo|down|stop|uninstall|status|env|preflight|maintenance)(?:\s|$)", command):
            continue
        if any(_matches(command, p) for p in [*(c.pattern for c in components), "aiops-watchdog.py"]):
            try:
                cwd = _cwd(pid)
            except StartupError:
                if _process_gone(pid):  # 盘点期间正常退出，不是待接管对象
                    continue
                raise
            if cwd == str(ROOT):
                raise StartupError("发现本仓库未登记旧进程；先人工核验停止，禁止隐式接管")


def _pids_for(pattern: str) -> list[int]:
    state = read_state()
    return [record["pid"] for record in state["processes"].values()
            if _matches(record["command"], pattern) and verify_identity(record)]


def is_running(pattern: str) -> bool:
    return bool(_pids_for(pattern))


def _pid_alive(pid: int) -> bool:
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    return process_identity(pid) is not None


def _rss_kb(pid: int) -> int | None:
    try:
        return int(_run(["ps", "-o", "rss=", "-p", str(pid)]).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _terminate_pids(pids: list[int], wait_s: float = 5.0, *, identities: dict | None = None) -> list[int]:
    if identities is None:
        state = read_state()
        records = [*state["processes"].values(), *([state["watchdog"]] if state.get("watchdog") else [])]
        identities = {r["pid"]: r for r in records}
    if any(pid not in identities for pid in pids):
        raise StartupError("拒绝停止未登记 PID")
    def alive(pid):
        return verify_identity(identities[pid])
    for pid in pids:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + wait_s
    remaining = [pid for pid in pids if alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in remaining if alive(pid)]
    # 不强杀业务进程；无法证明优雅退出时停止重启，留待维护窗口处置。
    return remaining


def prepare_snapshot() -> dict:
    from aiops_agent.config import startup_snapshot
    snapshot = startup_snapshot(ROOT)
    if snapshot["mode"] == "production" and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise StartupError("production 控制器必须在仓库 .venv 中运行")
    os.environ.update({"AIOPS_MODE": snapshot["mode"], "AIOPS_POLICY_PATH": snapshot["policy_path"],
                       "AIOPS_DATA_ROOT": snapshot["data_root"], "AIOPS_POLICY_SHA256": snapshot["policy_sha256"],
                       "TEMPORAL_NAMESPACE": snapshot["namespace"], "TEMPORAL_TASK_QUEUE": snapshot["task_queue"]})
    for name, listener in snapshot["listeners"].items():
        os.environ[f"AIOPS_{name.upper()}_HOST"] = listener["host"]
        os.environ[f"AIOPS_{name.upper()}_PORT"] = str(listener["port"])
    # 仅存摘要，原始数据库/Redis URL 与凭证绝不落入状态文件或日志。
    relevant = {key: value for key, value in os.environ.items()
                if key.startswith(("AIOPS_", "TEMPORAL_", "LOKI_")) and key != "AIOPS_LAUNCH_FINGERPRINT"}
    snapshot["fingerprint"] = hashlib.sha256(json.dumps([snapshot, relevant], sort_keys=True).encode()).hexdigest()
    pinned = os.environ.get("AIOPS_LAUNCH_FINGERPRINT")
    if pinned and pinned != snapshot["fingerprint"]:
        raise StartupError("launchd 配置快照不符；要求受控 restart/install")
    return snapshot


async def check_temporal() -> None:
    from temporalio.client import Client
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
    from aiops_agent.mode import temporal_namespace
    namespace = temporal_namespace()
    client = await asyncio.wait_for(Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
                                                   namespace=namespace), timeout=5)
    reply = await asyncio.wait_for(client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=namespace), timeout=timedelta(seconds=5)), timeout=5)
    if reply.namespace_info.name != namespace:
        raise StartupError("Temporal namespace 不符")


def check_dependencies(snapshot: dict) -> None:
    try:
        if _run(["docker", "info"], timeout=10).returncode:
            raise StartupError("Docker/Colima 不可用；未启动任何组件")
        asyncio.run(check_temporal())
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = os.environ.get("AIOPS_LOKI_URL", "http://localhost:3101").rstrip("/")
        with opener.open(url + "/ready", timeout=5) as response:
            if response.status != 200:
                raise StartupError("Loki 未就绪")
        if snapshot["mode"] == "production":
            from sqlalchemy import create_engine, text
            from sqlalchemy.pool import NullPool
            from redis import Redis
            engine = create_engine(os.environ["AIOPS_DATABASE_URL"], poolclass=NullPool,
                                   connect_args={"connect_timeout": 5, "read_timeout": 5, "write_timeout": 5})
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            finally:
                engine.dispose()
            redis = Redis.from_url(os.environ["AIOPS_REDIS_URL"], socket_connect_timeout=5, socket_timeout=5)
            try:
                if not redis.ping():
                    raise StartupError("Redis 未就绪")
            finally:
                redis.close()
    except StartupError:
        raise
    except Exception:
        raise StartupError("依赖预检失败（Docker/Temporal namespace/Loki/MySQL/Redis）；详情隐藏，未启动组件") from None


def check_ports(snapshot: dict, state: dict) -> None:
    for name, listener in snapshot["listeners"].items():
        component = "worker" if name == "metrics" else "alert_webhook" if name == "webhook" else name
        record = state["processes"].get(component)
        if record and verify_identity(record):
            continue
        try:
            with socket.socket(socket.AF_INET6 if ":" in listener["host"] else socket.AF_INET) as sock:
                # SO_REUSEADDR 与真实服务（uvicorn）绑定语义一致：仅拒绝活跃监听者。
                # 不设则 restart 停机后数秒内残留的 TIME_WAIT 会被误判为“已占用”（实测误报）。
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((listener["host"], listener["port"]))
        except OSError:
            raise StartupError(f"{name} 监听端口已占用；拒绝抢占") from None


def start_component(comp: Component) -> int:
    if maintenance_active():
        raise StartupError("维护标志有效，禁止拉起组件")
    state = read_state()
    with open(comp.log_path, "ab") as log:
        proc = subprocess.Popen(comp.cmd, cwd=ROOT, env=_component_env(), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    record = None
    try:
        time.sleep(0.2)
        if proc.poll() is not None:
            raise StartupError(f"{comp.name} 启动即退出，见组件日志")
        record = process_identity(proc.pid)
        if record is None or record["cwd"] != str(ROOT) or record["uid"] != os.getuid():
            raise StartupError("新进程身份无法验证")
        state["processes"][comp.name] = record
        save_state(state)
    except BaseException:
        # Popen 所有权仍在本进程，仅终止这个刚启动的子进程，不扫描其他进程。
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            set_maintenance("新组件退出未确认")
        raise
    return proc.pid


class RestartGuard:
    """滑动窗口内崩溃风暴冷却，各组件独立计数。"""
    def __init__(self, window_s=900.0, max_restarts=5, cooldown_s=600.0):
        self._window_s, self._max, self._cooldown_s = window_s, max_restarts, cooldown_s
        self._history, self._cooldown_until = {}, {}

    def allow(self, name: str, now: float) -> bool:
        until = self._cooldown_until.get(name)
        if until is None:
            return True
        if now >= until:
            self._cooldown_until.pop(name, None)
            self._history.pop(name, None)
            return True
        return False

    def record(self, name: str, now: float) -> None:
        history = [t for t in self._history.get(name, []) if now - t < self._window_s]
        history.append(now)
        self._history[name] = history
        if len(history) >= self._max:
            self._cooldown_until[name] = now + self._cooldown_s

    def cooldown_remaining(self, name: str, now: float) -> float:
        return max(0.0, self._cooldown_until.get(name, 0.0) - now)


def check_once(components, guard, now, logger, dry_run=False, running_fn=is_running, start_fn=start_component) -> int:
    if maintenance_active():
        return 0
    started = []
    try:
        for comp in components:
            if running_fn(comp.pattern) or not guard.allow(comp.name, now) or dry_run:
                continue
            started.append(start_fn(comp))
            guard.record(comp.name, now)
    except Exception:
        set_maintenance("组件启动失败")
        if started:
            _terminate_pids(started)
        raise
    return len(started)


def check_memory_once(components, guard, now, logger, limit_mb, dry_run=False,
                      pids_fn=_pids_for, rss_fn=_rss_kb, kill_fn=_terminate_pids, start_fn=start_component) -> int:
    if limit_mb <= 0 or maintenance_active():
        return 0
    restarted = 0
    for comp in components:
        pids = pids_fn(comp.pattern)
        readings = [v for v in (rss_fn(pid) for pid in pids) if v is not None]
        if not readings or max(readings) <= limit_mb * 1024 or dry_run or not guard.allow(comp.name, now):
            continue
        if kill_fn(pids):
            raise StartupError("旧进程退出未确认，禁止重启")
        start_fn(comp)
        guard.record(comp.name, now)
        restarted += 1
    return restarted


def reconcile(snapshot: dict, components: tuple[Component, ...], guard: RestartGuard, rss_mb: float) -> None:
    if maintenance_active():
        return
    state = read_state()
    live = [r for r in state["processes"].values() if verify_identity(r)]
    if state.get("watchdog") and verify_identity(state["watchdog"]):
        live.append(state["watchdog"])
    if live and state.get("fingerprint") != snapshot["fingerprint"]:
        raise StartupError("运行进程档位/策略/配置不一致；请受控 restart，拒绝接管")
    reject_unregistered(state, components)
    # 所有依赖与端口在第一条 Popen 之前检查，守护重拉也不得绕过。
    check_dependencies(snapshot)
    check_ports(snapshot, state)
    state.update(snapshot=snapshot, fingerprint=snapshot["fingerprint"])
    save_state(state)
    now = time.monotonic()
    try:
        check_once(components, guard, now, LOGGER)
        check_memory_once(components, guard, now, LOGGER, rss_mb)
        if any(not is_running(comp.pattern) for comp in components):
            raise StartupError("组件退出或仍处于冷却中，未完整启动")
    except Exception:
        # 回收本轮新增进程，不因后来组件失败留下半套新实例。
        new = read_state()
        pids = [r["pid"] for name, r in new["processes"].items() if r != state["processes"].get(name)]
        if pids:
            set_maintenance("组件巡检失败")
            _terminate_pids(pids)
        raise


def run_status(components, logger) -> int:
    state = read_state()
    missing = sum(not is_running(c.pattern) for c in components)
    watchdog = state.get("watchdog")
    logger.info("维护=%s；缺失组件=%d；守护=%s", maintenance_active(), missing,
                bool(watchdog and verify_identity(watchdog)))
    if state.get("snapshot"):
        logger.info("启动快照：%s", json.dumps(state["snapshot"], ensure_ascii=False))
    return int(bool(missing) or maintenance_active() or not watchdog or not verify_identity(watchdog))


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def validate_plist() -> dict | None:
    path = plist_path()
    _safe_path(path)
    if not path.exists():
        return None
    config = plistlib.loads(path.read_bytes())
    if (config.get("Label") != LABEL or config.get("WorkingDirectory") != str(ROOT)
            or config.get("ProgramArguments", [])[:2] != [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/aiops-watchdog.py")]):
        raise StartupError("launchd 注册属于其他仓库/解释器；拒绝操作")
    return config


def launchctl(*args: str, required=True) -> bool:
    if args and args[0] in ("bootout", "kickstart"):
        live = _run(["launchctl", "print", launch_target()])
        expected = {"program": str(ROOT / ".venv/bin/python"), "working directory": str(ROOT)}
        fields = dict(line.strip().split(" = ", 1) for line in live.stdout.splitlines() if " = " in line)
        if live.returncode or any(fields.get(key) != value for key, value in expected.items()):
            raise StartupError("运行中的 launchd 任务身份不符；拒绝操作其他项目")
    result = _run(["launchctl", *args])
    if required and result.returncode:
        raise StartupError("launchd 操作失败；维护状态保留")
    return result.returncode == 0


def launch_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def start_supervisor(snapshot: dict) -> None:
    state = read_state()
    existing = state.get("watchdog")
    if existing and verify_identity(existing):
        if state.get("fingerprint") != snapshot["fingerprint"]:
            raise StartupError("已有守护配置不符；请受控 restart")
        return
    state.pop("supervisor_ready", None)
    save_state(state)
    config = validate_plist() if sys.platform == "darwin" else None
    if config:
        pinned = config.get("EnvironmentVariables", {}).get("AIOPS_LAUNCH_FINGERPRINT")
        if pinned != snapshot["fingerprint"]:
            raise StartupError("launchd 快照与当前配置不符；先显式 install 更新注册")
        if not launchctl("print", launch_target(), required=False):
            launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path()))
        launchctl("kickstart", launch_target())
    else:
        # 守护同样使用统一组件启动器记录启动身份，但不加入业务组件表。
        comp = Component("watchdog", "aiops-watchdog.py", (str(ROOT / ".venv/bin/python"),
                         str(ROOT / "scripts/aiops-watchdog.py")), ROOT / "aiops_watchdog.log")
        start_component(comp)
        state = read_state()
        state["watchdog"] = state["processes"].pop("watchdog")
        save_state(state)


def stop_services() -> None:
    if not maintenance_active():
        set_maintenance("stop")
    with control_lock():
        state = read_state()
        reject_unregistered(state, build_components())
        records = [*state["processes"].values(), *([state["watchdog"]] if state.get("watchdog") else [])]
        # 先验证全体，任何一个身份异常均不向其他 PID 发送信号。
        for record in records:
            verify_identity(record)
        for record in reversed(records):
            if _terminate_pids([record["pid"]]):
                raise StartupError("进程未优雅退出；维护标志保留，禁止半套重启")
        state["processes"] = {}
        state.pop("watchdog", None)
        state.pop("supervisor_ready", None)
        save_state(state)


def install_launchd(snapshot: dict) -> None:
    if sys.platform != "darwin":
        raise StartupError("仅支持 macOS 用户级 launchd")
    validate_plist()
    check_dependencies(snapshot)
    state = read_state()
    reject_unregistered(state, build_components())
    check_ports(snapshot, state)
    if any(verify_identity(r) for r in state["processes"].values()) and state.get("fingerprint") != snapshot["fingerprint"]:
        raise StartupError("运行档位不一致；先受控 stop，禁止 install 接管")
    set_maintenance("install")
    if launchctl("print", launch_target(), required=False):
        launchctl("bootout", launch_target())
    with control_lock():
        state = read_state()
        if state.get("watchdog") and _terminate_pids([state["watchdog"]["pid"]]):
            raise StartupError("旧守护未退出，禁止接管")
        template = plistlib.loads((ROOT / "deploy/launchd/com.aiops.watchdog.plist").read_bytes())
        template.update(ProgramArguments=[str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/aiops-watchdog.py")],
                        WorkingDirectory=str(ROOT), StandardOutPath=str(ROOT / "aiops_watchdog.log"),
                        StandardErrorPath=str(ROOT / "aiops_watchdog.log"))
        write_plist(snapshot, template)
        clear_maintenance()
    try:
        launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path()))
        await_supervisor(snapshot)
    except Exception:
        set_maintenance("launchd 安装启动失败")
        raise


def write_plist(snapshot: dict, template: dict) -> None:
    # 只固化非敏感运行参数；凭证仍从已有 .env/受控环境取，不写入 plist。
    keys = ["AIOPS_MODE", "AIOPS_POLICY_PATH", "AIOPS_DATA_ROOT", "TEMPORAL_NAMESPACE", "TEMPORAL_TASK_QUEUE"]
    keys += [f"AIOPS_{name}_{kind}" for name in ("BFF", "WEBHOOK", "METRICS") for kind in ("HOST", "PORT")]
    template["EnvironmentVariables"] = {key: os.environ[key] for key in keys}
    template["EnvironmentVariables"].update(PATH=str(ROOT / ".venv/bin") + ":" + CONTROLLED_PATH,
                                            AIOPS_LAUNCH_FINGERPRINT=snapshot["fingerprint"])
    path = plist_path()
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".aiops-plist-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(template, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def await_supervisor(snapshot: dict, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state()
        record = state.get("watchdog")
        if (state.get("supervisor_ready") == snapshot["fingerprint"] and record
                and verify_identity(record)):
            return
        time.sleep(0.2)
    raise StartupError("守护未确认启动；请检查日志，不能视为完整启动")


def demo_seed() -> None:
    from aiops_agent.mode import is_demo
    if not is_demo():
        raise StartupError("production 禁止演示播种；不会自动覆盖 AIOPS_MODE")
    if maintenance_active():
        raise StartupError("维护中禁止演示播种")
    service = os.environ.get("AIOPS_DEMO_SERVICE", "order")
    for mode, count in (("surge", "800"), ("normal", "60")):
        subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "demo_log_generator.py"),
                        "--service", service, "--mode", mode, "--count", count], cwd=ROOT, env=_component_env(), check=True)
    subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "demo_cli.py"), "start", "--service", service,
                    "--alert-id", f"demo-{time.time_ns()}", "--description", "显式演示"], cwd=ROOT, env=_component_env(), check=True)


def control(action: str) -> int:
    if action in ("down", "stop"):
        stop_services()
        return 0
    if action == "uninstall":
        validate_plist()
        stop_services()
        if launchctl("print", launch_target(), required=False):
            launchctl("bootout", launch_target())
        plist_path().unlink(missing_ok=True)
        return 0
    if action == "maintenance":
        set_maintenance("operator")
        return 0
    if action == "status":
        return run_status(build_components(), LOGGER)
    if action == "down-all":
        raise StartupError("不自动停止共享依赖；请在已批准维护窗口使用 E 批次显式项目/卷配置")
    snapshot = prepare_snapshot()
    LOGGER.info("有效启动配置：%s", json.dumps(snapshot, ensure_ascii=False))
    if action in ("preflight", "env"):
        check_dependencies(snapshot)
        check_ports(snapshot, read_state())
        return 0
    if action == "install":
        install_launchd(snapshot)
        return 0
    if action == "demo" and snapshot["mode"] != "demo":
        raise StartupError("production 禁止演示播种")
    registered = validate_plist() if sys.platform == "darwin" else None
    if action == "restart":
        # 配置/依赖先验证，失败不停止现有业务；停止后的失败保持维护。
        check_dependencies(snapshot)
        stop_services()
        if registered:
            if launchctl("print", launch_target(), required=False):
                launchctl("bootout", launch_target())
            write_plist(snapshot, registered)
    elif registered and registered.get("EnvironmentVariables", {}).get("AIOPS_LAUNCH_FINGERPRINT") != snapshot["fingerprint"]:
        raise StartupError("托管配置与当前配置不符；请受控 restart")
    with control_lock():
        state = read_state()
        # 手工 maintenance 必须显式 resume；普通 up 只能恢复 stop 状态。
        if maintenance_active() and action not in ("resume", "restart"):
            try:
                reason = json.loads(maintenance_path().read_text()).get("reason")
            except (OSError, ValueError, AttributeError):
                reason = "unknown"
            if reason != "stop":
                raise StartupError("维护标志有效；确认维护完成后显式 resume")
        try:
            # 维护期也先检查全部依赖，确认后才允许任何组件拉起。
            check_dependencies(snapshot)
            check_ports(snapshot, state)
            clear_maintenance()
            reconcile(snapshot, build_components(), RestartGuard(), DEFAULT_RSS_LIMIT_MB)
            start_supervisor(snapshot)
        except Exception:
            set_maintenance("启动失败")
            # 仅回收本次新启动组件，保留此前健康实例。
            new = read_state()
            pids = [r["pid"] for name, r in new["processes"].items()
                    if r != state["processes"].get(name)]
            if pids:
                _terminate_pids(pids)
            raise
    try:
        await_supervisor(snapshot)
    except Exception:
        set_maintenance("守护未确认启动")
        new = read_state()
        pids = [r["pid"] for name, r in new["processes"].items() if r != state["processes"].get(name)]
        if pids:
            _terminate_pids(pids)
        raise
    if action == "demo":
        demo_seed()
    LOGGER.info("组件已启动（非业务 readiness 证明）；生产验收仍需 G2/G3")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["up", "down", "stop", "restart", "status", "env", "preflight",
                        "demo", "maintenance", "resume", "install", "uninstall", "down-all"])
    parser.add_argument("--interval", type=float, default=float(os.environ.get("AIOPS_WATCHDOG_INTERVAL", DEFAULT_INTERVAL)))
    parser.add_argument("--rss-mb", type=float, default=float(os.environ.get("AIOPS_WATCHDOG_RSS_MB", DEFAULT_RSS_LIMIT_MB)))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    load_environment()
    from aiops_agent.mode import ModeConfigError
    try:
        if args.action or args.status:
            return control(args.action or "status")
        if maintenance_active():
            LOGGER.warning("维护标志有效，守护不拉起任何组件")
            return 0
        if args.interval <= 0 or args.rss_mb < 0:
            raise StartupError("守护周期/内存阈值非法")
        snapshot = prepare_snapshot()
        components = build_components()
        lock = acquire_lock(LOGGER)
        if lock is None:
            state = read_state()
            if (state.get("fingerprint") != snapshot["fingerprint"] or not state.get("watchdog")
                    or not verify_identity(state["watchdog"])):
                raise StartupError("锁已占用但守护身份/档位不可确认；要求受控 restart")
            return 0
        try:
            guard = RestartGuard()
            while True:
                if maintenance_active():
                    return 0
                try:
                    with control_lock():
                        if not args.dry_run:
                            reconcile(snapshot, components, guard, args.rss_mb)
                            state = read_state()
                            state["watchdog"] = process_identity(os.getpid())
                            state["supervisor_ready"] = snapshot["fingerprint"]
                            save_state(state)
                except StartupError:
                    if args.once:
                        raise
                    LOGGER.error("巡检被安全门禁阻断；等待下一轮（未启动新组件）")
                    time.sleep(min(args.interval, 1))
                    continue
                if args.once:
                    return 0
                time.sleep(args.interval)
        finally:
            lock.close()
    except (Exception, KeyboardInterrupt) as exc:
        LOGGER.error("操作失败：%s", str(exc) if isinstance(exc, (StartupError, ModeConfigError)) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
