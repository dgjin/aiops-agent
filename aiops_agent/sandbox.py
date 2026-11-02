"""沙箱验证执行器（WP6 交付物）：隔离运行时内真实执行补丁后的测试。

流程（对齐实施计划 WP6 / M4）：
    run_patch_tests(patch, attempt)
      → scan_patch_diff：SAST 演示实现——对新增行做高危模式静态扫描；
      → prepare_workspace：把被监控仓库复制到独立工作区 data/sandbox/<patch_id>/ 并应用补丁 diff；
      → 按仓库形态选择执行计划（Python 套件 / Node 构建与测试 / 静态契约）；
      → 隔离运行时内执行（工作区只读挂载，Node 构建目录可写）；
      → 解析输出 + 退出码 → TestReport（含 verification_mode）。

隔离语义（生产 vs 本机对照，M4 加固）：
    - 生产形态：K8sJobSandboxRunner（本文件内）渲染 batch/v1 Job——
      runtimeClassName: gvisor（用户态内核，系统调用隔离）、
      readOnlyRootFilesystem + 全部 capabilities drop、runAsNonRoot/nobody、
      seccomp RuntimeDefault、Resources limits（256Mi/1CPU）、
      activeDeadlineSeconds（超时即杀）、backoffLimit=0、ttlSecondsAfterFinished；
      配合 aiops-sandbox namespace 级 default-deny NetworkPolicy（无出网权限）。
    - 本机形态：DockerSandboxRunner 用 docker run 对齐同一语义——
      --network none、--read-only + --tmpfs、--memory/--cpus/--pids-limit、
      --user nobody、--cap-drop ALL、no-new-privileges、seccomp（默认档或显式档）、
      --rm 即用即毁、-v <workspace>:/work:ro（Node 构建需要写目录时按计划放开）。
    - 体积护栏：stdout/stderr 上限（默认 256KiB/流）与工作区/产物配额（默认 256MiB）；
      超时后显式回收容器并核对不存在，不留孤儿容器。

镜像固定：production 要求 ``AIOPS_SANDBOX_IMAGE``（或 Node 镜像）带 ``@sha256:``
摘要，未固定时拒绝执行；demo 允许 tag 但报告中标注来源。

环境变量：
    AIOPS_SANDBOX_IMAGE          沙箱镜像（默认 python:3.12-slim）
    AIOPS_SANDBOX_NODE_IMAGE     Node 仓库镜像（默认 node:22-bookworm-slim）
    AIOPS_SANDBOX_ROOT           沙箱产物根目录（默认 data/sandbox）
    AIOPS_SANDBOX_MAX_STREAM_KB  单流输出上限（默认 256）
    AIOPS_SANDBOX_MAX_WORKSPACE_MB  工作区与产物配额（默认 256）
    AIOPS_SANDBOX_SECCOMP        自定义 seccomp 档路径（缺省用运行时默认档）
    AIOPS_SANDBOX_RUNTIME        docker（默认）| k8s
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import code_rag, metrics, mode
from .fix_agent import _normalize_rel, apply_unified_diff, is_new_file_diff, split_unified_diff
from .models import Patch, TestReport

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SANDBOX_ROOT = BASE_DIR / "data" / "sandbox"
SANDBOX_IMAGE = os.environ.get("AIOPS_SANDBOX_IMAGE", "python:3.12-slim")
NODE_SANDBOX_IMAGE = os.environ.get("AIOPS_SANDBOX_NODE_IMAGE", "node:22-bookworm-slim")

# verification_mode 取值（TestReport）：仅 UNIT_TEST 计入真实沙箱通过率
MODE_UNIT_TEST = "UNIT_TEST"
MODE_CONTRACT_TEST = "CONTRACT_TEST"
MODE_STATIC_ONLY = "STATIC_ONLY"

DEFAULT_MAX_STREAM_BYTES = 256 * 1024
DEFAULT_MAX_WORKSPACE_MB = 256
_SECCOMP_PROFILE = os.environ.get("AIOPS_SANDBOX_SECCOMP", "").strip()

# Node 步骤标记：容器内 sh 脚本输出，用于定位失败步骤（不经 shell 拼接用户输入）
_STEP_START = "__AIOPS_STEP_START__"
_STEP_OK = "__AIOPS_STEP_OK__"



def default_sandbox_root() -> Path:
    """沙箱产物根目录（惰性解析）：AIOPS_SANDBOX_ROOT 可覆盖，缺省 data/sandbox。

    生产建议指向独立卷/tmpfs（P1③，2026-10-12）——清理与写满只影响沙箱产物，不波及
    data/ 下的审计/令牌/清单等持久数据；惰性读取保证 .env（config.load_dotenv）生效后取值。
    """
    return Path(os.environ.get("AIOPS_SANDBOX_ROOT") or DEFAULT_SANDBOX_ROOT)

# 容器内执行的测试命令（与本地基线一致：CWD=demo-app 根）
DEFAULT_COMMAND = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

# 高危模式（SAST 演示实现）：新增行命中即拦截补丁进入审批链路
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\beval\s*\("), "eval()"),
    (re.compile(r"\bexec\s*\("), "exec()"),
    (re.compile(r"\bos\.system\s*\("), "os.system()"),
    (re.compile(r"\bsubprocess\b"), "subprocess 调用"),
    (re.compile(r"\bsocket\b"), "原生 socket"),
    (re.compile(r"\b(?:requests|urllib\.request)\b"), "外网请求库"),
    (re.compile(r"\brm\s+-rf\b"), "rm -rf"),
    (re.compile(r"(?i)\b(?:password|passwd|api_key|apikey|secret|token)\s*=\s*[\"'][^\"']+[\"']"), "硬编码敏感信息"),
]

# 前端/浏览器侧文件扩展名：补丁文件全为此类时改用前端模式集（见 scan_patch_diff）。
# 大小写不敏感（.TS/.Ts 等一律归入）。
_FRONTEND_SUFFIXES = {
    ".html", ".htm", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".vue", ".svelte", ".css", ".scss",
}

# 前端文件的高危模式：通用集里的 exec(/socket/requests 在 JS 生态属常见写法
# （regex.exec(、socket.io 变量、requests 变量名），直接套用会把正常需求改动误判拦截；
# 前端集聚焦浏览器代码中真实危险的行为：动态执行、系统命令后门、外泄口令。
_FRONTEND_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\beval\s*\("), "eval()"),
    (re.compile(r"\bnew\s+Function\s*\("), "new Function()"),
    (re.compile(r"\bos\.system\s*\("), "os.system()"),
    (re.compile(r"\bchild_process\b"), "child_process"),
    (re.compile(r"\brm\s+-rf\b"), "rm -rf"),
    (re.compile(r"(?i)\b(?:password|passwd|api_key|apikey|secret|token)\s*=\s*[\"'][^\"']+[\"']"), "硬编码敏感信息"),
]

_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".venv", ".git", "data",
    "node_modules", "dist", "build", "coverage", ".next", "out", "logs",
)

@dataclass
class SandboxJob:
    """一次沙箱执行任务（生产侧映射为 K8s Job 定义）。"""

    patch_id: str
    workspace: Path
    image: str = SANDBOX_IMAGE
    command: list[str] = field(default_factory=lambda: list(DEFAULT_COMMAND))
    timeout_seconds: int = 90
    network_disabled: bool = True
    memory_limit: str = "256m"
    cpus: str = "1"
    pids_limit: int = 128
    # 执行形态（UNIT_TEST / STATIC_ONLY）；CONTRACT_TEST 不进入运行时
    verification_mode: str = MODE_UNIT_TEST
    # Node 构建/测试需要写工作区（构建产物）；Python 套件保持只读
    workspace_writable: bool = False
    # 步骤名（Node 的 build/typecheck/test；Python 为空表示单一 unittest 套件）
    steps: list[str] = field(default_factory=list)
    image_pull_policy: str = "IfNotPresent"


@dataclass
class SandboxOutcome:
    """沙箱执行结果（退出码 + 解析后的测试结论 + 原始输出留痕）。"""

    exit_code: int
    passed: bool
    tests_run: int
    duration_seconds: float
    runner: str
    image: str
    timed_out: bool = False
    failed_tests: list[str] = field(default_factory=list)
    error_summary: str = ""
    stdout: str = ""
    stderr: str = ""
    verification_mode: str = MODE_UNIT_TEST
    steps: list[dict] = field(default_factory=list)
    output_truncated: bool = False
    image_pinned: bool = False
    cleanup_verified: bool = True


@dataclass
class ProjectPlan:
    """仓库执行计划：由仓库形态决定镜像、命令与验收口径。"""

    kind: str  # python / node / static
    verification_mode: str
    image: str = SANDBOX_IMAGE
    command: list[str] = field(default_factory=lambda: list(DEFAULT_COMMAND))
    steps: list[str] = field(default_factory=list)
    workspace_writable: bool = False
    # 静态契约/自动静态：不进入运行时（保留原因供报告说明）
    static_reason: str = ""


@dataclass
class _ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def max_stream_bytes() -> int:
    return _env_int("AIOPS_SANDBOX_MAX_STREAM_KB", DEFAULT_MAX_STREAM_BYTES // 1024) * 1024


def max_workspace_bytes() -> int:
    return _env_int("AIOPS_SANDBOX_MAX_WORKSPACE_MB", DEFAULT_MAX_WORKSPACE_MB) * 1024 * 1024


def image_is_pinned(image: str) -> bool:
    """镜像是否以摘要固定（tag 可被覆盖，不能作为可复现依据）。"""
    return "@sha256:" in (image or "")


def resolve_image_error(image: str) -> str:
    """production 未固定镜像摘要时返回错误文案（空串=通过）。"""
    if mode.is_production() and not image_is_pinned(image):
        return (
            f"production 要求沙箱镜像固定摘要（@sha256:），当前为 {image!r}；"
            "请设置 AIOPS_SANDBOX_IMAGE 为 python:<tag>@sha256:<digest> 形式"
        )
    return ""


def dir_size_bytes(root: Path) -> int:
    total = 0
    for path in Path(root).rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def workspace_quota_error(root: Path, limit: int | None = None) -> str:
    """工作区/产物配额检查（超限返回错误文案，空串=通过）。"""
    limit = limit or max_workspace_bytes()
    size = dir_size_bytes(root)
    if size <= limit:
        return ""
    return f"沙箱工作区体积 {size}B 超过配额 {limit}B（{limit // (1024 * 1024)}MiB）"


def parse_step_output(text: str) -> list[dict]:
    """解析 Node 步骤标记，返回 [{name, ok}]；未标记输出返回空列表。"""
    steps: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(_STEP_START):
            name = line[len(_STEP_START):].strip()
            if name:
                steps.append({"name": name, "ok": False})
        elif line.startswith(_STEP_OK) and steps:
            name = line[len(_STEP_OK):].strip()
            if steps[-1]["name"] == name:
                steps[-1]["ok"] = True
    return steps


def _failed_step(steps: list[dict]) -> str:
    for step in steps:
        if not step["ok"]:
            return str(step["name"])
    return ""


def _node_script(commands: list[tuple[str, str]]) -> str:
    """把步骤渲染为 POSIX sh 脚本：失败即停，逐步骤打标记。"""
    lines = ["set -eu"]
    for name, command in commands:
        lines.append(f"printf '%s %s\\n' '{_STEP_START}' '{name}'")
        lines.append(command)
        lines.append(f"printf '%s %s\\n' '{_STEP_OK}' '{name}'")
    return "\n".join(lines)


def _package_scripts(repo_dir: Path) -> dict:
    manifest = Path(repo_dir) / "package.json"
    if not manifest.is_file():
        return {}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def project_plan(repo_dir: Path, contract: dict | None = None) -> ProjectPlan:
    """按仓库形态选择执行计划（M4：Node 走真实构建/类型检查/测试，禁止联网安装）。

    判定顺序：
      1) 存在 Python 测试套件 → unittest 套件（UNIT_TEST）；
      2) 存在可执行 package.json/tsconfig → 运行声明脚本（有 test 步骤=UNIT_TEST，
         仅构建=STATIC_ONLY）；已声明契约不再掩盖真实构建；
      3) 显式契约（contract）→ 契约静态校验（CONTRACT_TEST）；
      4) 其余 → 自动静态验收（STATIC_ONLY）。
    """
    if has_python_tests(repo_dir):
        return ProjectPlan(kind="python", verification_mode=MODE_UNIT_TEST)
    scripts = _package_scripts(repo_dir)
    if scripts or (Path(repo_dir) / "tsconfig.json").is_file():
        commands: list[tuple[str, str]] = []
        if "build" in scripts:
            commands.append(("build", "npm run --silent build"))
        if "typecheck" in scripts:
            commands.append(("typecheck", "npm run --silent typecheck"))
        elif (Path(repo_dir) / "tsconfig.json").is_file():
            commands.append(("typecheck", "npx --no-install tsc --noEmit"))
        test_script = str(scripts.get("test") or "")
        real_test = bool(test_script) and "no test specified" not in test_script
        if real_test:
            commands.append(("test", "npm test --silent"))
        if commands:
            return ProjectPlan(
                kind="node",
                verification_mode=MODE_UNIT_TEST if real_test else MODE_STATIC_ONLY,
                image=NODE_SANDBOX_IMAGE,
                command=["/bin/sh", "-c", _node_script(commands)],
                steps=[name for name, _ in commands],
                # 构建/测试会写 dist、coverage 等产物；root 仍只读、配额另计
                workspace_writable=True,
            )
    if contract:
        return ProjectPlan(kind="contract", verification_mode=MODE_CONTRACT_TEST)
    return ProjectPlan(
        kind="static",
        verification_mode=MODE_STATIC_ONLY,
        static_reason="仓库未发现 Python 测试套件与可执行 Node 脚本",
    )


def _execute(cmd: list[str], timeout: int, max_bytes: int) -> _ExecResult:
    """有界执行：流式读取并截断，超时杀进程组，避免子进程输出撑爆内存。"""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return _ExecResult(exit_code=-127, stdout="", stderr=f"运行时不可用: {exc}")

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"value": False}

    def _drain(stream, key: str) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            room = max_bytes - len(buffers[key])
            if room > 0:
                buffers[key].extend(chunk[:room])
            if len(chunk) > room:
                truncated["value"] = True

    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    finally:
        for thread in threads:
            thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return _ExecResult(
        exit_code=exit_code,
        stdout=buffers["stdout"].decode("utf-8", errors="replace"),
        stderr=buffers["stderr"].decode("utf-8", errors="replace"),
        timed_out=timed_out,
        truncated=truncated["value"],
    )



def scan_patch_diff(diff: str, files: list[str] | None = None) -> tuple[bool, str]:
    """SAST 第一道（快速正则）：扫描 diff 新增行中的高危模式。返回 (ok, detail)。

    files 为补丁涉及的文件列表：全为前端扩展名时改用前端模式集——通用集会把 JS 生态
    的常见写法当成高危模式（regex.exec( → 命令执行、socket/requests 变量名 → 原生网络
    调用），使正常需求改动在 SAST 第一道就被误判拦截。
    """
    added = [
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    ]
    frontend = bool(files) and all(
        Path(str(name)).suffix.lower() in _FRONTEND_SUFFIXES for name in files
    )
    patterns = _FRONTEND_DANGEROUS_PATTERNS if frontend else _DANGEROUS_PATTERNS
    hits: list[str] = []
    for pattern, label in patterns:
        if any(pattern.search(line) for line in added):
            hits.append(label)
    if hits:
        return False, f"命中高危模式：{'、'.join(hits)}"
    scope = "，前端模式集" if frontend else ""
    return True, f"无高危模式，无硬编码敏感信息（新增 {len(added)} 行已扫描{scope}）"


# Bandit 第二道（AST 深度扫描，P2-04）：拦截补丁**新引入**的 HIGH/MEDIUM 问题
_BANDIT_BLOCKING_SEVERITIES = {"HIGH", "MEDIUM"}
_BANDIT_TIMEOUT_SECONDS = 30


def _bandit_scan(target: Path) -> tuple[list[dict] | None, str]:
    """对目录运行 bandit（JSON 输出），返回 (issues, error)；不可用时 issues 为 None。

    本机形态在宿主 venv 运行——bandit 仅做 AST 静态分析、不执行代码，安全等价；
    生产镜像可将 bandit 预装进沙箱镜像，把该命令下推到隔离容器内执行。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "bandit", "-r", str(target), "-f", "json", "-q"],
            capture_output=True,
            timeout=_BANDIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"bandit 不可用（{exc.__class__.__name__}）"
    try:
        payload = json.loads(_as_text(proc.stdout) or "{}")
    except json.JSONDecodeError:
        return None, "bandit 输出解析失败"
    results = payload.get("results")
    return (results if isinstance(results, list) else []), ""


