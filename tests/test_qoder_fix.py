"""Qoder 修复提供者（接入层）单测：不依赖真实 Qoder CLI（用 tests/fakes/fake_qodercli.sh 驱动）。

覆盖（对齐《Qoder 修复引擎接入设计》第 11 节）：
    - CLI 正常产出改动 → validated / provider=qoder / model_version 前缀 / diff 可应用可编译；
    - CLI 缺失 / 非零退出 / 空 diff / 超时 / 非法改动 → 安全侧降级为确定性兜底补丁；
    - 默认提供者为 ollama（零行为变更）；
    - 演示分支短路优先于提供者分发；
    - 隔离性：真实 demo-app/ 不被改动，工作区落在 AIOPS_QODER_ROOT；
    - 留痕、鉴权转写、prompt 约束。

运行：
    .venv/bin/python -m unittest tests.test_qoder_fix -v
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiops_agent import code_rag, fix_agent, qoder_fix
from aiops_agent.models import Alert, RootCause

FAKE_CLI = Path(__file__).resolve().parent / "fakes" / "fake_qodercli.sh"

ROOT_CAUSE = RootCause(
    error_type="NullPointerException",
    suspect_files=["com/example/service/OrderService.java"],
    confidence=0.95,
    summary="OrderService.submit 中 coupon 未做空值防护。",
)
ALERT = Alert(alert_id="q-1", service="order", description="错误日志突增，已触发自动根因分析")

_ENV_KEYS = (
    "AIOPS_FIX_PROVIDER",
    "AIOPS_QODER_BIN",
    "AIOPS_QODER_ROOT",
    "AIOPS_QODER_TIMEOUT",
    "AIOPS_QODER_MAX_TURNS",
    "AIOPS_QODER_MODEL",
    "AIOPS_QODER_TOKEN",
    "FAKE_QODER_MODE",
    "FAKE_QODER_TARGET",
    "QODER_PERSONAL_ACCESS_TOKEN",
)


class QoderTestBase(unittest.TestCase):
    """隔离环境变量与临时工作区根目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(
            {
                "AIOPS_QODER_BIN": str(FAKE_CLI),
                "AIOPS_QODER_ROOT": str(self.root / "qoder"),
            }
        )


class TestQoderAvailable(QoderTestBase):
    def test_reports_version(self) -> None:
        ok, version = qoder_fix.qoder_available(str(FAKE_CLI))
        self.assertTrue(ok, version)
        self.assertIn("9.9.9", version)

    def test_missing_reports_reason(self) -> None:
        ok, reason = qoder_fix.qoder_available("/nonexistent/definitely-not-qoder")
        self.assertFalse(ok)
        self.assertIn("未找到", reason)


class TestBuildPrompt(unittest.TestCase):
    def test_contains_target_and_guardrails(self) -> None:
        prompt = qoder_fix.build_qoder_prompt(ALERT, ROOT_CAUSE, [], "order_service.py")
        self.assertIn("order_service.py", prompt)
        self.assertIn("禁止新增文件", prompt)
        self.assertIn("重构无关代码", prompt)
        self.assertIn("不要输出 diff", prompt)
        self.assertIn("NullPointerException", prompt)

    def test_retry_hint_only_when_attempt_positive(self) -> None:
        self.assertEqual(qoder_fix._retry_hint(0), "")
        self.assertIn("防护位置", qoder_fix._retry_hint(1))


