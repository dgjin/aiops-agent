"""修复 Agent（WP4 交付物）：LLM 生成最小化 unified diff + 双重校验 + 安全侧兜底。

流程（对齐实施计划 WP4 任务 3）：
    run_fix(alert, root_cause, references, attempt)
      1. 定位目标文件（suspect_files 模糊匹配 demo-app，如 OrderService.java → order_service.py）；
      2. build_fix_prompt：目标文件完整内容（带行号）+ 根因 + Code RAG 引用 → 要求输出 JSON {diff, description, risk}；
         attempt>0 时追加重试强化提示（防护须置于解引用之前，或直接修正问题行）；
      3. PatchOutput pydantic 校验；
      4. apply_unified_diff 应用 + compile() 编译校验（对应验收「建议 diff 可编译」）；
      5. 任一步失败 → 确定性兜底 diff（degraded=True 留痕），绝不阻塞主流程；
         兜底 diff 基于目标文件真实内容动态构造，可被沙箱真实应用并执行测试。

演示分支短路（保证演示矩阵不依赖 LLM）：
    description 命中 low-conf / protected / test-fail / test-always-fail / canary-bad 时走确定性补丁：
      - test-fail：attempt=0 注入「防护位置错误」候选补丁（沙箱真实测试失败），attempt>=1 修正版；
      - test-always-fail：所有 attempt 均为错误候选（沙箱持续失败 → 重试超限升级）；
      - 其余分支（protected / canary-bad / low-conf）：语义正确的最小修复版（沙箱通过）。

环境变量：
    AIOPS_FIX_MODEL  生成模型（默认复用 triage 默认模型；可指向更大模型提升 diff 语义正确率，
                     如 AIOPS_FIX_MODEL=qwen3.8:27b-mlx）
"""

from __future__ import annotations

import os
import re
import time
from itertools import chain
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import code_rag
from .models import Alert, Patch, RootCause
from .triage import DEFAULT_MODEL, call_ollama, strip_code_fence

# 修复生成模型：AIOPS_FIX_MODEL 未设置时复用 triage 默认模型
DEFAULT_FIX_MODEL = os.environ.get("AIOPS_FIX_MODEL") or DEFAULT_MODEL

# 演示分支关键词：命中即走确定性补丁，避免破坏演示矩阵
_DEMO_STUB_MARKERS = ("low-conf", "protected", "test-fail", "test-always-fail", "canary-bad")

STUB_MODEL_VERSION = "aiops-fix-demo-v0.1"

# unified diff 应用时允许的行号偏移窗口（LLM 按带行号文件生成，通常精确）
_HUNK_SEARCH_WINDOW = 30

# attempt>0 时的重试强化提示（针对「防护位置错误」这一高频失败模式）
_RETRY_HINT = (
    "上一轮候选修复未通过沙箱测试。请复查防护位置：\n"
    "- 若缺陷为「对象可能为 None，随后被下标/属性访问」，在该访问行之后追加校验无效——运行时会在校验前抛异常；\n"
    "- 请把校验行插入到该访问行之前，或直接把访问行改为带条件的安全写法"
    '（如 `x = obj["k"] if obj else 0`）。'
)


class PatchOutput(BaseModel):
    """LLM 结构化补丁输出 schema（pydantic 强校验）。"""

    model_config = ConfigDict(extra="ignore")

    diff: str
    description: str
    risk: str


def _suspect_tokens(suspect: str) -> set[str]:
    """提取嫌疑路径词元（目录名 + 文件名蛇形拆分），用于候选相关性打分。"""
    path = Path(suspect)
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", path.name)
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()
    tokens = set(snake.split("_"))
    for part in path.parent.parts:
        tokens.update(re.sub(r"(?<!^)(?=[A-Z])", "_", part).lower().split("_"))
    tokens.discard("")
    return tokens


