"""D 批次模式/Temporal 专用测试：先 import tests，所有网络、worker、DB 均 mock。"""
import tests  # noqa: F401 - 必须先运行隔离钩子
import argparse
import ast
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from aiops_agent import config, mode
from aiops_agent import worker
from bff.temporal_gateway import TemporalGateway
import demo_cli

ROOT = Path(__file__).resolve().parents[1]


class StartupModeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="aiops-startup-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.policy = self.root / "policy.yaml"
        self.policy.write_text("release_gate:\n  shadow_mode: true\n")
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("isolated fixture, never executed")
        python.chmod(0o700)
        patch = mock.patch.dict(os.environ, {"AIOPS_MODE": "production",
            "AIOPS_DATA_ROOT": str(self.root / "prod"), "AIOPS_POLICY_PATH": str(self.policy),
            "AIOPS_DATABASE_URL": "mysql+pymysql://test:private-test-secret@invalid.invalid/test_only",
            "AIOPS_REDIS_URL": "redis://:private-test-secret@invalid.invalid/0"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        db = mock.patch("sqlalchemy.create_engine", side_effect=AssertionError("no DB access"))
        db.start()
        self.addCleanup(db.stop)

    def test_namespace_and_queue_defaults_in_both_modes(self):
        for value, namespace, queue in (("demo", "default", "aiops-tasks"),
                ("production", "aiops-production", "aiops-production-tasks")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"AIOPS_MODE": value}):
                self.assertEqual(mode.temporal_namespace(), namespace)
                self.assertEqual(mode.temporal_task_queue(), queue)

    def test_namespace_mismatch_rejected_in_both_aliases(self):
        for key in ("TEMPORAL_NAMESPACE", "AIOPS_TEMPORAL_NAMESPACE"):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: "default"}):
                with self.assertRaises(mode.ModeConfigError):
                    mode.temporal_namespace()

    def test_demo_cannot_use_production_namespace(self):
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo", "TEMPORAL_NAMESPACE": "aiops-production"}):
            with self.assertRaises(mode.ModeConfigError):
                mode.temporal_namespace()

    def test_queue_and_alias_conflict_rejected(self):
        for values in ({"TEMPORAL_TASK_QUEUE": "aiops-tasks"},
                {"TEMPORAL_NAMESPACE": "aiops-production", "AIOPS_TEMPORAL_NAMESPACE": "default"}):
            with self.subTest(values=values), mock.patch.dict(os.environ, values):
                with self.assertRaises(mode.ModeConfigError):
                    config.startup_snapshot(self.root)

    def test_snapshot_is_absolute_redacted_and_readonly(self):
        before = sorted(self.root.rglob("*"))
        snapshot = config.startup_snapshot(self.root)
        self.assertEqual(snapshot["policy_path"], str(self.policy))
        self.assertEqual(len(snapshot["policy_sha256"]), 64)
        self.assertTrue(snapshot["shadow_mode"])
        self.assertNotIn("private-test-secret", json.dumps(snapshot))
        self.assertEqual(snapshot["listeners"]["metrics"], {"host": "127.0.0.1", "port": 9091})
        self.assertEqual(before, sorted(self.root.rglob("*")))

    def test_missing_production_venv_rejected(self):
        with self.assertRaisesRegex(mode.ModeConfigError, "venv"):
            config.startup_snapshot(self.root / "other")

    def test_production_full_chain_policy_allowed(self):
        """放行侧：显式 shadow_mode: false 在 production 合法（全链路档）。"""
        self.policy.write_text("release_gate: {shadow_mode: false}")
        policy = config.load_policy()
        self.assertFalse(config.snapshot_for_workflow(policy)["shadow_mode"])

    def test_production_implicit_shadow_mode_rejected(self):
        """拦截侧：未显式声明 shadow_mode（含默认构造）仍拒绝，防静默漂移。"""
        self.policy.write_text("release_gate:\n  triage:\n    confidence_threshold: 0.9\n")
        with self.assertRaisesRegex(mode.ModeConfigError, "shadow_mode"):
            config.load_policy()
        with self.assertRaises(mode.ModeConfigError):
            config.snapshot_for_workflow(config.Policy())

    def test_policy_hash_pin_rejects_file_changes(self):
        first = config.load_policy()
        with mock.patch.dict(os.environ, {"AIOPS_POLICY_SHA256": first._sha256}):
            self.policy.write_text("release_gate: {shadow_mode: true}\n# changed\n")
            with self.assertRaisesRegex(mode.ModeConfigError, "hash"):
                config.load_policy()

    def test_loaded_policy_records_actual_source_and_immutable_hash(self):
        policy = config.load_policy(self.policy)
        snapshot = config.snapshot_for_workflow(policy)
        self.policy.write_text("release_gate: {shadow_mode: false}")
        again = config.snapshot_for_workflow(policy)
        self.assertEqual(snapshot, again)
        self.assertEqual(again["policy_source"], str(self.policy))
        self.assertEqual(again["temporal_namespace"], "aiops-production")
        self.assertEqual(again["temporal_task_queue"], "aiops-production-tasks")

    def test_bad_data_database_and_listener_configs(self):
        cases = [{"AIOPS_DATA_ROOT": "relative"}, {"AIOPS_DATA_ROOT": str(self.root / "data")},
                 {"AIOPS_DATABASE_URL": ""}, {"AIOPS_DATABASE_URL": "sqlite:///:memory:"},
                 {"AIOPS_REDIS_URL": ""}, {"AIOPS_BFF_HOST": "0.0.0.0"},
                 {"AIOPS_WEBHOOK_PORT": "bad"}, {"AIOPS_METRICS_PORT": "8600"},
                 {"TEMPORAL_ADDRESS": "user:password@host:7233"}, {"TEMPORAL_ADDRESS": "localhost"},
                 {"AIOPS_DATA_ROOT": str(self.policy)}]
        for values in cases:
            with self.subTest(values=values), mock.patch.dict(os.environ, values):
                with self.assertRaises(mode.ModeConfigError):
                    config.startup_snapshot(self.root)

    def test_demo_default_policy_not_injected_by_shell(self):
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}, clear=True):
            self.assertEqual(config.resolve_policy_path(), ROOT / "demo-policy.yaml")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.resolve_policy_path(), ROOT / "release-gate-policy.yaml")


class TemporalEntrypointsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"AIOPS_MODE": "production"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake = SimpleNamespace(namespace="aiops-production", workflow_service=SimpleNamespace(
            describe_namespace=mock.AsyncMock(return_value=SimpleNamespace(
                namespace_info=SimpleNamespace(name="aiops-production")))),
            start_workflow=mock.AsyncMock(return_value=SimpleNamespace(id="test-wf")))
        self.connect_patch = mock.patch("temporalio.client.Client.connect", new=mock.AsyncMock(return_value=self.fake))
        self.connect = self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)

    async def test_gateway_connections_and_real_rpc_with_cached_client(self):
        gateway = TemporalGateway()
        for _ in range(2):
            ok, elapsed, error = await gateway.ping()
            self.assertTrue(ok)
            self.assertGreaterEqual(elapsed, 0)
            self.assertIsNone(error)
        self.connect.assert_awaited_once_with("localhost:7233", namespace="aiops-production")
        self.assertEqual(self.fake.workflow_service.describe_namespace.await_count, 2)
        req = self.fake.workflow_service.describe_namespace.call_args.args[0]
        self.assertEqual(req.namespace, "aiops-production")

    async def test_ping_rejects_rpc_failure_and_masks_error(self):
        gateway = TemporalGateway()
        gateway._client = self.fake
        self.fake.workflow_service.describe_namespace.side_effect = RuntimeError("secret-password")
        ok, _, error = await gateway.ping()
        self.assertFalse(ok)
        self.assertNotIn("secret-password", error)

    async def test_ping_rejects_wrong_server_namespace(self):
        self.fake.workflow_service.describe_namespace.return_value.namespace_info.name = "default"
        self.assertFalse((await TemporalGateway().ping())[0])

    async def test_client_rejects_cached_wrong_namespace(self):
        gateway = TemporalGateway()
        self.fake.namespace = "default"
        gateway._client = self.fake
        with self.assertRaises(mode.ModeConfigError):
            await gateway.client()

    async def test_list_cache_cannot_cross_namespace(self):
        gateway = TemporalGateway()
        gateway._client = self.fake
        gateway._list_cache = (float("inf"), [{"from": "old-namespace"}])
        self.fake.namespace = "default"
        with self.assertRaises(mode.ModeConfigError):
            await gateway.list_flows()

    async def test_worker_wrong_interpreter_rejected_before_side_effects(self):
        with mock.patch.object(worker.sys, "prefix", "/not-the-repository-venv"), mock.patch.object(worker, "startup_snapshot") as preflight:
            with self.assertRaises(mode.ModeConfigError):
                await worker.main()
            preflight.assert_not_called()
        self.connect.assert_not_awaited()

    async def test_namespace_mismatch_before_connect(self):
        with mock.patch.dict(os.environ, {"TEMPORAL_NAMESPACE": "default"}):
            with self.assertRaises(mode.ModeConfigError):
                await TemporalGateway().client()
        self.connect.assert_not_awaited()

    async def test_fix_and_requirement_use_same_shadow_snapshot_and_queue(self):
        policy = config.Policy.model_validate({"release_gate": {"shadow_mode": True}})
        with mock.patch("aiops_agent.config.load_policy", return_value=policy):
            gateway = TemporalGateway()
            await gateway.start_flow("isolated-alert", "test-wf")
            await gateway.start_requirement_flow("isolated-task", "test-wf")
        for call in self.fake.start_workflow.await_args_list:
            self.assertEqual(call.kwargs["task_queue"], "aiops-production-tasks")
            snapshot = call.kwargs["args"][1]
            self.assertTrue(snapshot["shadow_mode"])
            self.assertEqual(snapshot["temporal_namespace"], "aiops-production")

    async def test_cli_read_uses_configured_namespace(self):
        await demo_cli._client()
        self.connect.assert_awaited_once_with("localhost:7233", namespace="aiops-production")

    async def test_production_cli_seed_rejected_before_connect(self):
        for command in (demo_cli.cmd_start, demo_cli.cmd_queue_patch):
            with self.subTest(command=command.__name__), self.assertRaises(mode.ModeConfigError):
                await command(argparse.Namespace())
        self.connect.assert_not_awaited()

    async def test_demo_cli_start_uses_historical_queue(self):
        with mock.patch.dict(os.environ, {"AIOPS_MODE": "demo"}), mock.patch.object(
                demo_cli, "load_policy", return_value=config.Policy()):
            await demo_cli.cmd_start(argparse.Namespace(alert_id="test", service="isolated", severity="test", description="test"))
        self.assertEqual(self.connect.call_args.kwargs["namespace"], "default")
        self.assertEqual(self.fake.start_workflow.call_args.kwargs["task_queue"], "aiops-tasks")

    async def test_worker_queue_is_runtime_not_import_time_constant(self):
        instance = mock.Mock(run=mock.AsyncMock())
        policy = config.Policy.model_validate({"release_gate": {"shadow_mode": True}})
        settings = {"listeners": {"metrics": {"host": "127.0.0.1", "port": 19091}}}
        with mock.patch.object(worker, "startup_snapshot", return_value=settings), mock.patch.object(
                worker, "load_policy", return_value=policy), mock.patch.object(worker.tracing, "temporal_interceptors", return_value=[]), mock.patch(
                "prometheus_client.start_http_server") as metrics, mock.patch.object(worker, "Worker", return_value=instance) as fake_worker:
            await worker.main()
        self.assertEqual(fake_worker.call_args.kwargs["task_queue"], "aiops-production-tasks")
        self.connect.assert_awaited_once_with("localhost:7233", namespace="aiops-production")
        metrics.assert_called_once_with(19091, addr="127.0.0.1")
        instance.run.assert_awaited_once()

    def test_children_inherit_parent_namespace_queue_and_snapshot(self):
        tree = ast.parse((ROOT / "aiops_agent/workflows.py").read_text())
        children = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "start_child_workflow"]
        self.assertEqual(len(children), 1)
        self.assertNotIn("task_queue", [k.arg for k in children[0].keywords])
        self.assertNotIn("namespace", [k.arg for k in children[0].keywords])
        arguments = next(k.value for k in children[0].keywords if k.arg == "args")
        self.assertEqual(ast.unparse(arguments.elts[1]), "self._policy")


if __name__ == "__main__":
    unittest.main()