class TestProviderSwitch(QoderTestBase):
    def test_default_provider_is_ollama(self) -> None:
        """未设 AIOPS_FIX_PROVIDER 时走 Ollama，行为零变更。"""
        os.environ.pop("AIOPS_FIX_PROVIDER", None)
        response = json.dumps(
            {"diff": _real_diff_for_order_service(), "description": "增加 coupon 空值防护", "risk": "低"},
            ensure_ascii=False,
        )
        with mock.patch("aiops_agent.fix_agent.call_ollama", return_value=response) as call:
            patch, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        call.assert_called_once()
        self.assertEqual(meta["provider"], "ollama")
        self.assertTrue(meta["validated"])
        self.assertEqual(patch.model_version, fix_agent.DEFAULT_MODEL)

    def test_unknown_provider_falls_back_to_ollama(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "not-a-provider"
        self.assertEqual(fix_agent.resolve_provider(), "ollama")


class TestQoderRunFix(QoderTestBase):
    def test_good_path_validated(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        patch, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["validated"], meta["reason"])
        self.assertFalse(meta["degraded"])
        self.assertEqual(meta["provider"], "qoder")
        self.assertEqual(patch.files, ["order_service.py"])
        self.assertTrue(patch.model_version.startswith("qoder-cli:"), patch.model_version)
        self.assertIn("if coupon else 0", patch.diff)
        self.assertEqual(patch.confidence, ROOT_CAUSE.confidence)

    def test_missing_cli_degrades(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        os.environ["AIOPS_QODER_BIN"] = "/nonexistent/definitely-not-qoder"
        patch, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["degraded"])
        self.assertFalse(meta["validated"])
        self.assertEqual(patch.model_version, fix_agent.STUB_MODEL_VERSION)
        self.assertIn("coupon", patch.diff)

    def test_cli_failure_degrades(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        os.environ["FAKE_QODER_MODE"] = "fail"
        _, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["degraded"])
        self.assertFalse(meta["validated"])

    def test_no_change_degrades(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        os.environ["FAKE_QODER_MODE"] = "noop"
        _, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["degraded"])
        self.assertIn("未对", meta["reason"])

    def test_timeout_degrades(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        os.environ["FAKE_QODER_MODE"] = "timeout"
        os.environ["AIOPS_QODER_TIMEOUT"] = "1"
        _, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["degraded"])
        self.assertIn("超时", meta["reason"])

    def test_broken_edit_degrades_via_compile_check(self) -> None:
        """Qoder 产出语法错误改动 → 编译校验拦截 → 降级。"""
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        os.environ["FAKE_QODER_MODE"] = "broken"
        _, meta = fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["degraded"])
        self.assertIn("编译校验失败", meta["reason"])

    def test_demo_branch_takes_precedence(self) -> None:
        """演示分支短路先于提供者分发：不创建 Qoder 工作区。"""
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        alert = Alert(alert_id="q-2", service="order", description="test-fail 演示")
        _, meta = fix_agent.run_fix(alert, ROOT_CAUSE, references=[], attempt=0)
        self.assertTrue(meta["stub"])
        self.assertFalse(meta["degraded"])
        self.assertFalse((self.root / "qoder").exists())

    def test_workspace_isolation_leaves_repo_untouched(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        target = code_rag.DEFAULT_REPO_DIR / "order_service.py"
        before = target.read_text(encoding="utf-8")
        fix_agent.run_fix(ALERT, ROOT_CAUSE, references=[], attempt=0)
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertTrue((self.root / "qoder" / "p-q-1-r0").is_dir())


class TestProposePatch(QoderTestBase):
    def _propose(self, attempt: int = 0):
        return qoder_fix.propose_patch(
            ALERT,
            ROOT_CAUSE,
            [],
            attempt,
            "order_service.py",
            repo_dir=code_rag.DEFAULT_REPO_DIR,
            workspace_root=self.root / "qoder",
        )

    def test_returns_diff_and_meta(self) -> None:
        diff, description, risk, meta = self._propose()
        self.assertIn("if coupon else 0", diff)
        self.assertTrue(description)
        self.assertTrue(risk)
        self.assertEqual(meta["provider"], "qoder")
        self.assertTrue(meta["cli_version"].startswith("fake-qodercli"))
        self.assertEqual(meta["model_version"], f"qoder-cli:{meta['cli_version']}")

    def test_writes_run_record_without_secret(self) -> None:
        os.environ["AIOPS_QODER_TOKEN"] = "super-secret-token"
        _, _, _, meta = self._propose()
        record = Path(meta["run_record"])
        self.assertTrue(record.is_file())
        data = json.loads(record.read_text(encoding="utf-8"))
        self.assertTrue(data["changed"])
        self.assertEqual(data["target_file"], "order_service.py")
        self.assertEqual(data["auth_mode"], "pat")
        self.assertNotIn("super-secret-token", json.dumps(data, ensure_ascii=False))

    def test_noop_raises_qoder_error(self) -> None:
        os.environ["FAKE_QODER_MODE"] = "noop"
        with self.assertRaises(qoder_fix.QoderFixError):
            self._propose()

    def test_missing_cli_raises_qoder_error(self) -> None:
        os.environ["AIOPS_QODER_BIN"] = "/nonexistent/qoder-xyz"
        with self.assertRaises(qoder_fix.QoderFixError):
            self._propose()


class TestEnvSanitization(unittest.TestCase):
    """回归防护：继承自 Qoder 进程的 Agent-SDK 变量必须被剔除。

    实测背景：若 worker 运行在 Qoder 进程树下，QODER_AGENT_SDK_ENTRYPOINT=sdk-ts 会被继承，
    qodercli 误入 SDK 模式并报 sdk_invalid_args，导致修复环节全部失败。
    """

    def test_strips_sdk_entrypoint_and_worker_vars(self) -> None:
        base = {
            "QODER_AGENT_SDK_ENTRYPOINT": "sdk-ts",
            "QODER_AGENT_SDK_VERSION": "1.0.49",
            "QODER_WORKER_RUNTIME_ASSET_ROOT": "/x/_worker",
            "QODER_WORKER_CWD": "/x",
            "QODER_SDK_AUTH_PAYLOAD_FILE": "/x/payload.json",
            "QODERCLI_RUNTIME_PACKAGING": "worker_mjs",
            "QODER_SESSION_TYPE": "app",
            "QODER_MCP_LAZY": "1",
            "FEATURE_FLAGS": "{...}",
            "PATH": "/usr/bin",
        }
        cleaned = qoder_fix.sanitize_env(base)
        for gone in (
            "QODER_AGENT_SDK_ENTRYPOINT",
            "QODER_AGENT_SDK_VERSION",
            "QODER_WORKER_RUNTIME_ASSET_ROOT",
            "QODER_WORKER_CWD",
            "QODER_SDK_AUTH_PAYLOAD_FILE",
            "QODERCLI_RUNTIME_PACKAGING",
            "QODER_SESSION_TYPE",
            "QODER_MCP_LAZY",
            "FEATURE_FLAGS",
        ):
            self.assertNotIn(gone, cleaned, gone)
        self.assertEqual(cleaned["PATH"], "/usr/bin")

    def test_keeps_auth_and_config_vars(self) -> None:
        base = {
            "QODER_PERSONAL_ACCESS_TOKEN": "tok",
            "QODER_CONFIG_DIR": "/Users/x/.qoder",
            "QODERCN_CONFIG_DIR": "/Users/x/.qoder",
            "QODER_PRODUCT_ID": "qoder",
            "HOME": "/Users/x",
        }
        cleaned = qoder_fix.sanitize_env(base)
        self.assertEqual(cleaned, base)


class TestDefaultModel(QoderTestBase):
    """默认修复模型 = DeepSeek-Flash（注意 Qoder 模型 ID 区分大小写）。"""

    def test_default_model_is_deepseek_flash(self) -> None:
        os.environ.pop("AIOPS_QODER_MODEL", None)
        result = qoder_fix.run_qoder_cli(self.root, "x", exe=str(FAKE_CLI), timeout=10)
        cmd = result["command"]
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "DeepSeek-Flash")

    def test_env_overrides_default_model(self) -> None:
        os.environ["AIOPS_QODER_MODEL"] = "Qwen3.8-Max"
        result = qoder_fix.run_qoder_cli(self.root, "x", exe=str(FAKE_CLI), timeout=10)
        cmd = result["command"]
        self.assertEqual(cmd[cmd.index("-m") + 1], "Qwen3.8-Max")

    def test_resolve_model_precedence(self) -> None:
        self.assertEqual(qoder_fix.resolve_model(), qoder_fix.DEFAULT_QODER_MODEL)
        os.environ["AIOPS_QODER_MODEL"] = "Kimi-K3"
        self.assertEqual(qoder_fix.resolve_model(), "Kimi-K3")
        self.assertEqual(qoder_fix.resolve_model("GLM-5.3"), "GLM-5.3")  # 显式参数最高优先