def _bandit_key(issue: dict, root: Path) -> tuple[str, str]:
    """归一化问题标识（相对路径 + 规则号），使 baseline 与 workspace 可比。"""
    try:
        filename = str(
            Path(str(issue.get("filename"))).resolve().relative_to(Path(root).resolve())
        )
    except (ValueError, OSError):
        filename = Path(str(issue.get("filename", "?"))).name
    return filename, str(issue.get("test_id", "?"))


def run_bandit(workspace: Path, baseline: Path) -> tuple[bool, str]:
    """Bandit 深度扫描（返回 (ok, detail)）：仅拦截补丁新引入的 HIGH/MEDIUM 问题。

    demo-app 存在既有的 bandit 发现（0.0.0.0 绑定、测试样例口令等），
    因此对 baseline（原代码）与 workspace（补丁后）分别扫描并做多重集差集——
    否则任何补丁都会被历史问题拦下。LOW / 既有问题仅记录不拦截；
    bandit 不可用时降级放行（正则第一道仍在），链路不中断。
    """
    ws_issues, ws_error = _bandit_scan(workspace)
    if ws_issues is None:
        return True, f"Bandit 未执行（{ws_error}）"
    base_issues, base_error = _bandit_scan(baseline)
    if base_issues is None:
        return True, f"Bandit 基线不可比（{base_error}），跳过深度拦截"
    known = Counter(_bandit_key(issue, baseline) for issue in base_issues)
    seen: Counter = Counter()
    new_issues: list[dict] = []
    for issue in ws_issues:
        key = _bandit_key(issue, workspace)
        seen[key] += 1
        if seen[key] > known.get(key, 0):
            new_issues.append(issue)
    blocking = [
        issue
        for issue in new_issues
        if str(issue.get("issue_severity", "")).upper() in _BANDIT_BLOCKING_SEVERITIES
    ]
    if blocking:
        desc = "；".join(
            f"{issue.get('test_id')} {issue.get('issue_severity')} "
            f"{Path(str(issue.get('filename', '?'))).name}:{issue.get('line_number')} "
            f"{str(issue.get('issue_text', '')).strip()}"
            for issue in blocking[:5]
        )
        return False, f"Bandit 拦截：补丁新增 {len(blocking)} 个 HIGH/MEDIUM 问题（{desc}）"
    suffix = (
        f"（新增 LOW 问题 {len(new_issues)} 个，已记录）" if new_issues else "（0 新增问题）"
    )
    return True, f"Bandit 通过{suffix}"


