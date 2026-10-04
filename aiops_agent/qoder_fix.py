"""Qoder 修复提供者（接入层）：Qoder CLI 无头调用 → 隔离工作区改动 → git diff。

定位（见《Qoder 修复引擎接入设计》）：
    只负责「提出候选改动」，**不负责判定改动是否正确**——正确性由既有沙箱测试 + 三道闸门裁决。
    本模块不新增、不削弱任何闸门。

契约（供 fix_agent.run_fix 组装 PatchOutput）：
    propose_patch(alert, root_cause, references, attempt, target_rel, repo_dir, ...)
        -> (diff: str, description: str, risk: str, meta: dict)
    失败一律抛 QoderFixError，由 fix_agent.run_fix 统一走安全侧兜底补丁。

两个「不依赖」原则（Qoder CLI 私有格式未公开，故不依赖）：
    1. 不解析 `--output-format json` 的字段结构（仅留痕）；
    2. 不依赖退出码语义；成败以「工作区是否产生可应用 diff」为准。

隔离与安全：
    - 只在 data/qoder/<patch_id>/ 副本内改动，绝不触碰真实代码目录；
    - --permission-mode accept_edits（非 bypass_permissions），配合工具白/黑名单；
    - --max-turns + 子进程超时双限；--no-session-persistence 保证无状态可复现；
    - 子进程以新会话 + /dev/null stdin 运行（脱离控制终端）：后台作业启动的 worker
      场景下，防 qodercli 读终端被 SIGTTIN 停止而永不退出（详见 run_qoder_cli 注释）；
    - 子进程环境经 sanitize_env() 清洗，剔除继承自 Qoder 进程的 Agent-SDK 变量
      （否则 qodercli 误入 SDK 模式，见文末 _ENV_DENY_PREFIXES 注释）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import code_rag
from .models import Alert, RootCause

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_QODER_BIN = "qoder"
DEFAULT_QODER_ROOT = BASE_DIR / "data" / "qoder"
# 默认修复模型。注意：Qoder 模型 ID **区分大小写**，实测 `-m deepseek-flash`（小写）不被识别，
# CLI 会静默回退到 auto（仅 stderr 警告、退出码 0）——因此必须使用规范写法 `DeepSeek-Flash`。
DEFAULT_QODER_MODEL = "DeepSeek-Flash"
DEFAULT_PERMISSION_MODE = "accept_edits"
DEFAULT_ALLOWED_TOOLS = "Read,Edit,Write,Grep,Glob"
DEFAULT_DISALLOWED_TOOLS = "Bash"
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_TURNS = 20

# 工作区复制过滤（与 sandbox 一致：排除缓存/虚拟环境/外层仓库数据/前端依赖与产物目录）
_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".venv", ".git", "data",
    "node_modules", "dist", "build", "coverage", ".next", "out", "logs",
    # skill 目录：工作区携带多来源同名 skill（如 archify）会让 qodercli 无头会话以
    # "skill name conflict" 中断（exit=1、零改动）；排除后仅剩用户级源，与 demo-app 行为一致。
    ".agents", ".claude", ".qoder",
)

# git 全局参数：固定身份、禁用签名、固定默认分支，保证在 CI 无配置环境下可用
_GIT_BASE = [
    "git",
    "-c",
    "user.email=aiops-agent@local",
    "-c",
    "user.name=aiops-agent",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
]

# 留痕中 stdout/stderr 的最大保留长度
_TRUNCATE = 4000

# 需从子进程环境中剔除的 Qoder 内部 / Agent-SDK 变量。
# 背景（实测）：若 AIOps worker 运行在 Qoder 自身进程树下，这些变量会被继承，qodercli 会误判
# 为「Agent SDK 入口」并进入 SDK 模式，报
#   sdk_invalid_args: Agent SDK entrypoint env is set but required flags are missing
# 触发变量已定位为 QODER_AGENT_SDK_ENTRYPOINT（如 =sdk-ts）。
_ENV_DENY_PREFIXES = (
    "QODER_AGENT_SDK",
    "QODER_WORKER",
    "QODER_SDK",
    "QODERCLI_RUNTIME",
    "QODER_SESSION_",
    "QODER_MCP_",
    "QODER_CLI_",
)
_ENV_DENY_EXACT = ("FEATURE_FLAGS",)


def sanitize_env(base: dict[str, str]) -> dict[str, str]:
    """剔除继承自 Qoder 进程的内部变量，避免 qodercli 误入 Agent-SDK 模式。

    保留 QODER_PERSONAL_ACCESS_TOKEN / QODER_CONFIG_DIR 等鉴权与配置变量（不匹配任何前缀）。
    """
    return {
        key: value
        for key, value in base.items()
        if key not in _ENV_DENY_EXACT and not key.startswith(_ENV_DENY_PREFIXES)
    }


class QoderFixError(RuntimeError):
    """Qoder 修复提供者的可降级错误（由 fix_agent.run_fix 捕获并回落到兜底补丁）。"""


def _env(name: str, default: str) -> str:
    """读取环境变量（调用时求值，便于测试注入）。空串视为未设置。"""
    value = os.environ.get(name)
    return value if value else default


def _resolve_executable(exe: str) -> str | None:
    """解析可执行文件：优先当作路径，其次走 PATH 查找。"""
    path = Path(exe)
    if path.is_file():
        return str(path)
    return shutil.which(exe)


def resolve_model(explicit: str | None = None) -> str:
    """解析修复模型：显式参数 > AIOPS_QODER_MODEL > 默认 DeepSeek-Flash。"""
    if explicit:
        return explicit
    return os.environ.get("AIOPS_QODER_MODEL") or DEFAULT_QODER_MODEL


def _detect_model_warning(stderr: str) -> str:
    """捕获 Qoder CLI 的「模型不可用→静默回退 auto」警告。

    实测：无效模型名（如小写 deepseek-flash）不会报错，退出码 0、结果看似成功，
    仅 stderr 输出 `Model "x" is not available right now; using "auto" instead.`。
    若不捕获，会误以为指定模型生效。返回警告原文（无警告返回空串）。
    """
    for line in (stderr or "").splitlines():
        if "is not available right now" in line:
            return line.strip()
    return ""


def qoder_available(bin_path: str | None = None, timeout: int = 15) -> tuple[bool, str]:
    """检查 Qoder CLI 是否可用。返回 (available, version_or_reason)，不抛异常。"""
    exe = bin_path or _env("AIOPS_QODER_BIN", DEFAULT_QODER_BIN)
    resolved = _resolve_executable(exe)
    if not resolved:
        return False, f"未找到 Qoder CLI 可执行文件: {exe}（安装：curl -fsSL https://qoder.com/install | bash）"
    try:
        proc = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 - 探测失败一律视为不可用
        return False, f"Qoder CLI 调用失败: {type(exc).__name__}: {exc}"
    text = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 and not text:
        return False, f"Qoder CLI --version 退出码 {proc.returncode}"
    version = text.splitlines()[0] if text else "unknown"
    return True, version


def build_qoder_prompt(
    alert: Alert,
    root_cause: RootCause,
    references: list[dict] | None,
    target_rel: str,
    retry_hint: str = "",
) -> str:
    """构造交给 Qoder 的修复任务说明。

    与 fix_agent.build_fix_prompt 语义一致，但弱化「内联文件全文」——Qoder 能自主读文件；
    强化「单点、单文件、禁重构」约束，并保留最小化修复与重试强化提示。
    """
    refs: list[str] = []
    for ref in references or []:
        kind = ref.get("kind")
        if kind == "ticket":
            refs.append(f"- [{ref.get('similarity')}] 历史工单 {ref.get('qualname')}: {ref.get('snippet', '')[:180]}")
        elif kind == "code":
            refs.append(
                f"- [{ref.get('similarity')}] 代码 {ref.get('file')}::{ref.get('qualname')}"
                f" (L{ref.get('start_line')}-{ref.get('end_line')})"
            )
        else:
            refs.append(f"- {ref}")
    refs_text = "\n".join(refs) or "- （无）"
    retry_section = (
        f"\n## 重试强化提示（上一轮候选修复未通过沙箱测试）\n{retry_hint}\n" if retry_hint else ""
    )
    if target_rel:
        target_block = (
            f"## 目标文件\n{target_rel}\n"
            "（请自行读取该文件，必要时读取其调用方与既有测试以确认修复位置）"
        )
        scope_rule = (
            f"- 只修改 `{target_rel}` 这一个文件；"
            "禁止新增文件、禁止改动测试、禁止重命名、禁止格式化或重构无关代码；"
        )
    else:
        # 自主定位模式：目标文件未能预先确定（如前端仓库），交由 Qoder 在仓库内自行定位
        target_block = (
            "## 目标文件\n（未预先定位）请根据嫌疑文件与仓库结构，自行定位缺陷所在的最小文件集合。"
        )
        scope_rule = (
            "- 只修改与缺陷直接相关的最小文件集合；"
            "禁止新增文件、禁止改动测试、禁止重命名、禁止格式化或重构无关代码；"
        )
    return f"""你是资深修复工程师。请修复下面这个线上缺陷，直接修改仓库中的代码文件。