class TestModelFallbackWarning(QoderTestBase):
    """回归防护：Qoder 对无效模型名会静默回退 auto（退出码 0、结果看似成功）。

    必须把 stderr 警告提升到 meta/留痕，否则会误以为指定模型已生效。
    """

    def test_silent_model_fallback_is_surfaced(self) -> None:
        os.environ["AIOPS_FIX_PROVIDER"] = "qoder"
        os.environ["FAKE_QODER_MODE"] = "warn_model"
        os.environ["AIOPS_QODER_MODEL"] = "deepseek-flash"  # 故意用无效的小写 ID
        diff, _desc, _risk, meta = qoder_fix.propose_patch(
            ALERT,
            ROOT_CAUSE,
            [],
            0,
            "order_service.py",
            repo_dir=code_rag.DEFAULT_REPO_DIR,
            workspace_root=self.root / "qoder",
        )
        self.assertTrue(diff.strip())  # 改动仍然产出（这正是危险之处）
        self.assertIn("is not available right now", meta["model_warning"])
        self.assertIn("模型回退警告", meta["reason"])
        record = json.loads(Path(meta["run_record"]).read_text(encoding="utf-8"))
        self.assertEqual(record["model"], "deepseek-flash")
        self.assertIn("is not available right now", record["model_warning"])

    def test_detect_model_warning_helper(self) -> None:
        self.assertEqual(qoder_fix._detect_model_warning(""), "")
        self.assertEqual(qoder_fix._detect_model_warning("all good"), "")
        warn = 'Model "x" is not available right now; using "auto" instead.'
        self.assertEqual(qoder_fix._detect_model_warning(warn), warn)