def prepare_workspace(
    patch: Patch, repo_dir: Path, sandbox_root: Path
) -> tuple[Path | None, str]:
    """复制被监控仓库到独立工作区并应用补丁 diff（支持修改与新增文件）。"""
    workspace = sandbox_root / patch.patch_id
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(repo_dir, workspace, ignore=_IGNORE)
    error = _apply_patch_to_workspace(patch, workspace)
    if error:
        return None, error
    return workspace, ""


def _apply_patch_to_workspace(patch: Patch, workspace: Path) -> str:
    """把补丁 diff 应用到工作区（返回错误信息，空串即成功）。

    多文件补丁（如受保护目录场景/需求实现同时改多个文件）按文件头切分逐文件应用；
    ``--- /dev/null`` 的新增文件以空原文应用并自动创建父目录——否则「需求实现新增
    组件/工具文件」会被误判为「目标文件不存在」而错误拦截。
    """
    sections = split_unified_diff(patch.diff)
    if len(sections) > 1:
        plan = list(sections.items())
    else:
        target_rel = _normalize_rel(patch.files[0]) if patch.files else ""
        plan = [(target_rel, patch.diff)]
    for rel, file_diff in plan:
        rel = _normalize_rel(rel)
        target = workspace / rel
        new_file = is_new_file_diff(file_diff)
        if target.is_file():
            if new_file:
                return f"新增文件 diff 指向工作区已存在的文件: {rel}"
            original = target.read_text(encoding="utf-8")
        elif new_file:
            target.parent.mkdir(parents=True, exist_ok=True)
            original = ""
        else:
            return f"工作区中目标文件不存在: {rel}"
        new_text = apply_unified_diff(original, file_diff)
        if new_text is None:
            return f"补丁 diff 无法应用（上下文不匹配）: {rel}"
        target.write_text(new_text, encoding="utf-8")
    return ""


