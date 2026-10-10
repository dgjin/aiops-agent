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
    - 服务端瞬时故障快速重试：仅限「会话初始化被拒」签名（error_during_execution/500/
      num_turns=0）且快速失败（wall<60s）的调用，退避 15s/45s；慢失败与任务类失败不重试
      （见常量注释与 _is_server_transient_failure）；
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
# 2026-10-10 实测：DeepSeek-Flash 在真实前端仓库（约 2.2k 文件）中 20 轮全部耗于只读
# 探索（Glob/Grep/Read）即被 error_max_turns 掐断、从未进入编辑（changed=False），
# 连续 3 次重试均同一失败模式后转人工。预算上调为 60 轮 / 600s，并在 prompt 中加入
# 「先锁定、早编辑」效率策略（见 build_qoder_prompt）。
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_TURNS = 60

# 服务端瞬时故障「快速重试」参数（2026-10-09 排查结论）：
# Qoder 服务端在会话初始化阶段会间歇性返回 500（subtype=error_during_execution、
# num_turns=0、duration_ms=0），呈短时窗口（实测约 30s~数分钟）反复出现；窗口内对
# 「快速失败」的调用做短退避重试有较高恢复概率。仅重试该签名且 wall<SERVER_FAST_FAIL
# 的调用；慢失败（>60s，多为大上下文处理后才报错、单次计费高）不重试，避免无效重复计费。
SERVER_RETRY_BACKOFF_SECONDS = (15, 45)
SERVER_FAST_FAIL_SECONDS = 60

# 工作区复制过滤（与 sandbox 一致：排除缓存/虚拟环境/外层仓库数据/前端依赖与产物目录）
_IGNORE_PATTERNS = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".venv", ".git", "data",
    "node_modules", "dist", "build", "coverage", ".next", "out", "logs",
    # skill 目录：工作区携带多来源同名 skill（如 archify）会让 qodercli 无头会话以
    # "skill name conflict" 中断（exit=1、零改动）；排除后仅剩用户级源，与 demo-app 行为一致。
    ".agents", ".claude", ".qoder",
    # 备份/归档目录：数据库全量备份等恢复点数据（实测 1.9GB）对代码修复无价值
    "backups",
)

# 工作区单文件体积上限：超过该阈值的文件不进工作区（SQL 全量备份、超大运行转录等）。
# 实测 2.2GB 工作区会话初始化启动约 28s、失败计费显著放大；过滤后启动约 2s。
MAX_WORKSPACE_FILE_BYTES = 20 * 1024 * 1024


def _ignore_workspace(dir_path: str, names: list[str]) -> set[str]:
    """工作区复制过滤：名称模式 + 超大文件（>MAX_WORKSPACE_FILE_BYTES）双层。"""
    ignored = set(_IGNORE_PATTERNS(dir_path, names))
    for name in names:
        if name in ignored:
            continue
        path = os.path.join(dir_path, name)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > MAX_WORKSPACE_FILE_BYTES:
                ignored.add(name)
        except OSError:
            continue
    return ignored