class TestActivityTimeoutInvariant(unittest.TestCase):
    """回归防护：修复活动的 Temporal 超时必须 > Qoder 子进程超时。

    实测教训：generate_patch 原用默认 60s，真实 Qoder 修复（约 50s 起）超时后被 Temporal
    直接取消，异常一路冒泡为活动失败并重试，**无法**降级为兜底补丁。子进程超时先触发时，
    propose_patch 抛 QoderFixError → run_fix 捕获 → 回落兜底补丁，活动正常成功。
    """

    def test_fix_activity_timeout_exceeds_provider_subprocess_timeout(self) -> None:
        from aiops_agent import workflows

        self.assertGreater(
            workflows._FIX_ACTIVITY_TIMEOUT.total_seconds(),
            float(qoder_fix.DEFAULT_TIMEOUT),
        )


class TestRunQoderCli(QoderTestBase):
    def test_token_transcribed_to_official_var(self) -> None:
        os.environ["AIOPS_QODER_TOKEN"] = "tok-123"
        result = qoder_fix.run_qoder_cli(self.root, "x", exe=str(FAKE_CLI), timeout=10)
        self.assertEqual(result["auth_mode"], "pat")
        # 命令留痕不得包含 token 明文
        self.assertNotIn("tok-123", " ".join(result["command"]))

    def test_command_uses_safe_flags(self) -> None:
        result = qoder_fix.run_qoder_cli(self.root, "x", exe=str(FAKE_CLI), timeout=10)
        cmd = result["command"]
        self.assertIn("-p", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("accept_edits", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--disallowed-tools", cmd)
        # 绝不使用 bypass/yolo
        self.assertNotIn("--yolo", cmd)
        self.assertNotIn("bypass_permissions", cmd)

    def test_subprocess_detached_from_controlling_tty(self) -> None:
        """回归：无头调用必须脱离控制终端与终端 stdin（防 SIGTTIN 挂起）。

        实测教训：worker 由后台作业启动（nohup … &，进程组非前台）时，qodercli 启动期
        读取控制终端会收到 SIGTTIN 被内核停止（ps STAT=T）且永不退出，generate_patch
        活动挂死；start_new_session + DEVNULL stdin 使子进程无控制终端可读，消除该状态。
        """
        with mock.patch("subprocess.run", wraps=subprocess.run) as run:
            qoder_fix.run_qoder_cli(self.root, "x", exe=str(FAKE_CLI), timeout=10)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertTrue(kwargs.get("start_new_session"))


def _real_diff_for_order_service() -> str:
    """按 demo-app/order_service.py 当前内容动态构造行号正确的 diff（与既有测试口径一致）。"""
    content = (code_rag.DEFAULT_REPO_DIR / "order_service.py").read_text(encoding="utf-8")
    lines = content.splitlines()
    anchor = next(i for i, line in enumerate(lines) if "discount = coupon" in line)
    hunk = lines[anchor - 3 : anchor + 3]
    start = anchor - 2
    body = "".join(f" {line}\n" for line in hunk[:3])
    body += "+        if coupon is None:\n"
    body += '+            raise ValueError("无效优惠券")\n'
    body += "".join(f" {line}\n" for line in hunk[3:])
    return f"--- a/order_service.py\n+++ b/order_service.py\n@@ -{start},6 +{start},9 @@\n{body}"


if __name__ == "__main__":
    unittest.main()
