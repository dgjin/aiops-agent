"""MR/PR 真实创建（优化方案 3.7 / GAP-14，任务 P5-01）。

能力与边界：

- **三平台**：GitLab（含自建实例，host 含 ``gitlab``）→ Merge Requests API；
  GitHub → Pull Requests API（labels 经 issues API 追加，失败不阻塞 PR 创建）；
  Gitee → Pull Requests API；
- **零新依赖**：stdlib ``urllib`` 直连 REST（与 release.ArgoCDPublisher 同风格），
  Temporal 沙箱内的 activity 导入本模块无额外依赖负担；
- **降级不中断**：未配置（repo_url / token 缺失，如演示环境）→ 返回确定性桩结果
  （``mode=recorded``，既有演示行为零变化）；真实调用失败 → 桩 + ``mode=degraded``
  + ``error`` 原因，流程照常进入闸门 2（人工审批仍可依据测试报告与告警上下文决策）；
- **描述自足**：MR 描述内嵌补丁 diff、测试报告与回滚预案，审批者一站式查看。

环境变量：
    AIOPS_GIT_TOKEN    Git 平台访问令牌（生产经 K8s Secret 注入；对应策略 token_secret 名称）

策略段（可选，缺省不启用，见 release-gate-policy.yaml）::

    git:
      repo_url: "https://gitlab.example.com/ops/demo-app"
      token_secret: "aiops-git-token"   # K8s Secret 名称（部署侧引用，运行时读取 AIOPS_GIT_TOKEN）
      default_branch: "main"
      mr_labels: [aiops, auto-fix]
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from .models import Patch, TestReport

log = logging.getLogger("aiops.git")

__all__ = [
    "GitIntegrationError",
    "MergeRequestCreator",
    "build_stub_mr",
    "create_mr_for_patch",
    "render_description",
    "render_title",
]


class GitIntegrationError(RuntimeError):
    """Git 平台调用失败（地址非法 / 网络异常 / 平台返回非 2xx）。"""


def _stable_id(text: str, mod: int) -> int:
    """桩用：由内容派生的稳定编号（避免依赖 Python 随机化 hash）。"""
    return sum(ord(ch) for ch in text) % mod


def _detect_provider(repo_url: str) -> str:
    """按仓库地址识别平台：gitlab / github / gitee；不支持时抛错（优化方案 3.7）。"""
    host = (urllib.parse.urlparse(repo_url).hostname or "").lower()
    for provider in ("gitlab", "github", "gitee"):
        if provider in host:
            return provider
    raise GitIntegrationError(f"不支持的 Git 平台: {repo_url}")


def _base_url(repo_url: str) -> str:
    """API 根地址（保留 scheme / host / port，兼容自建实例与自定义端口）。"""
    parsed = urllib.parse.urlparse(repo_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise GitIntegrationError(f"仓库地址非法: {repo_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _repo_path(repo_url: str) -> str:
    """仓库路径（去首尾斜杠与 .git 后缀）。"""
    path = urllib.parse.urlparse(repo_url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        raise GitIntegrationError(f"无法从地址提取仓库路径: {repo_url}")
    return path


def _owner_repo(repo_url: str) -> tuple[str, str]:
    """GitHub / Gitee 的 owner/repo 提取。"""
    parts = [p for p in _repo_path(repo_url).split("/") if p]
    if len(parts) < 2:
        raise GitIntegrationError(f"无法从地址提取 owner/repo: {repo_url}")
    return parts[0], "/".join(parts[1:])


def _gitlab_project_path(repo_url: str) -> str:
    """GitLab 项目路径（支持子群组）：``ops/team/demo-app`` → URL 编码串。"""
    return urllib.parse.quote(_repo_path(repo_url), safe="")


def _dynamic_labels(patch: Patch) -> list[str]:
    """动态标签（保留既有演示口径：alert / model / confidence 三要素）。"""
    return [
        f"ai-fix:{patch.alert_id}",
        f"model:{patch.model_version}",
        f"confidence:{patch.confidence:.2f}",
    ]


def render_title(patch: Patch) -> str:
    """MR 标题：告警 + 补丁摘要（单行截断防超长）。"""
    summary = (patch.description or patch.patch_id).replace("\n", " ").strip()
    return f"[AIOps 自动修复] {patch.alert_id}: {summary[:80]}"


def render_description(patch: Patch, test_report: TestReport | None = None) -> str:
    """MR 描述：修复说明 / 补丁 diff / 沙箱测试报告 / 回滚预案。"""
    from . import notify  # 延迟导入：回滚预案文案单一信源

    lines = [
        "## AIOps 自动修复",
        "",
        f"- 告警：`{patch.alert_id}`",
        f"- 补丁：`{patch.patch_id}`（风险 `{patch.risk}` / 置信度 `{patch.confidence:.2f}` / 模型 `{patch.model_version}`）",
        f"- 改动文件：{', '.join(f'`{f}`' for f in patch.files) or '（无）'}",
        "",
        "### 修复说明",
        patch.description or "（无）",
        "",
        "### 补丁 diff",
        "```diff",
        patch.diff.rstrip(),
        "```",
    ]
    if test_report is not None:
        lines += [
            "",
            "### 沙箱测试报告",
            f"- 结果：{'✅ 通过' if test_report.passed else '❌ 未通过'}",
            f"- 单元测试：{test_report.unit_tests or '-'}",
            f"- 回归测试：{test_report.regression_tests or '-'}",
            f"- SAST：{test_report.sast or '-'}",
        ]
        if test_report.details:
            lines.append(f"- 详情：{test_report.details}")
    lines += ["", "### 回滚预案", notify.ROLLBACK_PLAN]
    return "\n".join(lines)


def build_stub_mr(patch: Patch, *, mode: str = "recorded", error: str | None = None) -> dict:
    """确定性桩结果：未配置（recorded）与调用失败（degraded）共用，结构包含真实分支同款字段。"""
    return {
        "mr_id": f"!{_stable_id(patch.patch_id, 9000) + 1000}",
        "url": f"https://git.example.com/ops/{patch.alert_id}/-/merge_requests/demo",
        "labels": _dynamic_labels(patch),
        "provider": "stub",
        "mode": mode,
        "error": error,
    }


class MergeRequestCreator:
    """真实 MR/PR 创建：GitLab / GitHub / Gitee（优化方案 3.7）。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 通用入口
    # ------------------------------------------------------------------

    def create(
        self,
        patch: Patch,
        *,
        repo_url: str,
        token: str,
        target_branch: str = "main",
        labels: list[str] | None = None,
        test_report: TestReport | None = None,
        source_branch: str | None = None,
    ) -> dict:
        """创建 MR/PR；成功返回统一结构（``mode=live``），失败抛 :class:`GitIntegrationError`。

        源分支约定 ``aiops/fix-<patch_id>``（由外部 CI / 修复侧推送后再建 MR；
        本模块只负责建 MR，不推送代码）。
        """
        provider = _detect_provider(repo_url)
        source = source_branch or f"aiops/fix-{patch.patch_id}"
        all_labels = list(labels or []) + _dynamic_labels(patch)
        title = render_title(patch)
        description = render_description(patch, test_report)

        if provider == "gitlab":
            result = self._create_gitlab_mr(
                repo_url, source=source, target=target_branch,
                title=title, description=description, labels=all_labels, token=token,
            )
        elif provider == "github":
            result = self._create_github_pr(
                repo_url, source=source, target=target_branch,
                title=title, description=description, labels=all_labels, token=token,
            )
        else:
            result = self._create_gitee_pr(
                repo_url, source=source, target=target_branch,
                title=title, description=description, token=token,
            )
        result.update(
            {
                "provider": provider,
                "mode": "live",
                "error": None,
                "labels": all_labels,
                "source_branch": source,
                "target_branch": target_branch,
            }
        )
        return result

    # ------------------------------------------------------------------
    # 平台实现
    # ------------------------------------------------------------------

    def _create_gitlab_mr(
        self, repo_url: str, *, source: str, target: str, title: str,
        description: str, labels: list[str], token: str,
    ) -> dict:
        project = _gitlab_project_path(repo_url)
        data = self._request(
            f"{_base_url(repo_url)}/api/v4/projects/{project}/merge_requests",
            payload={
                "source_branch": source,
                "target_branch": target,
                "title": title,
                "description": description,
                "labels": ",".join(labels),
            },
            headers={"PRIVATE-TOKEN": token},
        )
        return {"mr_id": f"!{data.get('iid')}", "url": str(data.get("web_url") or "")}

    def _create_github_pr(
        self, repo_url: str, *, source: str, target: str, title: str,
        description: str, labels: list[str], token: str,
    ) -> dict:
        owner, repo = _owner_repo(repo_url)
        api = self._github_api(repo_url)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = self._request(
            f"{api}/repos/{owner}/{repo}/pulls",
            payload={"title": title, "head": source, "base": target, "body": description},
            headers=headers,
        )
        number = data.get("number")
        if labels and number:
            # labels 非关键路径：补挂失败仅告警，不影响 PR 创建结果
            try:
                self._request(
                    f"{api}/repos/{owner}/{repo}/issues/{number}/labels",
                    payload={"labels": labels},
                    headers=headers,
                )
            except GitIntegrationError as exc:
                log.warning("[git] GitHub PR 标签补挂失败（不影响创建）: %s", exc)
        return {"mr_id": f"#{number}", "url": str(data.get("html_url") or "")}

    def _create_gitee_pr(
        self, repo_url: str, *, source: str, target: str, title: str,
        description: str, token: str,
    ) -> dict:
        owner, repo = _owner_repo(repo_url)
        access_token = urllib.parse.quote(token, safe="")
        data = self._request(
            f"{_base_url(repo_url)}/api/v5/repos/{owner}/{repo}/pulls?access_token={access_token}",
            payload={"title": title, "head": source, "base": target, "body": description},
        )
        return {"mr_id": f"#{data.get('number')}", "url": str(data.get("html_url") or "")}

    @staticmethod
    def _github_api(repo_url: str) -> str:
        """GitHub API 根：公有云走 api.github.com，GitHub Enterprise 走 /api/v3。"""
        base = _base_url(repo_url)
        host = (urllib.parse.urlparse(base).hostname or "").lower()
        if host in ("github.com", "www.github.com"):
            return "https://api.github.com"
        return f"{base}/api/v3"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self, url: str, *, payload: dict | None = None, headers: dict | None = None
    ) -> dict:
        """极简 JSON HTTP POST（非 2xx / 网络错误统一转 :class:`GitIntegrationError`）。"""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            # 地址来自策略配置的仓库 URL（scheme 已在 _base_url 校验为 http/https）
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise GitIntegrationError(f"HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise GitIntegrationError(f"网络错误: {exc}") from exc
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise GitIntegrationError(f"响应非 JSON: {body[:200]}") from exc


# ----------------------------------------------------------------------
# Activity 入口：策略/环境读取 + 降级编排（流程永不因 MR 失败而中断）
# ----------------------------------------------------------------------


def create_mr_for_patch(patch: Patch, test_report: TestReport) -> dict:
    """按策略 git 段与 ``AIOPS_GIT_TOKEN`` 创建 MR；未配置或失败时返回桩结果。

    三种返回语义（``mode`` 字段）：

    - ``live``     真实创建成功（GitLab / GitHub / Gitee）；
    - ``recorded`` 未配置 repo_url / token（演示或尚未接入 Git 平台）；
    - ``degraded`` 已配置但调用失败（原因记录在 ``error``，日志告警，流程继续）。
    """
    try:
        from .config import load_policy  # 延迟导入：管理面依赖不出现在沙箱活动顶层

        git_cfg = load_policy().git
    except Exception as exc:  # noqa: BLE001 - 策略缺失/非法不阻断流程
        return build_stub_mr(patch, mode="degraded", error=f"策略加载失败: {exc}")

    token = os.environ.get("AIOPS_GIT_TOKEN", "")
    if not git_cfg.repo_url or not token:
        return build_stub_mr(patch, mode="recorded")

    try:
        return MergeRequestCreator().create(
            patch,
            repo_url=git_cfg.repo_url,
            token=token,
            target_branch=git_cfg.default_branch,
            labels=list(git_cfg.mr_labels),
            test_report=test_report,
        )
    except Exception as exc:  # noqa: BLE001 - 真实创建失败降级，闸门 2 人工审批兜底
        return build_stub_mr(patch, mode="degraded", error=str(exc))