def parse_unittest_output(text: str) -> dict:
    """解析 unittest 输出：用例数 / 失败用例名 / 首个错误摘要 / OK 标记。"""
    match = re.search(r"Ran (\d+) tests? in", text)
    tests_run = int(match.group(1)) if match else 0
    failed_tests = re.findall(r"^(?:FAIL|ERROR): (\S+)", text, re.MULTILINE)
    errors = re.findall(r"^([A-Za-z_.]*(?:Error|Exception)): (.+)$", text, re.MULTILINE)
    error_summary = f"{errors[-1][0]}: {errors[-1][1]}" if errors else ""
    return {
        "tests_run": tests_run,
        "failed_tests": failed_tests,
        "error_summary": error_summary,
    }


def _as_text(raw: bytes | str | None) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw or ""


class DockerSandboxRunner:
    """本机形态：docker run 一次性容器（隔离语义与 K8s Job+gVisor 对齐）。

    M4 加固：非 root（nobody）、drop 全部 capabilities、禁止提权、
    seccomp（运行时默认档；AIOPS_SANDBOX_SECCOMP 可指定自定义档）、
    输出与工作区体积上限、超时后显式回收并核对容器已消失。
    """

    name = "docker-container"

    def _container_name(self, job: SandboxJob) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", job.patch_id)[:40] or "patch"
        return f"aiops-sandbox-{safe}"

    def _build_cmd(self, job: SandboxJob, name: str | None = None) -> list[str]:
        mount_mode = "rw" if job.workspace_writable else "ro"
        cmd = [
            "docker", "run", "--rm",
            "--name", name or self._container_name(job),
            "--label", f"aiops.patch-id={job.patch_id}",
            "--network", "none" if job.network_disabled else "bridge",
            "--user", "65534:65534",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", job.memory_limit,
            "--cpus", job.cpus,
            "--pids-limit", str(job.pids_limit),
            "--read-only",
            "--tmpfs", "/tmp:size=32m",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp",
            "-e", "npm_config_offline=true",
            "-e", "npm_config_audit=false",
            "-e", "npm_config_fund=false",
        ]
        if _SECCOMP_PROFILE:
            # 显式档：与 K8s seccompProfile RuntimeDefault 等价的最小系统调用集
            cmd += ["--security-opt", f"seccomp={_SECCOMP_PROFILE}"]
        cmd += [
            "-v", f"{job.workspace}:/work:{mount_mode}",
            "-w", "/work",
            job.image,
        ]
        return cmd + job.command

    def _reclaim(self, name: str) -> str:
        """超时/异常后回收容器并核对不存在（返回错误文案，空串=已回收）。"""
        _execute(["docker", "rm", "-f", name], 30, 64 * 1024)
        listed = _execute(
            ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"], 15, 8 * 1024
        )
        if listed.exit_code != 0:
            return f"容器回收状态未知（docker ps 退出码 {listed.exit_code}）"
        if listed.stdout.strip():
            return f"容器回收失败：{name} 仍存在"
        return ""

    def run(self, job: SandboxJob) -> SandboxOutcome:
        started = time.monotonic()
        name = self._container_name(job)
        # 清掉同名残留（上次进程崩溃/被杀的孤儿），保证 --name 幂等
        _execute(["docker", "rm", "-f", name], 15, 8 * 1024)
        cmd = self._build_cmd(job, name)
        result = _execute(cmd, job.timeout_seconds, max_stream_bytes())
        duration = round(time.monotonic() - started, 3)
        cleanup_error = ""
        if result.timed_out:
            cleanup_error = self._reclaim(name)
        stdout, stderr = result.stdout, result.stderr
        parsed = parse_unittest_output(stderr + "\n" + stdout)
        steps = parse_step_output(stdout + "\n" + stderr)
        passed = (not result.timed_out) and result.exit_code == 0 and parsed["tests_run"] > 0
        if steps and not parsed["tests_run"]:
            # Node 步骤：以步骤标记 + 退出码判定，测试步骤缺失时由 verification_mode 区分
            passed = (not result.timed_out) and result.exit_code == 0 and not _failed_step(steps)
        error_summary = parsed["error_summary"]
        if result.timed_out:
            error_summary = "沙箱执行超时（已回收隔离实例）"
            if cleanup_error:
                error_summary = f"{error_summary}；{cleanup_error}"
        elif result.exit_code == 0 and parsed["tests_run"] == 0 and not steps:
            error_summary = "退出码为 0 但未发现任何测试用例"
        elif steps and result.exit_code != 0:
            error_summary = f"步骤失败：{_failed_step(steps) or '未知'}（退出码 {result.exit_code}）"
        return SandboxOutcome(
            exit_code=result.exit_code,
            passed=passed,
            tests_run=parsed["tests_run"],
            duration_seconds=duration,
            runner=self.name,
            image=job.image,
            timed_out=result.timed_out,
            failed_tests=parsed["failed_tests"],
            error_summary=error_summary,
            stdout=stdout,
            stderr=stderr,
            verification_mode=job.verification_mode,
            steps=steps,
            output_truncated=result.truncated,
            image_pinned=image_is_pinned(job.image),
            cleanup_verified=not cleanup_error,
        )



