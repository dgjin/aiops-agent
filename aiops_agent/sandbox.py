"""沙箱验证执行器（WP6 交付物）：隔离运行时内真实执行补丁后的测试。

流程（对齐实施计划 WP6）：
    run_patch_tests(patch, attempt)
      → scan_patch_diff：SAST 演示实现——对新增行做高危模式静态扫描；
      → prepare_workspace：把 demo-app 复制到独立工作区 data/sandbox/<patch_id>/ 并应用补丁 diff；
      → SandboxRunner.run：隔离运行时内执行 unittest discover（工作区只读挂载）；
      → 解析 unittest 输出 + 退出码 → TestReport。

隔离语义（生产 vs 本机对照）：
    - 生产形态：K8sJobSandboxRunner 渲染 batch/v1 Job manifest——
      runtimeClassName: gvisor（用户态内核，系统调用隔离）、
      readOnlyRootFilesystem + ROOT 全部 capabilities drop、
      runAsNonRoot/nobody、seccomp RuntimeDefault、
      Resources limits（256Mi/1CPU）、activeDeadlineSeconds（超时即杀）、
      backoffLimit=0（一次即成，失败不重启）、ttlSecondsAfterFinished（即用即毁）；
      配合 namespace 级 default-deny NetworkPolicy（无出网权限）。
    - 本机形态：DockerSandboxRunner 用 docker run 对齐同一语义——
      --network none、--read-only + --tmpfs、--memory/--cpus/--pids-limit、
      --rm 即用即毁、-v <workspace>:/work:ro 制品只读。

环境变量：
    AIOPS_SANDBOX_IMAGE   沙箱镜像（默认 python:3.12-slim）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import code_rag, metrics
from .fix_agent import apply_unified_diff, split_unified_diff
from .models import Patch, TestReport

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SANDBOX_ROOT = BASE_DIR / "data" / "sandbox"
SANDBOX_IMAGE = os.environ.get("AIOPS_SANDBOX_IMAGE", "python:3.12-slim")

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

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", ".git", "data")


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


def scan_patch_diff(diff: str) -> tuple[bool, str]:
    """SAST 第一道（快速正则）：扫描 diff 新增行中的高危模式。返回 (ok, detail)。"""
    added = [
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    ]
    hits: list[str] = []
    for pattern, label in _DANGEROUS_PATTERNS:
        if any(pattern.search(line) for line in added):
            hits.append(label)
    if hits:
        return False, f"命中高危模式：{'、'.join(hits)}"
    return True, f"无高危模式，无硬编码敏感信息（新增 {len(added)} 行已扫描）"


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
    """复制 demo-app 到独立工作区并应用补丁 diff。返回 (workspace, error)。"""
    workspace = sandbox_root / patch.patch_id
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(repo_dir, workspace, ignore=_IGNORE)

    # 多文件补丁（如受保护目录场景需同时改动 auth/** 与应用文件）：逐文件分别应用
    sections = split_unified_diff(patch.diff)
    if len(sections) > 1:
        for rel, file_diff in sections.items():
            target = workspace / rel
            if not target.is_file():
                return None, f"工作区中目标文件不存在: {rel}"
            original = target.read_text(encoding="utf-8")
            new_text = apply_unified_diff(original, file_diff)
            if new_text is None:
                return None, f"补丁 diff 无法应用（上下文不匹配）: {rel}"
            target.write_text(new_text, encoding="utf-8")
        return workspace, ""

    target_rel = patch.files[0] if patch.files else ""
    target = workspace / target_rel
    if not target.is_file():
        return None, f"工作区中目标文件不存在: {target_rel}"
    original = target.read_text(encoding="utf-8")
    new_text = apply_unified_diff(original, patch.diff)
    if new_text is None:
        return None, f"补丁 diff 无法应用（上下文不匹配）: {target_rel}"
    target.write_text(new_text, encoding="utf-8")
    return workspace, ""


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
    """本机形态：docker run 一次性容器（隔离语义与 K8s Job+gVisor 对齐）。"""

    name = "docker-container"

    def _build_cmd(self, job: SandboxJob) -> list[str]:
        cmd = [
            "docker", "run", "--rm",
            "--network", "none" if job.network_disabled else "bridge",
            "--memory", job.memory_limit,
            "--cpus", job.cpus,
            "--pids-limit", str(job.pids_limit),
            "--read-only",
            "--tmpfs", "/tmp:size=32m",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{job.workspace}:/work:ro",
            "-w", "/work",
            job.image,
        ]
        return cmd + job.command

    def run(self, job: SandboxJob) -> SandboxOutcome:
        cmd = self._build_cmd(job)
        started = time.monotonic()
        timed_out = False
        raw_out: bytes | str = b""
        raw_err: bytes | str = b""
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=job.timeout_seconds)
            exit_code = proc.returncode
            raw_out, raw_err = proc.stdout or b"", proc.stderr or b""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            raw_out, raw_err = exc.stdout or b"", exc.stderr or b""
        except OSError as exc:
            return SandboxOutcome(
                exit_code=-127,
                passed=False,
                tests_run=0,
                duration_seconds=round(time.monotonic() - started, 3),
                runner=self.name,
                image=job.image,
                error_summary=f"沙箱运行时不可用: {exc}",
            )
        duration = round(time.monotonic() - started, 3)
        stdout, stderr = _as_text(raw_out), _as_text(raw_err)
        parsed = parse_unittest_output(stderr + "\n" + stdout)
        passed = (not timed_out) and exit_code == 0 and parsed["tests_run"] > 0
        error_summary = parsed["error_summary"]
        if not timed_out and exit_code == 0 and parsed["tests_run"] == 0:
            error_summary = "退出码为 0 但未发现任何测试用例"
        return SandboxOutcome(
            exit_code=exit_code,
            passed=passed,
            tests_run=parsed["tests_run"],
            duration_seconds=duration,
            runner=self.name,
            image=job.image,
            timed_out=timed_out,
            failed_tests=parsed["failed_tests"],
            error_summary=error_summary,
            stdout=stdout,
            stderr=stderr,
        )


class K8sJobSandboxRunner:
    """生产形态：batch/v1 Job + gVisor 运行时（本机无集群时仅渲染 manifest 留痕）。

    生产接线：render_manifest() → kubectl apply → 轮询 Job 状态 → 读取 pod 日志；
    全部资源位于 aiops-sandbox namespace，配 default-deny NetworkPolicy（无出网权限）。
    """

    name = "k8s-job-gvisor"

    def render_manifest(self, job: SandboxJob) -> dict:
        labels = {"app": "aiops-sandbox", "patch-id": job.patch_id}
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": f"aiops-sandbox-{job.patch_id}", "namespace": "aiops-sandbox", "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": job.timeout_seconds,
                "ttlSecondsAfterFinished": 600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "runtimeClassName": "gvisor",
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65534,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "sandbox",
                                "image": job.image,
                                "command": job.command,
                                "workingDir": "/work",
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
                                    {"name": "workspace", "mountPath": "/work", "readOnly": True}
                                ],
                            }
                        ],
                        # 生产：initContainer 从制品库拉取补丁后源码到 emptyDir（本机 Docker 形态由宿主复制）
                        "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}}],
                    },
                },
            },
        }

    def run(self, job: SandboxJob) -> SandboxOutcome:
        raise RuntimeError(
            "本机无 K8s 集群：请使用 DockerSandboxRunner；"
            "生产环境以 render_manifest() 提交 Job（runtimeClassName=gvisor）并轮询 pod 日志"
        )


def run_patch_tests(
    patch: Patch,
    attempt: int,
    *,
    repo_dir: Path | None = None,
    sandbox_root: Path | None = None,
    runner: DockerSandboxRunner | K8sJobSandboxRunner | None = None,
) -> TestReport:
    """编排一次沙箱验证：SAST → 工作区准备 → 隔离执行 → TestReport。"""
    repo_dir = Path(repo_dir) if repo_dir else code_rag.DEFAULT_REPO_DIR
    sandbox_root = Path(sandbox_root) if sandbox_root else DEFAULT_SANDBOX_ROOT

    sast_ok, sast_detail = scan_patch_diff(patch.diff)
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
        )

    job = SandboxJob(patch_id=patch.patch_id, workspace=workspace)
    outcome = (runner or DockerSandboxRunner()).run(job)
    return _report_from_outcome(patch, attempt, outcome, sast_combined)


def _report_from_outcome(
    patch: Patch, attempt: int, outcome: SandboxOutcome, sast_detail: str
) -> TestReport:
    tests_run = outcome.tests_run
    failed = len(outcome.failed_tests)
    if outcome.timed_out:
        unit = f"执行超时（{outcome.duration_seconds:.1f}s 未完成）"
        details = f"沙箱执行超时：未在时限内完成（attempt={attempt}）"
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
        f"demo-app/tests 全量 {tests_run} 用例：{'通过' if outcome.passed else '存在失败'}"
        if tests_run
        else "未执行"
    )
    metrics.observe_sandbox_run(outcome.passed, outcome.duration_seconds)
    return TestReport(
        patch_id=patch.patch_id,
        passed=outcome.passed,
        unit_tests=unit,
        regression_tests=regression,
        sast=sast_detail,
        details=details,
    )