# git 全局参数：固定身份、禁用签名、固定默认分支、文件名不转义，保证在 CI 无配置环境下可用。
# core.quotepath=false：非 ASCII 文件名不转义（否则 diff 头输出 "a/\346..."，下游按路径应用补丁会找不到文件）。
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
    "-c",
    "core.quotepath=false",
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
            f"- 优先只修改 `{target_rel}` 这一个文件；"
            "如需新增文件，只允许新增与缺陷/需求直接相关的最小新文件；"
            "禁止改动测试、禁止重命名、禁止格式化或重构无关代码；"
        )
    else:
        # 自主定位模式：目标文件未能预先确定（如前端仓库），交由 Qoder 在仓库内自行定位
        target_block = (
            "## 目标文件\n（未预先定位）请根据嫌疑文件与仓库结构，自行定位缺陷所在的最小文件集合。"
        )
        scope_rule = (
            "- 只修改与缺陷直接相关的最小文件集合；"
            "如需新增文件，只允许新增与缺陷/需求直接相关的最小新文件；"
            "禁止改动测试、禁止重命名、禁止格式化或重构无关代码；"
        )
    return f"""你是资深修复工程师。请完成下面这个修复/实现任务，**必须使用 Edit/Write 工具实际修改仓库中的代码文件**（只读探索不算完成）。

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

## 效率策略（重要）
- 先用最少的只读探索（建议不超过 8 次 Glob/Grep/Read，优先直接读取上述嫌疑文件）快速锁定改动文件与具体行号；
- 锁定后必须立即开始用 Edit/Write 修改，不要继续无目的扫描；工具调用总预算有限，务必预留编辑时间；
- 禁止重复读取同一文件；禁止对无关目录做大范围扫描。

## 修复要求
{scope_rule}
- 做最小化单点修复，保持既有代码风格与类型标注；
- 空指针类缺陷（对象可能为 None，随后被下标/属性访问）：空值校验必须插入到该访问语句**之前**，或直接把访问行改为带条件的安全写法；
- 当前执行环境不提供命令行/测试运行能力，不要尝试运行命令或测试，改动完成即结束（由系统在隔离沙箱中统一验证）；
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
    shutil.copytree(repo_dir, workspace, ignore=_ignore_workspace)
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
        # 仅加载用户级设置源：项目级 skill 源不参与加载，从加载层消除同名 skill 冲突（与工作区复制过滤双保险）
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


def collect_diff(workspace: Path) -> str:
    """采集 Qoder 对工作区的全部改动（unified diff，含 a/ b/ 前缀与新增文件）。空 diff 返回空串。

    先 `git add -A` 把未跟踪的新文件纳入索引，再 `git diff --cached HEAD`——
    否则「新增文件」（需求实现常见形态）不会出现在 diff 中，改动会被误判为「未产生改动」。
    """
    _git(workspace, "add", "-A")
    return _git(workspace, "diff", "--cached", "--no-color", "HEAD")


def _write_run_record(record_dir: Path, patch_id: str, payload: dict) -> str:
    """写入运行留痕 data/qoder/<patch_id>.run.json，返回路径。留痕失败不阻断主流程。"""
    try:
        record_dir.mkdir(parents=True, exist_ok=True)
        path = record_dir / f"{patch_id}.run.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - 留痕失败不影响修复主流程
        return ""


def _parse_result_json(stdout: str) -> dict:
    """解析 CLI stdout 中的 result JSON（仅供「是否值得快速重试」决策）。

    模块契约上不依赖 CLI 的 JSON 字段结构（见模块 docstring「两个不依赖」）；本函数
    只做故障分类，任何解析失败都安全退化为「不重试」。
    """
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _is_server_transient_failure(run: dict) -> bool:
    """识别 Qoder 服务端「会话初始化被拒」的瞬时故障（决定是否快速重试）。

    签名（2026-10-09 实测 12/12 一致）：subtype=error_during_execution + error_code=500
    + num_turns=0；且整次调用 wall < SERVER_FAST_FAIL_SECONDS——仅「快速失败」重试（边际
    成本低、窗口内恢复概率高）。超时、慢失败、任务类失败（max_turns/无改动）不重试。
    """
    if run.get("timed_out"):
        return False
    try:
        if float(run.get("duration_seconds") or 0) >= SERVER_FAST_FAIL_SECONDS:
            return False
        result = _parse_result_json(run.get("stdout", ""))
        return (
            result.get("subtype") == "error_during_execution"
            and int(result.get("error_code") or 0) == 500
            and int(result.get("num_turns") or 0) == 0
        )
    except (TypeError, ValueError):
        return False


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
        "server_retry_attempts": 0,
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
    run: dict = {}
    server_retries = 0
    for round_no in range(1 + len(SERVER_RETRY_BACKOFF_SECONDS)):
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
        if round_no >= len(SERVER_RETRY_BACKOFF_SECONDS):
            break
        if not _is_server_transient_failure(run):
            break
        # 服务端瞬时故障（会话初始化被拒的快速失败签名）：短退避后快速重试
        time.sleep(SERVER_RETRY_BACKOFF_SECONDS[round_no])
        server_retries += 1
    meta["generations"] = 1 + server_retries
    meta["server_retry_attempts"] = server_retries

    diff = collect_diff(workspace)

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
            "server_retry_attempts": server_retries,
            "changed": bool(diff.strip()),
            "diff_stat": _diff_stat(diff),
            "stdout_tail": run["stdout"][-1000:],
            "stderr_tail": run["stderr"][-1000:],
        },
    )

    if run["timed_out"]:
        raise QoderFixError(f"Qoder CLI 超时（>{timeout if timeout is not None else _env('AIOPS_QODER_TIMEOUT', str(DEFAULT_TIMEOUT))}s）")
    if not diff.strip():
        retry_note = (
            "（疑似 Qoder 服务端瞬时故障 error_during_execution/500，"
            f"已快速重试 {server_retries} 次）"
            if server_retries
            else ""
        )
        raise QoderFixError(
            f"Qoder 未对 {target_display} 产生改动{retry_note}（exit={run['exit_code']}）；"
            f"输出摘要: {(run['stderr'] or run['stdout'] or '').strip()[:160]}"
        )

    description = f"Qoder 自主修复 {root_cause.error_type}（目标文件 {target_display}）。"
    risk = "中：由 Qoder CLI 自主生成，已通过 diff 应用与编译校验，仍须经沙箱测试与三道闸门。"
    meta["reason"] = (
        f"Qoder CLI {version}/{meta['model']} 自主修复完成（changed={_diff_stat(diff)}）"
    )
    if server_retries:
        meta["reason"] += f"；服务端瞬时故障重试 {server_retries} 次后成功"
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