def locate_target_file(suspect_files: list[str], repo_dir: Path) -> Path | None:
    """按 suspect_files 模糊定位目标文件：OrderService.java → order_service.py（类名转蛇形）。

    打分：候选文件名与嫌疑路径词元的命中数优先（src/order/service.py → order_service.py
    优于 token_service.py），其次非 tests 目录、路径字典序，保证确定性。
    """
    tokens: set[str] = set()
    candidates: list[Path] = []
    for suspect in suspect_files or []:
        tokens |= _suspect_tokens(suspect)
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", Path(suspect).name)
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()
        for pattern in (f"{snake}.py", f"*{snake}*.py"):
            for path in sorted(repo_dir.rglob(pattern)):
                if "__pycache__" in path.parts or path in candidates:
                    continue
                candidates.append(path)

    def sort_key(path: Path) -> tuple:
        hits = len(set(path.stem.split("_")) & tokens)
        return (-hits, "tests" in path.parts, str(path))

    candidates.sort(key=sort_key)
    return candidates[0] if candidates else None


def _line_numbered(text: str) -> str:
    return "\n".join(f"{i:>4}| {line}" for i, line in enumerate(text.splitlines(), 1))


def _format_references(references: list[dict] | None) -> str:
    lines: list[str] = []
    for ref in references or []:
        kind = ref.get("kind")
        if kind == "ticket":
            lines.append(f"- [{ref.get('similarity')}] 工单 {ref.get('qualname')}: {ref.get('snippet', '')[:180]}")
        elif kind == "code":
            lines.append(
                f"- [{ref.get('similarity')}] 代码 {ref.get('file')}::{ref.get('qualname')}"
                f" (L{ref.get('start_line')}-{ref.get('end_line')})"
            )
        else:
            lines.append(f"- {ref}")
    return "\n".join(lines) or "- （无）"


def build_fix_prompt(
    alert: Alert,
    root_cause: RootCause,
    references: list[dict] | None,
    target_rel: str,
    file_content: str,
    retry_hint: str = "",
) -> str:
    """构造修复 prompt：根因 + RAG 引用 + 目标文件完整内容（带行号）；retry_hint 供重试强化。"""
    retry_section = ""
    if retry_hint:
        retry_section = f"\n## 重试强化提示（上一轮候选修复未通过沙箱测试）\n{retry_hint}\n"
    return f"""你是资深修复工程师。请针对以下故障生成最小化修复补丁（unified diff），只输出一个 JSON 对象。

## 告警
- alert_id: {alert.alert_id}
- 服务: {alert.service}
- 描述: {alert.description}

## 根因分析（confidence={root_cause.confidence:.2f}）
- 类型: {root_cause.error_type}
- 说明: {root_cause.summary}
- 嫌疑文件: {", ".join(root_cause.suspect_files) or "（无）"}

## 相似历史修复（Code RAG 检索）
{_format_references(references)}

## 目标文件完整内容（带行号）
{_line_numbered(file_content)}
{retry_section}
## 修复要求
- 只输出最小化 unified diff：必须含 @@ -start,count +start,count @@ 行；
- 上下文行必须与「目标文件完整内容」逐字符一致（含缩进、行内注释与空行），禁止省略、合并或改写任何上下文行；
- 空指针类缺陷（对象可能为 None，且后续存在下标/属性访问）：新增的空值校验必须通过 + 行插入到该下标/属性访问语句之前；
- 仅修改必要行，禁止顺手重构 / 重命名 / 格式化无关代码；
- diff 文件头使用 a/{target_rel} 与 b/{target_rel}；
- 保持既有代码风格与类型标注。

## 输出 JSON（不要输出其他内容）
{{
  "diff": "--- a/{target_rel}\\n+++ b/{target_rel}\\n@@ -... @@\\n ...",
  "description": "一句中文变更说明（最小化、单点修复）",
  "risk": "风险等级与理由，如：低：仅新增防御分支，不改动既有逻辑"
}}"""


