"""批次 I 契约：所有服务均为 mock/临时 SQLite，不接触真实服务。"""
from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event
from temporalio.api.namespace.v1 import NamespaceInfo
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import DescribeNamespaceResponse, DescribeTaskQueueResponse

from aiops_agent import db, metrics, readiness
from bff.routes import health


def gateway(age=0, name="aiops-production", state=1):
    stamp = Timestamp(seconds=int(time.time() - age))
    rpc = SimpleNamespace(
        describe_namespace=AsyncMock(return_value=DescribeNamespaceResponse(
            namespace_info=NamespaceInfo(name=name, state=state))),
        describe_task_queue=AsyncMock(return_value=DescribeTaskQueueResponse(
            pollers=[PollerInfo(identity="unit-worker", last_access_time=stamp)])),
    )
    return SimpleNamespace(client=AsyncMock(return_value=SimpleNamespace(namespace="aiops-production", workflow_service=rpc))), rpc


class TemporalReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_rpc_on_every_request(self):
        gw, rpc = gateway()
        for _ in range(2):
            result = await readiness.probe_temporal(gw, "aiops-production", "aiops-production-tasks")
            self.assertTrue(result["temporal"]["ok"])
            self.assertTrue(result["worker"]["ok"])
        self.assertEqual(rpc.describe_namespace.await_count, 2)
        self.assertEqual(rpc.describe_task_queue.await_count, 4)
        for call in rpc.describe_task_queue.call_args_list:
            self.assertEqual(call.args[0].namespace, "aiops-production")
            self.assertEqual(call.args[0].task_queue.name, "aiops-production-tasks")

    async def test_stale_empty_future_and_missing_poller_times(self):
        for age in (121, -30):
            gw, _ = gateway(age=age)
            result = await readiness.probe_temporal(gw, "aiops-production", "q")
            self.assertTrue(result["temporal"]["ok"])
            self.assertFalse(result["worker"]["ok"])
        for pollers in ([], [PollerInfo(identity="no-time")]):
            gw, rpc = gateway()
            rpc.describe_task_queue.return_value = DescribeTaskQueueResponse(pollers=pollers)
            self.assertFalse((await readiness.probe_temporal(gw, "aiops-production", "q"))["worker"]["ok"])

    async def test_both_queue_types_required(self):
        gw, rpc = gateway()
        rpc.describe_task_queue.side_effect = [rpc.describe_task_queue.return_value, DescribeTaskQueueResponse()]
        result = await readiness.probe_temporal(gw, "aiops-production", "q")
        self.assertFalse(result["worker"]["ok"])
        self.assertEqual(result["worker"]["pollers"], {"workflow": 1, "activity": 0})

    async def test_namespace_wrong_disabled_and_rpc_failure(self):
        for name, state in (("default", 1), ("aiops-production", 2)):
            gw, _ = gateway(name=name, state=state)
            self.assertFalse((await readiness.probe_temporal(gw, "aiops-production", "q"))["temporal"]["ok"])
        gw, rpc = gateway()
        rpc.describe_namespace.side_effect = RuntimeError("secret@internal-address")
        result = await readiness.probe_temporal(gw, "aiops-production", "q")
        self.assertFalse(result["temporal"]["ok"])
        self.assertNotIn("secret", str(result))