## 告警
- alert_id: {alert.alert_id}
- 服务: {alert.service}
- 描述: {alert.description}

## 根因分析（confidence={root_cause.confidence:.2f}）
- 类型: {root_cause.error_type}
- 说明: {root_cause.summary}
- 嫌疑文件: {", ".join(root_cause.suspect_files) or "（无）"}

## 相似历史修复（Code RAG 检索）
{refs_text}

{target_block}

## 修复要求
{scope_rule}
- 做最小化单点修复，保持既有代码风格与类型标注；
- 空指针类缺陷（对象可能为 None，随后被下标/属性访问）：空值校验必须插入到该访问语句**之前**，或直接把访问行改为带条件的安全写法；
- 修改后请运行仓库既有单测确认目标用例转绿（若环境允许），再结束任务；
- 完成后直接结束，**不要输出 diff 文本**（改动由系统通过 git 自动采集）。
{retry_section}"""


def _git(workspace: Path, *args: str, timeout: int = 60) -> str:
    """在隔离工作区执行 git 命令，失败抛 QoderFixError。"""
    cmd = [*_GIT_BASE, "-C", str(workspace), "--no-pager", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise QoderFixError(f"git 命令超时: {' '.join(args)}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise QoderFixError(f"git {' '.join(args)} 失败（exit={proc.returncode}）: {detail}")
    return proc.stdout


def prepare_qoder_workspace(repo_dir: Path, patch_id: str, root: Path | None = None) -> Path:
    """复制仓库到隔离工作区并建立 git 基线提交。返回工作区路径。

    基线提交保证后续 `git diff` 只反映 Qoder 的改动（不依赖外层仓库状态）。
    """
    root = Path(root) if root else Path(_env("AIOPS_QODER_ROOT", str(DEFAULT_QODER_ROOT)))
    workspace = root / patch_id
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo_dir, workspace, ignore=_IGNORE)
    if not any(workspace.iterdir()):
        raise QoderFixError(f"仓库目录为空，无法建立工作区: {repo_dir}")
    _git(workspace, "init", "-q")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "baseline")
    return workspace


def run_qoder_cli(
    workspace: Path,
    prompt: str,
    *,
    exe: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
    max_turns: int | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: str | None = None,
) -> dict:
    """以无头模式调用 Qoder CLI（工作目录 = 隔离工作区）。返回运行元数据。

    不使用 --yolo / bypass_permissions：保持最小权限，仅自动接受工作区内的安全编辑。
    """
    exe = exe or _env("AIOPS_QODER_BIN", DEFAULT_QODER_BIN)
    resolved = _resolve_executable(exe)
    if not resolved:
        raise QoderFixError(f"未找到 Qoder CLI 可执行文件: {exe}")
    timeout = int(timeout if timeout is not None else _env("AIOPS_QODER_TIMEOUT", str(DEFAULT_TIMEOUT)))
    max_turns = int(max_turns if max_turns is not None else _env("AIOPS_QODER_MAX_TURNS", str(DEFAULT_MAX_TURNS)))
    permission_mode = permission_mode or _env("AIOPS_QODER_PERMISSION_MODE", DEFAULT_PERMISSION_MODE)
    allowed = allowed_tools if allowed_tools is not None else _env("AIOPS_QODER_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS)
    disallowed = (
        disallowed_tools
        if disallowed_tools is not None
        else _env("AIOPS_QODER_DISALLOWED_TOOLS", DEFAULT_DISALLOWED_TOOLS)
    )
    model = resolve_model(model)

    argv = [
        resolved,
        "-p",
        prompt,
        "-o",
        "json",
        "--permission-mode",
        permission_mode,
        "-w",
        str(workspace),
        "--max-turns",
        str(max_turns),
        "--no-session-persistence",
        # 仅加载用户级设置源：项目级 skill 源不参与加载，从加载层消除同名 skill 冲突（与 _IGNORE 双保险）
        "--setting-sources",
        "user",
    ]
    if allowed:
        argv += ["--allowed-tools", allowed]
    if disallowed:
        argv += ["--disallowed-tools", disallowed]
    if model:
        argv += ["-m", model]

    # 环境：剔除继承的 Qoder 内部/Agent-SDK 变量（否则 qodercli 误入 SDK 模式）
    env = sanitize_env(os.environ)
    # 鉴权：显式 AIOPS_QODER_TOKEN 优先转写为官方环境变量；否则沿用进程内已有 PAT/登录态
    token = os.environ.get("AIOPS_QODER_TOKEN")
    if token:
        env["QODER_PERSONAL_ACCESS_TOKEN"] = token

    start = time.monotonic()
    timed_out = False
    try:
        # stdin=DEVNULL + start_new_session（新会话，脱离控制终端）：qodercli 为交互式 CLI，
        # 启动期会探测/读取终端。worker 由后台作业（nohup … &）启动时进程组非前台，子进程读
        # 控制终端会收到 SIGTTIN 被内核停止（ps STAT=T）且永不退出 → generate_patch 挂死、
        # 子进程超时保护失效、Temporal 活动超时后重试也无法执行（实测教训 2026-10-04）。
        # 新会话无控制终端：终端访问只会失败并回退到无头模式，不再被挂起。
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        exit_code: int | None = proc.returncode
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = f"Qoder CLI 超时（>{timeout}s），已终止"
    duration = round(time.monotonic() - start, 2)

    return {
        "command": argv,
        # 注意：argv 不含 token（token 走环境变量），可安全留痕
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "stdout": stdout[-_TRUNCATE:],
        "stderr": stderr[-_TRUNCATE:],
        "auth_mode": "pat" if env.get("QODER_PERSONAL_ACCESS_TOKEN") else "session",
    }


def collect_diff(workspace: Path, target_rel: str) -> str:
    """采集 Qoder 对目标文件的改动（unified diff，含 a/ b/ 前缀）。空 diff 返回空串。"""
    scope = target_rel or "."
    return _git(workspace, "diff", "--no-color", "HEAD", "--", scope)


def _write_run_record(record_dir: Path, patch_id: str, payload: dict) -> str:
    """写入运行留痕 data/qoder/<patch_id>.run.json，返回路径。留痕失败不阻断主流程。"""
    try:
        record_dir.mkdir(parents=True, exist_ok=True)
        path = record_dir / f"{patch_id}.run.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - 留痕失败不影响修复主流程
        return ""


def propose_patch(
    alert: Alert,
    root_cause: RootCause,
    references: list[dict] | None,
    attempt: int,
    target_rel: str,
    repo_dir: Path | None = None,
    *,
    model: str | None = None,
    timeout: int | None = None,
    max_turns: int | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: str | None = None,
    workspace_root: Path | None = None,
    qoder_bin: str | None = None,
) -> tuple[str, str, str, dict]:
    """调用 Qoder 无头修复，返回 (diff, description, risk, meta)。

    任一步失败抛 QoderFixError（由 run_fix 回落兜底补丁，保证主流程不中断）。
    """
    repo_dir = Path(repo_dir) if repo_dir else code_rag.DEFAULT_REPO_DIR
    patch_id = f"p-{alert.alert_id}-r{attempt}"
    target_display = target_rel or "（仓库级自主定位）"
    meta: dict = {
        "provider": "qoder",
        "stub": False,
        "generations": 1,
        "reason": "",
        "cli_version": "",
        "exit_code": None,
        "timed_out": False,
        "workspace": "",
        "run_record": "",
        "model": "",
        "model_warning": "",
        "model_version": "",
        "elapsed_seconds": 0.0,
    }
    start = time.monotonic()

    exe = qoder_bin or _env("AIOPS_QODER_BIN", DEFAULT_QODER_BIN)
    available, version = qoder_available(exe)
    if not available:
        raise QoderFixError(version)
    meta["cli_version"] = version
    meta["model_version"] = f"qoder-cli:{version}"

    workspace = prepare_qoder_workspace(repo_dir, patch_id, workspace_root)
    meta["workspace"] = str(workspace)

    prompt = build_qoder_prompt(
        alert,
        root_cause,
        references,
        target_rel,
        retry_hint=_retry_hint(attempt),
    )
    run = run_qoder_cli(
        workspace,
        prompt,
        exe=exe,
        model=model,
        timeout=timeout,
        max_turns=max_turns,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )
    meta["exit_code"] = run["exit_code"]
    meta["timed_out"] = run["timed_out"]
    meta["model"] = resolve_model(model)
    meta["model_warning"] = _detect_model_warning(run["stderr"])

    diff = collect_diff(workspace, target_rel)

    record_dir = Path(workspace_root) if workspace_root else Path(_env("AIOPS_QODER_ROOT", str(DEFAULT_QODER_ROOT)))
    meta["run_record"] = _write_run_record(
        record_dir,
        patch_id,
        {
            "alert_id": alert.alert_id,
            "patch_id": patch_id,
            "target_file": target_rel,
            "cli_version": version,
            "model": meta["model"],
            "model_warning": meta["model_warning"],
            "auth_mode": run["auth_mode"],
            "command": run["command"],
            "exit_code": run["exit_code"],
            "timed_out": run["timed_out"],
            "duration_seconds": run["duration_seconds"],
            "changed": bool(diff.strip()),
            "diff_stat": _diff_stat(diff),
            "stdout_tail": run["stdout"][-1000:],
            "stderr_tail": run["stderr"][-1000:],
        },
    )

    if run["timed_out"]:
        raise QoderFixError(f"Qoder CLI 超时（>{timeout if timeout is not None else _env('AIOPS_QODER_TIMEOUT', str(DEFAULT_TIMEOUT))}s）")
    if not diff.strip():
        raise QoderFixError(
            f"Qoder 未对 {target_display} 产生改动（exit={run['exit_code']}）；"
            f"输出摘要: {(run['stderr'] or run['stdout'] or '').strip()[:160]}"
        )

    description = f"Qoder 自主修复 {root_cause.error_type}（目标文件 {target_display}）。"
    risk = "中：由 Qoder CLI 自主生成，已通过 diff 应用与编译校验，仍须经沙箱测试与三道闸门。"
    meta["reason"] = (
        f"Qoder CLI {version}/{meta['model']} 自主修复完成（changed={_diff_stat(diff)}）"
    )
    if meta["model_warning"]:
        meta["reason"] += f"；⚠ 模型回退警告：{meta['model_warning']}"
    meta["elapsed_seconds"] = round(time.monotonic() - start, 2)
    return diff, description, risk, meta


def _retry_hint(attempt: int) -> str:
    """attempt>0 的重试强化提示（与 fix_agent 口径一致）。"""
    if attempt <= 0:
        return ""
    return (
        "上一轮候选修复未通过沙箱测试。请复查防护位置：\n"
        "- 若缺陷为「对象可能为 None，随后被下标/属性访问」，在该访问行之后追加校验无效——运行时会在校验前抛异常；\n"
        "- 请把校验行插入到该访问行之前，或直接把访问行改为带条件的安全写法"
        '（如 `x = obj["k"] if obj else 0`）。'
    )


def _diff_stat(diff: str) -> str:
    """diff 摘要：+新增/-删除 行数（留痕用）。"""
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return f"+{added}/-{removed}"