def _parse_hunks(diff_text: str) -> list[dict]:
    """解析 unified diff 的 hunk 列表（演示约束：单文件 diff）。

    宽容处理 LLM 常见的非标准 hunk 头：`@@ -34,6 @@`（缺 +start,count 部分）。
    """
    hunks: list[dict] = []
    current: dict | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)?(?: \+\d+(?:,\d+)?)? @@", raw)
            if not match:
                return []
            current = {"old_start": int(match.group(1)), "lines": []}
            hunks.append(current)
        elif current is not None:
            if raw.startswith("--- ") or raw.startswith("+++ "):
                continue  # 文件头（多文件 diff 时忽略后续文件）
            if raw[:1] in (" ", "+", "-"):
                current["lines"].append(raw)
    return [h for h in hunks if h["lines"]]


def _match_context(src_line: str, content: str) -> bool:
    """上下文/删除行匹配：严格相等优先；随后容忍行内空白差异（前导缩进必须一致）。

    实测 LLM 高频失败模式：复制上下文行时改写行内注释前的对齐空格（如 2 空格变 3 空格）。
    缩进差异不视为匹配（Python 语义敏感）；宽松匹配的残余风险由编译 + 沙箱双重校验兜底。
    """
    if src_line == content:
        return True
    indent_src = src_line[: len(src_line) - len(src_line.lstrip())]
    indent_ctx = content[: len(content) - len(content.lstrip())]
    if indent_src != indent_ctx:
        return False
    return " ".join(src_line.split()) == " ".join(content.split())


def _apply_hunk(src_lines: list[str], hunk: dict, pos: int) -> tuple[int, list[str]] | None:
    """在 src_lines 的 pos 处试应用 hunk：全部上下文/删除行匹配则返回 (新游标, 输出行)，否则 None。

    容错（均为 diff 语义不变的放宽）：
    - LLM 常省略上下文中的空行：允许跳过源文件中的空白行再继续匹配；
    - 行内空白差异按折叠匹配（见 _match_context）；
    - 上下文行输出原样保留源文件文本，避免 LLM 的空白改写污染未修改行。
    """
    cursor = pos
    out: list[str] = []
    for raw in hunk["lines"]:
        tag, content = raw[:1], raw[1:]
        if tag == "+":
            out.append(content)
            continue
        if tag not in (" ", "-"):
            continue
        while cursor < len(src_lines) and src_lines[cursor].strip() == "" and src_lines[cursor] != content:
            cursor += 1
        if cursor >= len(src_lines) or not _match_context(src_lines[cursor], content):
            return None
        if tag == " ":
            out.append(src_lines[cursor])
        cursor += 1
    return cursor, out


def apply_unified_diff(original: str, diff_text: str) -> str | None:
    """将 unified diff 应用到原始文本，失败返回 None（不抛异常，由调用方兜底）。"""
    hunks = _parse_hunks(strip_code_fence(diff_text))
    if not hunks:
        return None
    src_lines = original.splitlines()
    out: list[str] = []
    cursor = 0
    for hunk in hunks:
        hint = hunk["old_start"] - 1
        offsets = chain(
            [0],
            chain(range(-1, -_HUNK_SEARCH_WINDOW - 1, -1), range(1, _HUNK_SEARCH_WINDOW + 1)),
        )
        found: tuple[int, tuple[int, list[str]]] | None = None
        for offset in offsets:
            pos = hint + offset
            if pos < cursor or pos < 0:
                continue
            applied = _apply_hunk(src_lines, hunk, pos)
            if applied is not None:
                found = (pos, applied)
                break
        if found is None:
            return None
        pos, (end, hunk_out) = found
        out.extend(src_lines[cursor:pos])
        out.extend(hunk_out)
        cursor = end
    out.extend(src_lines[cursor:])
    result = "\n".join(out)
    return result + "\n" if original.endswith("\n") else result


def validate_source(new_text: str, filename: str) -> tuple[bool, str]:
    """编译校验（对应验收「建议 diff 可编译」）。"""
    try:
        compile(new_text, filename, "exec")
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc.msg} (line {exc.lineno})"
    return True, ""


