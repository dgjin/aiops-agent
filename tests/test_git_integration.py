"""P5-01：MR 真实创建（git_integration）单测。

覆盖：平台识别 / 描述渲染 / 三平台 API 调用（mock urllib）/ 错误语义 /
未配置桩（recorded）/ 失败降级（degraded）/ activity 委托。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from aiops_agent import activities, git_integration
from aiops_agent.config import Policy
from aiops_agent.models import Patch, TestReport

REPORT = TestReport(patch_id="p-wp5-1", passed=True, unit_tests="5/5", sast="通过")


def _patch(alert_id: str = "wp5-a1") -> Patch:
    return Patch(
        patch_id="p-wp5-1",
        alert_id=alert_id,
        files=["order_service.py"],
        diff="--- a/order_service.py\n+++ b/order_service.py\n@@ -1 +1 @@\n-x = None\n+x = guard()\n",
        description="空指针防护",
        risk="low",
        model_version="qwen3:8b",
        confidence=0.92,
    )


class _Resp:
    def __init__(self, payload: dict, status: int = 201):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _http_error(code: int = 401, body: bytes = b'{"message":"unauthorized"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://git.example/api", code, "err", None, io.BytesIO(body))


class DetectProviderTest(unittest.TestCase):
    def test_gitlab_cloud_and_self_hosted(self) -> None:
        self.assertEqual(git_integration._detect_provider("https://gitlab.com/o/r"), "gitlab")
        self.assertEqual(
            git_integration._detect_provider("https://gitlab.example.com/ops/demo-app"), "gitlab"
        )

    def test_github_and_gitee(self) -> None:
        self.assertEqual(git_integration._detect_provider("https://github.com/o/r.git"), "github")
        self.assertEqual(git_integration._detect_provider("https://gitee.com/o/r"), "gitee")

    def test_unsupported_raises(self) -> None:
        with self.assertRaises(git_integration.GitIntegrationError):
            git_integration._detect_provider("https://git.example.com/ops/demo-app")


class RenderTest(unittest.TestCase):
    def test_description_contains_diff_report_and_rollback(self) -> None:
        text = git_integration.render_description(_patch(), REPORT)
        self.assertIn("补丁 diff", text)
        self.assertIn("+x = guard()", text)
        self.assertIn("沙箱测试报告", text)
        self.assertIn("5/5", text)
        self.assertIn("回滚预案", text)
        self.assertIn("金丝雀劣化自动回滚", text)

    def test_description_without_report(self) -> None:
        text = git_integration.render_description(_patch(), None)
        self.assertNotIn("沙箱测试报告", text)
        self.assertIn("回滚预案", text)

    def test_title_single_line(self) -> None:
        patch = _patch()
        patch.description = "第一行\n第二行"
        title = git_integration.render_title(patch)
        self.assertTrue(title.startswith("[AIOps 自动修复] wp5-a1:"))
        self.assertNotIn("\n", title)


class GitLabCreateTest(unittest.TestCase):
    def test_create_success_payload_and_headers(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen",
            return_value=_Resp(
                {"iid": 42, "web_url": "https://gitlab.example.com/ops/demo-app/-/merge_requests/42"}
            ),
        ) as urlopen:
            result = creator.create(
                _patch(),
                repo_url="https://gitlab.example.com/ops/demo-app",
                token="glpat-x",
                target_branch="main",
                labels=["aiops", "auto-fix"],
                test_report=REPORT,
            )
        self.assertEqual(result["mr_id"], "!42")
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["provider"], "gitlab")
        self.assertEqual(result["source_branch"], "aiops/fix-p-wp5-1")
        self.assertEqual(
            result["url"], "https://gitlab.example.com/ops/demo-app/-/merge_requests/42"
        )
        request = urlopen.call_args.args[0]
        self.assertIn("/api/v4/projects/ops%2Fdemo-app/merge_requests", request.full_url)
        self.assertEqual(request.get_header("Private-token"), "glpat-x")
        body = json.loads(request.data)
        self.assertEqual(body["source_branch"], "aiops/fix-p-wp5-1")
        self.assertEqual(body["target_branch"], "main")
        self.assertEqual(body["labels"], "aiops,auto-fix,ai-fix:wp5-a1,model:qwen3:8b,confidence:0.92")


class GitHubCreateTest(unittest.TestCase):
    def test_create_success_and_labels_append(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        responses = [
            _Resp({"number": 7, "html_url": "https://github.com/o/r/pull/7"}),
            _Resp({"labels": []}, status=200),
        ]
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen", side_effect=responses
        ) as urlopen:
            result = creator.create(
                _patch(), repo_url="https://github.com/o/r", token="ghp-x", labels=["aiops"]
            )
        self.assertEqual(result["mr_id"], "#7")
        self.assertEqual(result["provider"], "github")
        first = urlopen.call_args_list[0].args[0]
        self.assertEqual(first.full_url, "https://api.github.com/repos/o/r/pulls")
        self.assertEqual(json.loads(first.data)["head"], "aiops/fix-p-wp5-1")
        self.assertEqual(first.get_header("Authorization"), "Bearer ghp-x")
        second = urlopen.call_args_list[1].args[0]
        self.assertIn("/repos/o/r/issues/7/labels", second.full_url)

    def test_labels_failure_does_not_block_pr(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        responses = [
            _Resp({"number": 7, "html_url": "https://github.com/o/r/pull/7"}),
            _http_error(422, b'{"message":"label missing"}'),
        ]
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen", side_effect=responses
        ):
            result = creator.create(
                _patch(), repo_url="https://github.com/o/r", token="ghp-x", labels=["aiops"]
            )
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["mr_id"], "#7")

    def test_enterprise_host_uses_api_v3(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen",
            return_value=_Resp({"number": 1, "html_url": "https://github.example.com/o/r/pull/1"}),
        ) as urlopen:
            creator.create(
                _patch(), repo_url="https://github.example.com/o/r", token="t", labels=[]
            )
        first = urlopen.call_args_list[0].args[0]
        self.assertEqual(
            first.full_url, "https://github.example.com/api/v3/repos/o/r/pulls"
        )


class GiteeCreateTest(unittest.TestCase):
    def test_create_success_access_token_in_query(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen",
            return_value=_Resp({"number": 3, "html_url": "https://gitee.com/o/r/pulls/3"}),
        ) as urlopen:
            result = creator.create(
                _patch(), repo_url="https://gitee.com/o/r", token="gitee-x", labels=[]
            )
        self.assertEqual(result["mr_id"], "#3")
        self.assertEqual(result["provider"], "gitee")
        request = urlopen.call_args.args[0]
        self.assertIn("/api/v5/repos/o/r/pulls?access_token=gitee-x", request.full_url)
        self.assertEqual(json.loads(request.data)["base"], "main")


class ErrorTest(unittest.TestCase):
    def test_http_error_raises(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen", side_effect=_http_error(401)
        ):
            with self.assertRaises(git_integration.GitIntegrationError) as ctx:
                creator.create(_patch(), repo_url="https://gitlab.example.com/o/r", token="t")
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_network_error_raises(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen",
            side_effect=OSError("conn refused"),
        ):
            with self.assertRaises(git_integration.GitIntegrationError):
                creator.create(_patch(), repo_url="https://gitlab.example.com/o/r", token="t")

    def test_non_json_response_raises(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)

        class _BadResp(_Resp):
            def __init__(self):
                super().__init__({}, status=200)
                self._body = b"<html>502</html>"

        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen", return_value=_BadResp()
        ):
            with self.assertRaises(git_integration.GitIntegrationError):
                creator.create(_patch(), repo_url="https://gitlab.example.com/o/r", token="t")

    def test_unsupported_repo_raises_before_network(self) -> None:
        creator = git_integration.MergeRequestCreator(timeout=1)
        with mock.patch(
            "aiops_agent.git_integration.urllib.request.urlopen"
        ) as urlopen:
            with self.assertRaises(git_integration.GitIntegrationError):
                creator.create(_patch(), repo_url="https://git.example.com/o/r", token="t")
        urlopen.assert_not_called()


class CreateMrForPatchTest(unittest.TestCase):
    """activity 入口：策略/环境读取 + 降级编排。"""

    def test_no_config_returns_recorded_stub(self) -> None:
        with mock.patch("aiops_agent.config.load_policy", return_value=Policy()), mock.patch.dict(
            os.environ, {"AIOPS_GIT_TOKEN": ""}
        ):
            result = git_integration.create_mr_for_patch(_patch(), REPORT)
        self.assertEqual(result["mode"], "recorded")
        self.assertIsNone(result["error"])
        self.assertTrue(result["mr_id"].startswith("!"))
        self.assertEqual(
            result["labels"],
            ["ai-fix:wp5-a1", "model:qwen3:8b", "confidence:0.92"],
        )

    def test_token_missing_still_recorded(self) -> None:
        policy = Policy(git={"repo_url": "https://gitlab.example.com/ops/demo-app"})
        with mock.patch("aiops_agent.config.load_policy", return_value=policy), mock.patch.dict(
            os.environ, {"AIOPS_GIT_TOKEN": ""}
        ):
            result = git_integration.create_mr_for_patch(_patch(), REPORT)
        self.assertEqual(result["mode"], "recorded")

    def test_configured_invokes_creator_with_policy_fields(self) -> None:
        policy = Policy(
            git={
                "repo_url": "https://gitlab.example.com/ops/demo-app",
                "default_branch": "release",
                "mr_labels": ["ops"],
            }
        )
        live = {
            "mr_id": "!9",
            "url": "https://gitlab.example.com/ops/demo-app/-/merge_requests/9",
            "labels": ["ops", "ai-fix:wp5-a1"],
            "provider": "gitlab",
            "mode": "live",
            "error": None,
        }
        with mock.patch("aiops_agent.config.load_policy", return_value=policy), mock.patch.dict(
            os.environ, {"AIOPS_GIT_TOKEN": "tok"}
        ), mock.patch.object(
            git_integration.MergeRequestCreator, "create", return_value=live
        ) as create:
            result = git_integration.create_mr_for_patch(_patch(), REPORT)
        self.assertEqual(result["mode"], "live")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["repo_url"], "https://gitlab.example.com/ops/demo-app")
        self.assertEqual(kwargs["token"], "tok")
        self.assertEqual(kwargs["target_branch"], "release")
        self.assertEqual(kwargs["labels"], ["ops"])

    def test_create_failure_degrades(self) -> None:
        policy = Policy(git={"repo_url": "https://gitlab.example.com/ops/demo-app"})
        with mock.patch("aiops_agent.config.load_policy", return_value=policy), mock.patch.dict(
            os.environ, {"AIOPS_GIT_TOKEN": "tok"}
        ), mock.patch.object(
            git_integration.MergeRequestCreator,
            "create",
            side_effect=git_integration.GitIntegrationError("HTTP 500: boom"),
        ):
            result = git_integration.create_mr_for_patch(_patch(), REPORT)
        self.assertEqual(result["mode"], "degraded")
        self.assertIn("HTTP 500", result["error"])
        self.assertTrue(result["mr_id"])

    def test_policy_load_failure_degrades(self) -> None:
        with mock.patch(
            "aiops_agent.config.load_policy", side_effect=FileNotFoundError("no policy")
        ):
            result = git_integration.create_mr_for_patch(_patch(), REPORT)
        self.assertEqual(result["mode"], "degraded")
        self.assertIn("策略加载失败", result["error"])


class ActivityTest(unittest.TestCase):
    """activity 委托：demo 桩保持既有标签口径；mode 透传。"""

    def test_activity_recorded_stub_labels(self) -> None:
        with mock.patch("aiops_agent.config.load_policy", return_value=Policy()), mock.patch.dict(
            os.environ, {"AIOPS_GIT_TOKEN": ""}
        ):
            mr = asyncio.run(activities.create_merge_request(_patch(), REPORT))
        self.assertEqual(mr["mode"], "recorded")
        self.assertEqual(
            mr["labels"],
            ["ai-fix:wp5-a1", "model:qwen3:8b", "confidence:0.92"],
        )

    def test_activity_passes_through_live_result(self) -> None:
        live = {
            "mr_id": "!77",
            "url": "https://gitlab.example.com/ops/demo-app/-/merge_requests/77",
            "labels": ["aiops"],
            "provider": "gitlab",
            "mode": "live",
            "error": None,
        }
        with mock.patch.object(
            git_integration, "create_mr_for_patch", return_value=live
        ) as fn:
            mr = asyncio.run(activities.create_merge_request(_patch(), REPORT))
        self.assertEqual(mr, live)
        fn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