class SchemaReadinessTest(unittest.TestCase):
    def test_missing_tables_columns_and_readonly(self):
        metadata = MetaData()
        for name in readiness.AGGREGATION_TABLES | {"monitored_apps"}:
            Table(name, metadata, Column("id", Integer, primary_key=True), Column("depends_on", Integer))
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with patch.object(db, "metadata", metadata), patch.object(db, "get_engine", side_effect=AssertionError("DDL forbidden")):
            self.assertFalse(readiness.inspect_schema(engine)["ok"])
            metadata.create_all(engine)  # fixture construction only
            statements = []
            event.listen(engine, "before_cursor_execute", lambda c, cu, sql, p, co, ex: statements.append(sql))
            self.assertTrue(readiness.inspect_schema(engine)["ok"])
            self.assertFalse(any(s.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE")) for s in statements))
            inspector = Mock()
            inspector.get_table_names.return_value = list(metadata.tables)
            inspector.get_columns.return_value = [{"name": "id"}]
            with patch.object(readiness, "inspect", return_value=inspector):
                result = readiness.inspect_schema(engine)
            self.assertIn("depends_on", result["missing"]["monitored_apps"])

    def test_missing_C_contract_fails_closed(self):
        with patch.object(db, "metadata", MetaData()):
            self.assertEqual(readiness.inspect_schema(Mock())["detail"], "schema_contract_missing")

    def test_mysql_error_redacted_and_engine_disposed(self):
        engine = Mock()
        engine.connect.side_effect = RuntimeError("password-in-error")
        with patch.object(readiness.mode, "db_url", return_value="mysql+pymysql://u:p@localhost/test"), \
                patch.object(readiness, "create_engine", return_value=engine), \
                patch.object(db, "get_engine", side_effect=AssertionError):
            result = readiness.probe_mysql()
        self.assertFalse(result["mysql"]["ok"])
        self.assertNotIn("password", str(result))
        engine.dispose.assert_called_once()

    def test_redis_requires_successful_ping(self):
        redis = Mock()
        redis.ping.side_effect = RuntimeError("credentials")
        with patch.dict(os.environ, {"AIOPS_REDIS_URL": "redis://localhost:33333/0"}), \
                patch("redis.Redis.from_url", return_value=redis):
            self.assertFalse(readiness.probe_redis()["ok"])
        redis.close.assert_called_once()


class ReadinessReportTest(unittest.IsolatedAsyncioTestCase):
    async def test_configuration_failure_does_not_connect(self):
        for setting in ({"AIOPS_MODE": "invalid"}, {"AIOPS_MODE": "production"}):
            gw = Mock()
            with patch.dict(os.environ, setting):
                report = await readiness.check(gw)
            self.assertFalse(report["ok"])
            self.assertFalse(report["checks"]["configuration"]["ok"])
            gw.client.assert_not_called()

    async def test_production_shadow_mode_must_be_explicit(self):
        """放行：显式 true/false 均合法；拦截：未声明（默认构造）拒绝。"""
        from aiops_agent.config import Policy, validate_runtime_policy
        with patch.dict(os.environ, {"AIOPS_MODE": "production"}):
            with self.assertRaises(readiness.mode.ModeConfigError):
                validate_runtime_policy(Policy())  # 未显式声明 shadow_mode → 拒绝
            validate_runtime_policy(Policy.model_validate({"release_gate": {"shadow_mode": False}}))
            validate_runtime_policy(Policy.model_validate({"release_gate": {"shadow_mode": True}}))
        with patch.dict(os.environ, {"AIOPS_MODE": "demo"}):
            validate_runtime_policy(Policy())  # demo 档无显式要求

    async def test_each_critical_failure_and_optional_degradation(self):
        snapshot = dict(mode="production", policy_sha256="abc", shadow_mode=True,
                        namespace="aiops-production", task_queue="aiops-production-tasks")
        blank = {key: "" for key in ("AIOPS_OLLAMA_URL", "AIOPS_LOKI_URL", "AIOPS_NOTIFY_WEBHOOK",
                                     "AIOPS_FEISHU_WEBHOOK", "AIOPS_DINGTALK_WEBHOOK")}
        for failed in (None, "mysql", "schema", "redis", "temporal", "worker"):
            values = {name: readiness.status(name != failed, "fixture")
                      for name in ("mysql", "schema", "redis", "temporal", "worker")}
            with patch.dict(os.environ, blank), \
                    patch.object(readiness.config, "startup_snapshot", return_value=snapshot), \
                    patch.object(readiness, "probe_mysql", return_value={k: values[k] for k in ("mysql", "schema")}), \
                    patch.object(readiness, "probe_redis", return_value=values["redis"]), \
                    patch.object(readiness, "probe_temporal", new=AsyncMock(return_value={k: values[k] for k in ("temporal", "worker")})):
                report = await readiness.check(Mock())
            self.assertEqual(report["ok"], failed is None)
            self.assertEqual(set(report["degradations"]), {"llm", "loki", "notifications"})
            self.assertTrue(all(not v["ok"] for v in report["degradations"].values()))


class HealthRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(health.router)
        self.report = {"ok": False, "checks": {"temporal": readiness.status(False, "namespace_rpc_failed")},
                       "degradations": readiness.degradations(), "configuration": {"mode": "production"}}
        self.probe = patch.object(readiness, "check", new=AsyncMock(return_value=self.report))
        self.mock_probe = self.probe.start()
        self.addCleanup(self.probe.stop)

    def test_loopback_live_only_and_ready_503_minimal(self):
        with TestClient(self.app, client=("127.0.0.1", 50000)) as client:
            self.assertEqual(client.get("/livez").json(), {"ok": True})
            self.mock_probe.assert_not_called()
            response = client.get("/readyz")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"ok": False})
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.report["ok"] = True
            self.assertEqual(client.get("/readyz").status_code, 200)

    def test_non_loopback_and_forged_forwarding_refused(self):
        for host, headers in (("192.0.2.1", {}), ("192.0.2.1", {"X-Forwarded-For": "127.0.0.1"}),
                              ("127.0.0.1", {"X-Forwarded-For": "192.0.2.1"}),
                              ("127.0.0.1", {"Forwarded": "for=127.0.0.1"})):
            with TestClient(self.app, client=(host, 50000)) as client:
                for path in ("/livez", "/readyz", "/metrics"):
                    self.assertEqual(client.get(path, headers=headers).status_code, 404)
        self.mock_probe.assert_not_called()

    def test_detail_requires_auth_even_when_router_standalone(self):
        with TestClient(self.app) as client:
            for path in ("/api/health", "/api/subsystems"):
                self.assertEqual(client.get(path).status_code, 401)
        self.mock_probe.assert_not_called()

    def test_authenticated_compatibility_and_details(self):
        @self.app.middleware("http")
        async def identity(request, call_next):
            request.state.identity = SimpleNamespace(role="viewer")
            return await call_next(request)
        with TestClient(self.app) as client:
            response = client.get("/api/health")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(set(response.json()), {"server_time", "ok", "temporal"})
            self.assertFalse(response.json()["ok"])
            details = client.get("/api/subsystems").json()
            self.assertTrue(details["degraded_mode"])
            self.assertEqual(details["configuration"], {"mode": "production"})