def _replace_lines_diff(
    target_rel: str,
    content: str,
    anchor_substr: str,
    new_lines: list[str],
    *,
    span: int = 1,
    context: int = 2,
) -> str | None:
    """把锚行（含 anchor_substr）起的 span 行替换为 new_lines，构造最小单 hunk diff。"""
    lines = content.splitlines()
    idx = next((i for i, line in enumerate(lines) if anchor_substr in line), None)
    if idx is None or idx + span > len(lines):
        return None
    old_lines = lines[idx : idx + span]
    start = max(0, idx - context)
    before = lines[start:idx]
    after = lines[idx + span : idx + span + context]
    body = [f" {line}" for line in before]
    body += [f"-{line}" for line in old_lines]
    body += [f"+{line}" for line in new_lines]
    body += [f" {line}" for line in after]
    old_start = start + 1
    old_count = len(before) + len(old_lines) + len(after)
    new_count = len(before) + len(new_lines) + len(after)
    return (
        f"--- a/{target_rel}\n+++ b/{target_rel}\n"
        f"@@ -{old_start},{old_count} +{old_start},{new_count} @@\n"
        + "\n".join(body)
        + "\n"
    )


def _insert_after_diff(
    target_rel: str,
    content: str,
    anchor_substr: str,
    added_lines: list[str],
    *,
    context: int = 2,
) -> str | None:
    """在锚行之后插入 added_lines，构造最小单 hunk diff。"""
    lines = content.splitlines()
    idx = next((i for i, line in enumerate(lines) if anchor_substr in line), None)
    if idx is None:
        return None
    return _replace_lines_diff(
        target_rel, content, anchor_substr, [lines[idx], *added_lines], context=context
    )


def _fallback_diff_for(target_rel: str, content: str, bad: bool) -> tuple[str | None, str]:
    """按目标文件形态生成确定性 diff。返回 (diff, description)；bad=True 为演示用错误候选。"""
    name = Path(target_rel).name
    if name == "order_service.py":
        if bad:
            # 演示候选：校验追加在解引用行之后——运行时仍先抛 TypeError（供沙箱真实拦截）
            return (
                _insert_after_diff(
                    target_rel,
                    content,
                    "discount = coupon",
                    ['        if coupon is None:', '            raise ValueError("无效优惠券")'],
                ),
                "对优惠券空值路径新增校验（候选方案）。",
            )
        return (
            _replace_lines_diff(
                target_rel,
                content,
                "discount = coupon",
                ['        discount = coupon["discount"] if coupon else 0'],
            ),
            "空优惠券按无折扣处理（单点最小修复，不改动其他逻辑）。",
        )
    if name == "token_service.py":
        return (
            _insert_after_diff(
                target_rel,
                content,
                'expiry_text, _, signature = token.partition(".")',
                ['        if not expiry_text.isdigit():', '            raise TokenError(f"令牌格式非法: {token!r}")'],
            ),
            "令牌格式非法时抛出 TokenError，避免裸 ValueError 泄漏。",
        )
    return None, ""


def fallback_patch(
    alert: Alert,
    root_cause: RootCause,
    attempt: int,
    target_rel: str,
    *,
    bad: bool = False,
    repo_dir: Path | None = None,
) -> Patch:
    """确定性兜底补丁：LLM 不可用/输出非法/演示分支时使用（安全侧，流程不中断）。

    diff 基于目标文件真实内容动态构造（可被沙箱真实应用并执行测试）；
    目标文件不可读或形态未知时退回占位 diff（沙箱将报告「无法应用」）。
    """
    repo_dir = Path(repo_dir) if repo_dir else code_rag.DEFAULT_REPO_DIR
    diff: str | None = None
    description = ""
    target = repo_dir / target_rel
    if target.is_file():
        diff, description = _fallback_diff_for(target_rel, target.read_text(encoding="utf-8"), bad)
    if not diff:
        diff = (
            f"--- a/{target_rel}\n"
            f"+++ b/{target_rel}\n"
            f"@@ -42,6 +42,9 @@ def submit(payload):\n"
            f"+    if payload.get('coupon') is None:\n"
            f"+        return _submit_without_coupon(payload)\n"
        )
        description = "对空值路径添加提前返回（占位补丁：目标文件不可读或形态未知）。"
    return Patch(
        patch_id=f"p-{alert.alert_id}-r{attempt}",
        alert_id=alert.alert_id,
        files=[target_rel],
        diff=diff,
        description=description,
        risk="低：仅新增防御分支，不改动既有逻辑。",
        model_version=STUB_MODEL_VERSION,
        confidence=root_cause.confidence,
    )