class K8sJobSandboxRunner:
    """生产形态：batch/v1 Job + gVisor 运行时，经官方 Kubernetes Python 客户端执行。

    执行链：工作区打包为 Secret（体积上限，超限显式失败）→ 创建 Job（幂等名称 +
    spec 哈希标签）→ 轮询状态（activeDeadlineSeconds）→ 读取 Pod 日志 →
    解析结论 → 删除 Job 与 Secret 并核对 Pod 消失。

    全部资源位于 aiops-sandbox namespace，配 default-deny NetworkPolicy（无出网权限）；
    Worker 仅授予该 namespace 的 Job/Pod 最小权限（deploy/staging/rbac.yaml）。
    单测使用 fake client，不访问真实集群。
    """

    name = "k8s-job-gvisor"
    K8S_NAMESPACE = os.environ.get("AIOPS_SANDBOX_NAMESPACE", "aiops-sandbox")
    LOADER_IMAGE = os.environ.get(
        "AIOPS_SANDBOX_LOADER_IMAGE", "busybox:1.36@sha256:9ae97d36d26566ff84e8893c64a6dc4fe8ca6d1144bf5b87b2b85a32def253c7"
    )
    SECRET_MAX_BYTES = 512 * 1024

    def __init__(
        self,
        batch_api=None,
        core_api=None,
        namespace: str | None = None,
        poll_interval: float = 1.0,
        cleanup_timeout: float = 30.0,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self._batch = batch_api
        self._core = core_api
        self.namespace = namespace or self.K8S_NAMESPACE
        self.poll_interval = poll_interval
        self.cleanup_timeout = cleanup_timeout
        self._sleep = sleep
        self._clock = clock

    # ---------------- 客户端 ----------------
    def _apis(self):
        if self._batch is not None and self._core is not None:
            return self._batch, self._core
        from kubernetes import client, config  # 延迟导入：本机无集群时不影响其他路径

        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001 - 集群外（本地联调）回退 kubeconfig
            config.load_kube_config()
        self._batch = self._batch or client.BatchV1Api()
        self._core = self._core or client.CoreV1Api()
        return self._batch, self._core

    # ---------------- 命名与清单 ----------------
    @staticmethod
    def _spec_hash(payload: bytes) -> str:
        import hashlib

        return hashlib.sha256(payload).hexdigest()[:10]

    def job_name(self, patch_id: str, spec_hash: str) -> str:
        safe = re.sub(r"[^a-z0-9-]", "-", (patch_id or "patch").lower())[:40].strip("-") or "patch"
        return f"aiops-sandbox-{safe}-{spec_hash}"

    def render_manifest(
        self, job: SandboxJob, *, name: str | None = None, secret_name: str | None = None
    ) -> dict:
        spec_hash = "0" * 10
        name = name or self.job_name(job.patch_id, spec_hash)
        secret_name = secret_name or f"{name}-workspace"
        labels = {
            "app": "aiops-sandbox",
            "aiops.io/component": "sandbox",
            "aiops.io/patch-id": re.sub(r"[^A-Za-z0-9_.-]", "-", job.patch_id)[:63],
            "aiops.io/spec-hash": spec_hash,
        }
        env = [
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "HOME", "value": "/tmp"},
            {"name": "npm_config_offline", "value": "true"},
            {"name": "npm_config_audit", "value": "false"},
        ]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": job.timeout_seconds,
                "ttlSecondsAfterFinished": 600,
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {"aiops.io/verification-mode": job.verification_mode},
                    },
                    "spec": {
                        "runtimeClassName": "gvisor",
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65534,
                            "runAsGroup": 65534,
                            "fsGroup": 65534,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "initContainers": [
                            {
                                "name": "workspace-loader",
                                "image": self.LOADER_IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/bin/sh", "-c", "tar -xzf /payload/workspace.tgz -C /work"],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "limits": {"memory": "128Mi", "cpu": "200m"},
                                    "requests": {"memory": "32Mi", "cpu": "50m"},
                                },
                                "volumeMounts": [
                                    {"name": "workspace", "mountPath": "/work"},
                                    {"name": "payload", "mountPath": "/payload", "readOnly": True},
                                ],
                            }
                        ],
                        "containers": [
                            {
                                "name": "sandbox",
                                "image": job.image,
                                "imagePullPolicy": job.image_pull_policy,
                                "command": job.command,
                                "workingDir": "/work",
                                "env": env,
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "limits": {"memory": "256Mi", "cpu": "1", "ephemeral-storage": "1Gi"},
                                    "requests": {"memory": "64Mi", "cpu": "100m"},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "workspace",
                                        "mountPath": "/work",
                                        "readOnly": not job.workspace_writable,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}},
                            {"name": "payload", "secret": {"secretName": secret_name}},
                        ],
                    },
                },
            },
        }

    # ---------------- 工作区打包 ----------------
    def pack_workspace(self, workspace: Path) -> tuple[bytes | None, str]:
        """把工作区打包为 gzip tar（超限返回错误），供 Secret 载荷使用。"""
        import io
        import tarfile

        buffer = io.BytesIO()
        try:
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                for path in sorted(Path(workspace).rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.add(path, arcname=str(path.relative_to(workspace)))
        except OSError as exc:
            return None, f"工作区打包失败：{exc}"
        payload = buffer.getvalue()
        if len(payload) > self.SECRET_MAX_BYTES:
            return None, (
                f"工作区归档 {len(payload)}B 超过 Secret 上限 {self.SECRET_MAX_BYTES}B；"
                "需接入制品库（对象存储）后再启用 K8s 执行器"
            )
        return payload, ""

    # ---------------- Job 生命周期 ----------------
    def _create_secret(self, core, secret_name: str, payload: bytes, patch_id: str) -> None:
        import base64

        from kubernetes import client as k8s_client

        body = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(
                name=secret_name,
                namespace=self.namespace,
                labels={"app": "aiops-sandbox", "aiops.io/patch-id": patch_id[:63]},
            ),
            type="Opaque",
            immutable=True,
            data={"workspace.tgz": base64.b64encode(payload).decode("ascii")},
        )
        try:
            core.create_namespaced_secret(self.namespace, body)
        except Exception as exc:  # noqa: BLE001 - 同名残留（崩溃重试）删除后重建
            if getattr(exc, "status", None) != 409:
                raise
            core.delete_namespaced_secret(secret_name, self.namespace)
            core.create_namespaced_secret(self.namespace, body)

    @staticmethod
    def _v1job(manifest: dict):
        """把 dict 清单转成客户端类型（create_namespaced_job 需要 V1Job）。"""
        from kubernetes import client as k8s_client

        return k8s_client.ApiClient()._ApiClient__deserialize(manifest, "V1Job")  # noqa: SLF001

    def _ensure_job(self, batch, manifest: dict) -> str:
        """创建 Job；409 冲突时同 spec 复用、不同 spec 删除重建（幂等）。"""
        name = manifest["metadata"]["name"]
        spec_hash = manifest["metadata"]["labels"]["aiops.io/spec-hash"]
        body = self._v1job(manifest)
        try:
            batch.create_namespaced_job(self.namespace, body)
            return "created"
        except Exception as exc:  # noqa: BLE001 - 通过状态码区分冲突与其他错误
            status = getattr(exc, "status", None)
            if status != 409:
                raise
        try:
            existing = batch.read_namespaced_job(name, self.namespace)
        except Exception:  # noqa: BLE001 - 冲突后读不到：交给后续创建失败暴露
            return "conflict-unresolved"
        labels = (getattr(existing.metadata, "labels", None) or {})
        if labels.get("aiops.io/spec-hash") == spec_hash:
            return "reused"
        batch.delete_namespaced_job(
            name,
            self.namespace,
            propagation_policy="Background",
            grace_period_seconds=0,
        )
        batch.create_namespaced_job(self.namespace, body)
        return "replaced"

    def _job_state(self, batch, name: str) -> str:
        job = batch.read_namespaced_job_status(name, self.namespace)
        status = job.status
        failed = getattr(status, "failed", None) or 0
        succeeded = getattr(status, "succeeded", None) or 0
        for condition in getattr(status, "conditions", None) or []:
            if getattr(condition, "type", "") == "Failed":
                return "deadline" if getattr(condition, "reason", "") == "DeadlineExceeded" else "failed"
        if succeeded:
            return "succeeded"
        if failed:
            return "failed"
        return "running"

    def _pods(self, core, name: str) -> list:
        pods = core.list_namespaced_pod(self.namespace, label_selector=f"job-name={name}")
        return list(getattr(pods, "items", None) or [])

    def _pod_failure_reason(self, pod) -> str:
        statuses = getattr(pod.status, "container_statuses", None) or []
        for status in statuses:
            state = getattr(status, "state", None)
            waiting = getattr(state, "waiting", None) if state else None
            reason = getattr(waiting, "reason", "") if waiting else ""
            if reason in {"ErrImagePull", "ImagePullBackOff", "InvalidImageName"}:
                return f"镜像拉取失败：{reason}"
            terminated = getattr(state, "terminated", None) if state else None
            if terminated and getattr(terminated, "reason", "") == "OOMKilled":
                return "沙箱内存超限被终止（OOMKilled）"
        return ""

    def _read_logs(self, core, pods: list) -> tuple[str, str]:
        """读取 sandbox 容器日志（按上限截断），返回 (日志, 错误)。"""
        for pod in pods:
            name = getattr(pod.metadata, "name", "")
            if not name:
                continue
            try:
                raw = core.read_namespaced_pod_log(
                    name, self.namespace, container="sandbox", tail_lines=4000
                )
            except Exception as exc:  # noqa: BLE001 - 日志不可读必须显式留痕
                return "", f"读取 Pod 日志失败：{exc}"
            text = raw if isinstance(raw, str) else str(raw)
            limit = max_stream_bytes()
            truncated = len(text.encode("utf-8", errors="replace")) > limit
            return text[:limit], ("日志超限已截断" if truncated else "")
        return "", "未找到 Job 的 Pod"

    def _wait(self, batch, core, job: SandboxJob, name: str) -> tuple[str, str, list]:
        deadline = self._clock() + job.timeout_seconds + 30
        missing = 0
        while True:
            try:
                state = self._job_state(batch, name)
            except Exception as exc:  # noqa: BLE001 - Job 刚创建时可能短暂 404
                if getattr(exc, "status", None) == 404 and missing < 3:
                    missing += 1
                    self._sleep(self.poll_interval)
                    continue
                raise
            pods = self._pods(core, name)
            if state in {"succeeded", "failed", "deadline"}:
                return state, "", pods
            if self._clock() >= deadline:
                return "timeout", "", pods
            reason = ""
            for pod in pods:
                reason = self._pod_failure_reason(pod)
                if reason:
                    return "image-failed", reason, pods
            self._sleep(self.poll_interval)

    def _cleanup(self, batch, core, name: str, secret_name: str) -> str:
        """删除 Job/Secret 并核对 Pod 消失（返回错误文案，空串=已清理）。"""
        try:
            batch.delete_namespaced_job(
                name, self.namespace, propagation_policy="Background", grace_period_seconds=0
            )
        except Exception as exc:  # noqa: BLE001 - 404 视为已删除，其余记录
            if getattr(exc, "status", None) != 404:
                return f"Job 删除失败：{exc}"
        try:
            core.delete_namespaced_secret(secret_name, self.namespace)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) != 404:
                return f"Secret 删除失败：{exc}"
        deadline = self._clock() + self.cleanup_timeout
        while self._clock() < deadline:
            if not self._pods(core, name):
                return ""
            self._sleep(self.poll_interval)
        return "清理超时：Pod 未在时限内消失"

    def run(self, job: SandboxJob) -> SandboxOutcome:
        started = self._clock()
        image_error = resolve_image_error(job.image)
        if image_error:
            return SandboxOutcome(
                exit_code=-1, passed=False, tests_run=0,
                duration_seconds=0.0, runner=self.name, image=job.image,
                error_summary=image_error, verification_mode=job.verification_mode,
                image_pinned=False,
            )
        payload, pack_error = self.pack_workspace(job.workspace)
        if payload is None:
            return SandboxOutcome(
                exit_code=-1, passed=False, tests_run=0,
                duration_seconds=0.0, runner=self.name, image=job.image,
                error_summary=pack_error, verification_mode=job.verification_mode,
                image_pinned=image_is_pinned(job.image),
            )
        try:
            batch, core = self._apis()
        except Exception as exc:  # noqa: BLE001 - 集群不可达必须显式失败
            return SandboxOutcome(
                exit_code=-1, passed=False, tests_run=0,
                duration_seconds=round(self._clock() - started, 3), runner=self.name,
                image=job.image, error_summary=f"K8s 客户端不可用：{exc}",
                verification_mode=job.verification_mode,
                image_pinned=image_is_pinned(job.image),
            )

        name = self.job_name(job.patch_id, self._spec_hash(payload))
        secret_name = f"{name}-workspace"
        manifest = self.render_manifest(job, name=name, secret_name=secret_name)
        # 清单里的 spec 哈希用于冲突判定（render 时占位）
        manifest["metadata"]["labels"]["aiops.io/spec-hash"] = self._spec_hash(payload)
        manifest["spec"]["template"]["metadata"]["labels"]["aiops.io/spec-hash"] = self._spec_hash(payload)

        cleanup_error = ""
        try:
            self._create_secret(core, secret_name, payload, job.patch_id)
            self._ensure_job(batch, manifest)
            state, reason, pods = self._wait(batch, core, job, name)
            logs, log_error = self._read_logs(core, pods)
        except Exception as exc:  # noqa: BLE001 - 提交/轮询异常一律失败并清理
            state, reason, logs, log_error = "failed", f"K8s 执行异常：{exc}", "", ""
            pods = []
        finally:
            try:
                cleanup_error = self._cleanup(batch, core, name, secret_name)
            except Exception as exc:  # noqa: BLE001
                cleanup_error = f"清理异常：{exc}"

        duration = round(self._clock() - started, 3)
        parsed = parse_unittest_output(logs)
        steps = parse_step_output(logs)
        timed_out = state == "timeout" or state == "deadline"
        passed = state == "succeeded" and not timed_out
        if passed and parsed["tests_run"] == 0 and steps:
            passed = not _failed_step(steps)
        if passed and parsed["tests_run"] == 0 and not steps:
            passed = job.verification_mode == MODE_STATIC_ONLY
        error_summary = parsed["error_summary"] or reason or log_error
        if state == "succeeded" and parsed["tests_run"] == 0 and not steps:
            error_summary = "退出码为 0 但未发现任何测试用例"
        if cleanup_error:
            error_summary = f"{error_summary}；{cleanup_error}" if error_summary else cleanup_error
        return SandboxOutcome(
            exit_code=0 if passed else -1,
            passed=passed,
            tests_run=parsed["tests_run"],
            duration_seconds=duration,
            runner=self.name,
            image=job.image,
            timed_out=timed_out,
            failed_tests=parsed["failed_tests"],
            error_summary=error_summary,
            stdout=logs,
            stderr="",
            verification_mode=job.verification_mode,
            steps=steps,
            image_pinned=image_is_pinned(job.image),
            cleanup_verified=not cleanup_error,
        )