class ReadinessMetricsTest(unittest.TestCase):
    def test_server_loopback_default_and_invalid_ports(self):
        with patch.dict(os.environ, {}, clear=True), patch("prometheus_client.start_http_server") as server:
            self.assertEqual(metrics.start_worker_server(), 9091)
            server.assert_called_once_with(9091, addr="127.0.0.1")
        for port in ("bad", "65536", "-1", "0"):
            with patch.dict(os.environ, {"AIOPS_METRICS_PORT": port}), patch("prometheus_client.start_http_server") as server:
                self.assertEqual(metrics.start_worker_server(), 0)
                server.assert_not_called()

    def test_aggregate_metrics_and_fixed_labels(self):
        from prometheus_client import REGISTRY
        metrics.set_alert_pending(3)
        self.assertEqual(REGISTRY.get_sample_value("aiops_alert_pending"), 3)
        metrics.set_alert_pending(None)
        self.assertEqual(REGISTRY.get_sample_value("aiops_alert_pending"), -1)
        for method, metric in ((metrics.observe_compensation_failure, "aiops_compensation_failures_total"),
                               (metrics.observe_release_conflict, "aiops_release_conflicts_total"),
                               (metrics.observe_rollback_failure, "aiops_rollback_failures_total")):
            before = REGISTRY.get_sample_value(metric)
            method()
            self.assertEqual(REGISTRY.get_sample_value(metric), before + 1)
        metrics.observe_component_restart("workflow-private-id")
        text = metrics.render()[0].decode()
        self.assertNotIn("workflow-private-id", text)
        self.assertIn('component="other"', text)

    def test_backup_26h_certificate_30d_and_unknown(self):
        from prometheus_client import REGISTRY
        now = time.time()
        self.addCleanup(metrics.set_backup_last_success, None)
        self.addCleanup(metrics.set_certificate_expiry, None)
        with patch.object(metrics.time, "time", return_value=now):
            metrics.set_backup_last_success(now - 26 * 3600)
            self.assertEqual(REGISTRY.get_sample_value("aiops_backup_overdue"), 0)
            metrics.set_certificate_expiry(now + 30 * 86400)
            self.assertEqual(REGISTRY.get_sample_value("aiops_certificate_renewal_due"), 1)
        with patch.object(metrics.time, "time", return_value=now + 1):
            self.assertEqual(REGISTRY.get_sample_value("aiops_backup_overdue"), 1)
        metrics.set_backup_last_success(float("nan"))
        metrics.set_certificate_expiry(None)
        self.assertEqual(REGISTRY.get_sample_value("aiops_backup_overdue"), 1)
        self.assertEqual(REGISTRY.get_sample_value("aiops_certificate_renewal_due"), 1)