def run_fix(
    alert: Alert,
    root_cause: RootCause,
    references: list[dict] | None,
    attempt: int,
    repo_dir: Path | None = None,
    model: str | None = None,
    timeout: int = 45,
) -> tuple[Patch, dict]:
    """执行一次修复生成：返回 (Patch, meta)。meta 含 degraded/validated/elapsed 等留痕信息。

    模型优先级：显式 model 参数 > AIOPS_FIX_MODEL（DEFAULT_FIX_MODEL）> triage 默认模型。
    timeout 为大模型推理预留（默认 45s；大模型评估可上调）。
    """
    repo_dir = Path(repo_dir) if repo_dir else code_rag.DEFAULT_REPO_DIR
    used_model = model or DEFAULT_FIX_MODEL
    meta: dict = {
        "model": used_model,
        "degraded": False,
        "stub": False,
        "validated": False,
        "reason": "",
        "generations": 0,
        "elapsed_seconds": 0.0,
    }
    start = time.monotonic()
    desc = alert.description.lower()

    target = locate_target_file(root_cause.suspect_files, repo_dir)
    target_rel = (
        str(target.relative_to(repo_dir))
        if target
        else (root_cause.suspect_files[0] if root_cause.suspect_files else "src/unknown.py")
    )

    if any(marker in desc for marker in _DEMO_STUB_MARKERS):
        bad = "test-always-fail" in desc or ("test-fail" in desc and attempt == 0)
        patch = fallback_patch(alert, root_cause, attempt, target_rel, bad=bad, repo_dir=repo_dir)
        kind = "沙箱将拦截的错误候选" if bad else "修复候选"
        meta.update({"stub": True, "reason": f"演示分支：确定性补丁（{kind}）"})
        meta["elapsed_seconds"] = round(time.monotonic() - start, 2)
        return patch, meta

    if target is None:
        patch = fallback_patch(alert, root_cause, attempt, target_rel, repo_dir=repo_dir)
        meta.update({"degraded": True, "reason": f"未定位到目标文件: {root_cause.suspect_files}"})
        meta["elapsed_seconds"] = round(time.monotonic() - start, 2)
        return patch, meta

    content = target.read_text(encoding="utf-8")
    try:
        prompt = build_fix_prompt(
            alert,
            root_cause,
            references,
            target_rel,
            content,
            retry_hint=_RETRY_HINT if attempt > 0 else "",
        )
        raw = call_ollama(prompt, model=used_model, timeout=timeout)
        meta["generations"] = 1
        parsed = PatchOutput.model_validate_json(strip_code_fence(raw))
        new_text = apply_unified_diff(content, parsed.diff)
        if new_text is None:
            raise ValueError("diff 无法应用到目标文件（上下文不匹配）")
        ok, error = validate_source(new_text, target_rel)
        if not ok:
            raise ValueError(f"补丁编译校验失败: {error}")
        patch = Patch(
            patch_id=f"p-{alert.alert_id}-r{attempt}",
            alert_id=alert.alert_id,
            files=[target_rel],
            diff=parsed.diff,
            description=parsed.description,
            risk=parsed.risk,
            model_version=used_model,
            confidence=root_cause.confidence,
        )
        meta["validated"] = True
    except Exception as exc:  # noqa: BLE001 - 一切异常走安全侧兜底，不阻断流程
        meta.update({"degraded": True, "reason": f"{type(exc).__name__}: {exc}"})
        patch = fallback_patch(alert, root_cause, attempt, target_rel, repo_dir=repo_dir)

    meta["elapsed_seconds"] = round(time.monotonic() - start, 2)
    return patch, meta