def has_python_tests(repo_dir: Path) -> bool:
    """仓库是否存在可执行的 Python 测试套件（对齐 `unittest discover -s tests` 收集范围）。

    discover 仅收集 tests/ 目录下匹配 test*.py 的文件：目录缺失或无匹配文件时，
    执行 Python 套件必然收集 0 用例（「沙箱内未发现任何测试用例」）——非 Python
    测试仓库（如 TS/Node 应用）应走静态验收，而非误入套件路径判失败。
    """
    tests_dir = Path(repo_dir) / "tests"
    return tests_dir.is_dir() and any(tests_dir.glob("test*.py"))


def _contract_report(
    patch: Patch, attempt: int, sast_detail: str, *, auto: bool = False
) -> TestReport:
    """静态验收报告：补丁应用成功即通过（不执行 Python 测试套件与 Bandit）。

    两类触发（判定语义一致，文案区分触发原因）：
    - 契约模式（auto=False）：注册表条目显式启用（contract 非空，前端/非 Python 仓库）；
    - 自动静态验收（auto=True）：未显式启用契约，但仓库未发现 Python 测试套件
      （has_python_tests 为 False）——该路径执行 Python 套件必然 0 用例，属误入路径。
    2026-10-10 起取消探针关键词判据（原「补丁后全树仍可检出关键词」不再作为通过
    条件）：探针关键词仅保留给直连发布的在线页面探针（release.run_canary_direct）。
    生成降级（degraded）的兜底补丁为生成环节失败产物，直接拦截。
    """
    if patch.degraded:
        return TestReport(
            patch_id=patch.patch_id,
            passed=False,
            unit_tests="未执行（补丁生成降级，非真实修复）",
            regression_tests="未执行（静态契约模式）",
            sast=sast_detail,
            details=(
                f"契约模式拦截降级兜底补丁（生成环节失败产物）："
                f"补丁文件 {patch.files}（attempt={attempt}）"
            ),
            verification_mode=MODE_STATIC_ONLY if auto else MODE_CONTRACT_TEST,
        )
    if auto:
        report = TestReport(
            patch_id=patch.patch_id,
            passed=True,
            unit_tests="静态验证通过（未发现 Python 测试套件，自动静态验收）",
            regression_tests="未执行（无 Python 测试套件）",
            sast=sast_detail,
            details=(
                f"自动静态验收：仓库 tests/ 未发现 test*.py（非 Python 测试仓库），"
                f"补丁已应用至工作区（{len(patch.files)} 个文件，attempt={attempt}）"
            ),
            verification_mode=MODE_STATIC_ONLY,
        )
        metrics.observe_sandbox_static(report.passed, report.verification_mode)
        return report
    report = TestReport(
        patch_id=patch.patch_id,
        passed=True,
        unit_tests="契约校验通过（静态契约模式：补丁应用成功）",
        regression_tests="未执行（静态契约模式）",
        sast=sast_detail,
        details=f"契约校验：补丁已应用至工作区（{len(patch.files)} 个文件，attempt={attempt}）",
        verification_mode=MODE_CONTRACT_TEST,
    )
    metrics.observe_sandbox_static(report.passed, report.verification_mode)
    return report