class ReadinessCIContractTest(unittest.TestCase):
    def test_offline_compose_and_reject_drift(self):
        import copy
        import yaml
        from scripts import ci_integration as ci
        doc = yaml.safe_load(ci.COMPOSE.read_text())
        with patch.object(ci, "docker", side_effect=AssertionError("no Docker in unit tests")):
            ci.static_check()
            for change in (lambda d: d.update(name="production"),
                           lambda d: d.update(include=["../../docker-compose.yml"]),
                           lambda d: d["services"]["mysql"].update(ports=["127.0.0.1:3306:3306"]),
                           lambda d: d["services"]["redis"].update(volumes=["production:/data"]),
                           lambda d: d["services"]["temporal"].update(network_mode="host"),
                           lambda d: d["services"]["mysql"].update(image="mysql:latest")):
                bad = copy.deepcopy(doc)
                change(bad)
                with patch("yaml.safe_load", return_value=bad), self.assertRaises(RuntimeError):
                    ci.static_check()

    def test_run_rejected_before_docker_outside_ci(self):
        from scripts import ci_integration as ci
        with patch.dict(os.environ, {"CI": "false"}), patch.object(ci, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "GitHub-hosted"):
                ci.prepare()
            docker.assert_not_called()

    def test_exact_application_targets(self):
        from pathlib import Path
        from scripts import ci_integration as ci
        root = "/tmp/aiops-ci-isolated-i-unit-fixture"
        env = {"AIOPS_CI_ROOT": root, "AIOPS_MODE": "production", "AIOPS_DATABASE_URL": ci.DB_URL,
               "AIOPS_REDIS_URL": "redis://127.0.0.1:26380/15", "TEMPORAL_ADDRESS": "127.0.0.1:27233",
               "TEMPORAL_NAMESPACE": ci.NAMESPACE, "TEMPORAL_TASK_QUEUE": ci.QUEUE,
               "AIOPS_DATA_ROOT": root + "/data", "AIOPS_POLICY_PATH": root + "/policy.yaml"}
        with patch.dict(os.environ, env, clear=True), patch.object(Path, "read_text", return_value=ci.PROJECT):
            ci.validate_application_target()
            for key, value in (("TEMPORAL_ADDRESS", "127.0.0.1:7233"), ("TEMPORAL_NAMESPACE", "default"),
                               ("TEMPORAL_TASK_QUEUE", "aiops-tasks"), ("AIOPS_REDIS_URL", "redis://localhost:6379/0"),
                               ("AIOPS_DATABASE_URL", "mysql+pymysql://u:p@localhost/production"),
                               ("AIOPS_DATA_ROOT", "/var/lib/aiops")):
                with patch.dict(os.environ, {key: value}), self.assertRaises(RuntimeError):
                    ci.validate_application_target()

    def test_dependency_platforms_hash_and_closure(self):
        from scripts import ci_dependencies as deps
        self.assertTrue(deps.selected("x-1-cp312-cp312-manylinux_2_28_x86_64.whl"))
        self.assertTrue(deps.selected("x-1-cp39-abi3-macosx_11_0_arm64.whl"))
        self.assertFalse(deps.selected("x-1-cp313-cp313-manylinux_2_28_x86_64.whl"))
        self.assertFalse(deps.selected("x-1-cp312-cp312-win_amd64.whl"))
        report = {"install": [{"metadata": {"name": "example", "version": "1", "requires_dist": [
            'linux-only>=2; sys_platform == "linux"']}, "download_info": {"archive_info": {"hashes": {"sha256": "abc"}}}}]}
        lock = "example==1\n    --hash=sha256:abc\nlinux-only==2\n"
        deps.check_closure(report, lock)
        for bad in (lock.replace("abc", "def"), lock.replace("linux-only==2", "linux-only==1"),
                    lock.replace("example==1", "example==2")):
            with self.assertRaises(SystemExit):
                deps.check_closure(report, bad)

    def test_running_ownership_refuses_foreign_mount_and_port(self):
        import copy
        import json
        from pathlib import Path
        from scripts import ci_integration as ci
        root = "/tmp/aiops-ci-isolated-i-unit-fixture"
        containers = []
        for name, image in ci.IMAGES.items():
            bindings = {}
            if name in ci.PORTS:
                host, internal = ci.PORTS[name]
                bindings = {f"{internal}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(host)}]}
            mounts = []
            if name in {"mysql", "temporal"}:
                filename, dest = (("bootstrap.sql", "/docker-entrypoint-initdb.d/ci.sql") if name == "mysql"
                                  else ("server.yaml", "/etc/aiops-ci/server.yaml"))
                mounts = [{"Type": "bind", "RW": False, "Source": root + "/" + filename, "Destination": dest}]
            containers.append({"Config": {"Image": image, "Labels": {"com.docker.compose.project": ci.PROJECT,
                "com.docker.compose.service": name, "io.aiops.ci": "batch-i"}},
                "HostConfig": {"PortBindings": bindings}, "Mounts": mounts})
        with patch.dict(os.environ, {"AIOPS_CI_ROOT": root}), patch.object(Path, "read_text", return_value=ci.PROJECT):
            with patch.object(ci, "docker", side_effect=["fixture-id", json.dumps(containers)]):
                ci.running_targets()
            for change in (lambda d: d[0]["Mounts"][0].update(Source="/var/lib/production.sql"),
                           lambda d: d[0]["HostConfig"]["PortBindings"].update({"33060/tcp": [{"HostIp": "0.0.0.0", "HostPort": "33060"}]}),
                           lambda d: d[0]["Config"]["Labels"].update({"com.docker.compose.project": "production"})):
                bad = copy.deepcopy(containers)
                change(bad)
                with patch.object(ci, "docker", side_effect=["fixture-id", json.dumps(bad)]), self.assertRaises(RuntimeError):
                    ci.running_targets()

    def test_ci_preserves_gates_and_no_event_shell_interpolation(self):
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs["isolated-integration"]["needs"]), {"tests", "web-build", "security-scan", "image-security"})
        for job in jobs.values():
            self.assertNotIn("continue-on-error", job)
            for step in job["steps"]:
                self.assertNotIn("continue-on-error", step)
                self.assertNotIn("${{", step.get("run", ""))
        self.assertIn("discover -s tests -t .", str(jobs["tests"]))
        self.assertIn("--require-hashes", str(jobs["tests"]))
        self.assertIn("pip_audit", str(jobs["security-scan"]))

    def test_k8s_health_contract_offline(self):
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for name in ("bff", "worker"):
            docs = list(yaml.safe_load_all((root / f"deploy/k8s/{name}.yaml").read_text()))
            deployment = next(d for d in docs if d["kind"] == "Deployment")
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            for kind in ("readinessProbe", "livenessProbe"):
                probe = container[kind]
                self.assertIn("exec", probe)
                self.assertNotIn("/metrics", str(probe))