def default_runner() -> DockerSandboxRunner | K8sJobSandboxRunner:
    """按 AIOPS_SANDBOX_RUNTIME 选择执行器（默认本机 Docker；生产用 k8s）。"""
    runtime = (os.environ.get("AIOPS_SANDBOX_RUNTIME") or "docker").strip().lower()
    if runtime == "k8s":
        return K8sJobSandboxRunner()
    return DockerSandboxRunner()


def run_patch_tests(
    patch: Patch,
    attempt: int,
    *,
    repo_dir: Path | None = None,
    sandbox_root: Path | None = None,
    runner: DockerSandboxRunner | K8sJobSandboxRunner | None = None,
    contract: dict | None = None,
) -> TestReport:
    """编排一次沙箱验证：SAST → 工作区准备 → 隔离执行 → TestReport。

    contract：被监控前端应用的静态契约模式开关（注册表条目 contract 非空即启用）。
        非空时走契约模式——不执行测试套件与 Bandit；补丁应用成功即为通过。
    执行计划（M4）：Python 套件 → Node 构建/类型检查/测试（镜像需预装依赖、
        禁止联网安装）→ 无套件时静态验收。仅 UNIT_TEST 计入真实沙箱通过率。
    """
    repo_dir = Path(repo_dir) if repo_dir else code_rag.DEFAULT_REPO_DIR
    sandbox_root = Path(sandbox_root) if sandbox_root else default_sandbox_root()

    sast_ok, sast_detail = scan_patch_diff(patch.diff, files=patch.files)
    if not sast_ok:
        return TestReport(
            patch_id=patch.patch_id,
            passed=False,
            unit_tests="未执行（SAST 拦截）",
            regression_tests="未执行",
            sast=sast_detail,
            details=f"静态扫描失败：{sast_detail}（attempt={attempt}）",
        )

    workspace, prep_error = prepare_workspace(patch, repo_dir, sandbox_root)
    if prep_error:
        return TestReport(
            patch_id=patch.patch_id,
            passed=False,
            unit_tests="未执行（工作区准备失败）",
            regression_tests="未执行",
            sast=sast_detail,
            details=f"{prep_error}（attempt={attempt}）",
        )

    plan = project_plan(repo_dir, contract)
    # 契约模式（前端仓库）与无测试套件的静态验收：不进入隔离运行时
    if plan.kind in {"contract", "static"}:
        return _contract_report(patch, attempt, sast_detail, auto=plan.kind == "static")

    image_error = resolve_image_error(plan.image)
    if image_error:
        return TestReport(
            patch_id=patch.patch_id,
            passed=False,
            unit_tests="未执行（镜像未固定摘要）",
            regression_tests="未执行",
            sast=sast_detail,
            details=f"{image_error}（attempt={attempt}）",
            verification_mode=plan.verification_mode,
        )
    quota_error = workspace_quota_error(workspace)
    if quota_error:
        return TestReport(
            patch_id=patch.patch_id,
            passed=False,
            unit_tests="未执行（工作区配额超限）",
            regression_tests="未执行",
            sast=sast_detail,
            details=f"{quota_error}（attempt={attempt}）",
            verification_mode=plan.verification_mode,
        )

    sast_combined = sast_detail
    if plan.kind == "python":
        # 第二道 SAST：Bandit AST 深度扫描（仅拦截补丁新引入的 HIGH/MEDIUM 问题）
        bandit_ok, bandit_detail = run_bandit(workspace, repo_dir)
        sast_combined = f"{sast_detail}；{bandit_detail}"
        if not bandit_ok:
            return TestReport(
                patch_id=patch.patch_id,
                passed=False,
                unit_tests="未执行（Bandit 拦截）",
                regression_tests="未执行",
                sast=sast_combined,
                details=f"深度静态扫描失败：{bandit_detail}（attempt={attempt}）",
                verification_mode=plan.verification_mode,
            )

    job = SandboxJob(
        patch_id=patch.patch_id,
        workspace=workspace,
        image=plan.image,
        command=list(plan.command),
        verification_mode=plan.verification_mode,
        workspace_writable=plan.workspace_writable,
        steps=list(plan.steps),
    )
    before = dir_size_bytes(workspace)
    outcome = (runner or default_runner()).run(job)
    if plan.workspace_writable and not outcome.timed_out:
        grown = dir_size_bytes(workspace) - before
        if grown > max_workspace_bytes():
            outcome.passed = False
            outcome.error_summary = (
                f"沙箱产物超限：新增 {grown}B 超过配额 {max_workspace_bytes()}B"
            )
    return _report_from_outcome(patch, attempt, outcome, sast_combined)


def _report_from_outcome(
    patch: Patch, attempt: int, outcome: SandboxOutcome, sast_detail: str
) -> TestReport:
    tests_run = outcome.tests_run
    failed = len(outcome.failed_tests)
    # 空串=历史/未知来源：展示补全为 UNKNOWN，指标与发布闸门一律不按真实测试处理
    mode_label = outcome.verification_mode or "UNKNOWN"
    is_real = mode_label == MODE_UNIT_TEST
    if outcome.timed_out:
        unit = f"执行超时（{outcome.duration_seconds:.1f}s 未完成）"
        details = f"沙箱执行超时：未在时限内完成（attempt={attempt}）"
    elif not is_real and not tests_run:
        step_names = "/".join(step["name"] for step in outcome.steps) or "构建"
        unit = (
            f"静态验收通过（{step_names} 完成，未执行测试）"
            if outcome.passed
            else f"静态验收失败（{step_names}）"
        )
        details = (
            f"隔离执行[{outcome.runner}] {step_names} {'通过' if outcome.passed else '失败'}"
            f"（镜像 {outcome.image}，attempt={attempt}）"
        )
        if not outcome.passed and outcome.error_summary:
            details = f"{details}；{outcome.error_summary}"
    elif tests_run == 0 and outcome.steps:
        names = "/".join(step["name"] for step in outcome.steps)
        unit = f"{names} {'通过' if outcome.passed else '失败'}（Node 脚本）"
        details = (
            f"隔离执行[{outcome.runner}] Node 步骤 {names} "
            f"{'通过' if outcome.passed else '失败'}（镜像 {outcome.image}，attempt={attempt}）"
        )
        if not outcome.passed and outcome.error_summary:
            details = f"{details}；{outcome.error_summary}"
    elif tests_run == 0:
        unit = "0 tests（未发现用例）"
        details = outcome.error_summary or "沙箱内未发现任何测试用例（attempt=%d）" % attempt
    elif outcome.passed:
        unit = f"{tests_run}/{tests_run} passed"
        details = (
            f"隔离执行[{outcome.runner}] Ran {tests_run} tests in {outcome.duration_seconds:.2f}s"
            f"（镜像 {outcome.image}，attempt={attempt}）"
        )
    else:
        passed_count = max(tests_run - failed, 0)
        unit = f"{passed_count}/{tests_run} passed（{failed} 个用例未通过）"
        names = "、".join(outcome.failed_tests) or "未知用例"
        details = f"失败用例：{names}；{outcome.error_summary or '详见沙箱输出'}（attempt={attempt}）"
    regression = (
        f"全量 {tests_run} 用例：{'通过' if outcome.passed else '存在失败'}"
        if tests_run
        else "未执行"
    )
    if is_real:
        metrics.observe_sandbox_run(outcome.passed, outcome.duration_seconds)
    else:
        metrics.observe_sandbox_static(outcome.passed, mode_label)
    if outcome.output_truncated:
        details = f"{details}；输出超限已截断"
    return TestReport(
        patch_id=patch.patch_id,
        passed=outcome.passed,
        unit_tests=unit,
        regression_tests=regression,
        sast=sast_detail,
        details=details,
        verification_mode=mode_label,
    )