class DegradationsTest(unittest.TestCase):
    """降级判定：已配置依赖不进入降级横幅（标注未验收）；未配置项如实降级。"""

    blank = {key: "" for key in ("AIOPS_OLLAMA_URL", "AIOPS_LOKI_URL", "AIOPS_NOTIFY_WEBHOOK",
                                 "AIOPS_FEISHU_WEBHOOK", "AIOPS_DINGTALK_WEBHOOK")}

    def test_unconfigured_all_degraded(self):
        with patch.dict(os.environ, self.blank):
            result = readiness.degradations()
        self.assertEqual(set(result), {"llm", "loki", "notifications"})
        for item in result.values():
            self.assertFalse(item["ok"])
            self.assertEqual(item["detail"], "not_configured_or_local_fallback")

    def test_configured_not_degraded_but_unverified(self):
        configured = {"AIOPS_OLLAMA_URL": "http://localhost:11434",
                      "AIOPS_LOKI_URL": "http://localhost:3101",
                      "AIOPS_DINGTALK_WEBHOOK": "https://example.invalid/robot"}
        with patch.dict(os.environ, {**self.blank, **configured}):
            result = readiness.degradations()
        for item in result.values():
            self.assertTrue(item["ok"])
            self.assertEqual(item["detail"], "configured_not_verified")
