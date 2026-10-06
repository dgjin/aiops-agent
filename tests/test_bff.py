"""BFF 层单元测试：前置校验 / 项归一化 / 聚合 / 操作审计（不依赖运行中的 Temporal）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bff import aggregator, app, audit, config_items, temporal_gateway
from bff.routes.monitored_apps import _find_rollback_source
from bff.temporal_gateway import _item_from_result, _item_from_status, check_stage_allowed


class StageGuardTest(unittest.TestCase):
    def test_allowed(self):
        self.assertIsNone(check_stage_allowed("WAIT_APPROVAL", {"WAIT_APPROVAL"}))

    def test_blocked(self):
        message = check_stage_allowed("NOTIFYING", {"WAIT_APPROVAL"})
        self.assertIsNotNone(message)
        self.assertIn("NOTIFYING", message)
        self.assertIn("WAIT_APPROVAL", message)

    def test_unknown_stage(self):
        message = check_stage_allowed(None, {"NOTIFYING"})
        self.assertIn("未知", message)


class ItemNormalizeTest(unittest.TestCase):
    def test_from_status(self):
        base: dict = {}
        _item_from_status(
            base,
            {
                "stage": "WAIT_APPROVAL",
                "alert": {"alert_id": "a-1", "service": "order"},
                "deadline": {"kind": "approval", "at": "2026-10-01T14:00:00+00:00"},
                "needs_second": True,
                "approval": None,
                "second_approval": None,
                "deploy_command": None,
                "queued_patches": [{"workflow_id": "w-2", "alert_id": "a-2"}],
            },
        )
        self.assertEqual(base["stage"], "WAIT_APPROVAL")
        self.assertTrue(base["needs_second"])
        self.assertEqual(base["deadline"]["kind"], "approval")
        self.assertEqual(base["queued_patches"][0]["alert_id"], "a-2")

    def test_from_result(self):
        base: dict = {}
        _item_from_result(
            base,
            {
                "stage": "DONE",
                "alert_id": "a-1",
                "service": "order",
                "duration_seconds": 12.5,
                "confidence": 0.9,
                "model_version": "qwen3:8b",
                "patch_id": "p-1",
                "queued_patches": ["a-9"],
            },
        )
        self.assertEqual(
            base["alert"],
            {
                "alert_id": "a-1",
                "service": "order",
                "description": "",
                "severity": "critical",
            },
        )
        self.assertEqual(base["gate_events"], [])
        self.assertEqual(base["queued_patches"], [{"workflow_id": None, "alert_id": "a-9"}])
        self.assertEqual(base["confidence"], 0.9)
        self.assertEqual(base["model_version"], "qwen3:8b")

    def test_from_result_carries_alert_meta_and_gate_events(self):
        """P1-3：重试构造原告警所需的 description/severity 与升级原因所需的 gate_events。"""
        base: dict = {}
        _item_from_result(
            base,
            {
                "stage": "ESCALATED",
                "alert_id": "a-2",
                "service": "order",
                "alert": {"description": "订单错误率激增", "severity": "warning"},
                "gate_events": ["gate1:blocked:confidence-low", "gate2:timeout"],
                "root_cause": "下游超时",
            },
        )
        self.assertEqual(base["alert"]["description"], "订单错误率激增")
        self.assertEqual(base["alert"]["severity"], "warning")
        self.assertEqual(base["gate_events"][-1], "gate2:timeout")
        self.assertEqual(base["root_cause"], "下游超时")


class AuditTest(unittest.TestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(
                    actor="tester", action="approval:approve", wf_id="aiops-fix-order-a-1",
                    result="signaled",
                )
                audit.write_audit(
                    actor="tester", action="deploy-command:cancel", wf_id="aiops-fix-order-a-1",
                    result="signaled",
                )
                rows = audit.read_audit()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "deploy-command:cancel")  # 最近的在最前
        self.assertEqual(rows[0]["actor"], "tester")
        self.assertEqual(rows[0]["result"], "signaled")

    def test_read_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                self.assertEqual(audit.read_audit(), [])

    def test_write_creates_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(actor="tester", action="x", wf_id="w")
                self.assertTrue((Path(tmp) / "web-audit").is_dir())


class AggregatorTest(unittest.TestCase):
    def test_index_stats_shape(self):
        stats = aggregator.index_stats()
        if stats is None:
            self.skipTest("data/code_index.json 不存在")
        self.assertEqual(stats["dim"], 1024)
        self.assertGreater(stats["chunks"], 0)
        self.assertIn("backend", stats)

    def test_artifacts_for_patch_structure(self):
        result = aggregator.artifacts_for_patch("p-nonexistent-0")
        self.assertEqual(result["notify"], [])
        self.assertEqual(result["argocd"], [])
        self.assertIsNone(result["sandbox_dir"])

    def test_recent_releases(self):
        releases = aggregator.recent_releases(3)
        if not releases:
            self.skipTest("无 argocd 留痕")
        self.assertLessEqual(len(releases), 3)
        self.assertIn("version", releases[0])

    def test_probe_stable_down(self):
        result = aggregator.probe_stable("1", timeout=0.3)  # 端口 1 无服务
        self.assertFalse(result["running"])
        self.assertIn("error", result)


class StageFromExecStatusTest(unittest.TestCase):
    """B3：硬失败/取消的流程读不到 result，需按执行状态派生终态，避免显示为「未知」。"""

    def test_failed_and_timeout_map_to_failed(self):
        self.assertEqual(temporal_gateway.stage_from_exec_status("FAILED"), "FAILED")
        self.assertEqual(temporal_gateway.stage_from_exec_status("TIMED_OUT"), "FAILED")

    def test_cancelled_and_terminated_map_to_cancelled(self):
        self.assertEqual(temporal_gateway.stage_from_exec_status("CANCELED"), "CANCELLED")
        self.assertEqual(temporal_gateway.stage_from_exec_status("TERMINATED"), "CANCELLED")

    def test_completed_and_unknown_return_none(self):
        self.assertIsNone(temporal_gateway.stage_from_exec_status("COMPLETED"))
        self.assertIsNone(temporal_gateway.stage_from_exec_status("RUNNING"))
        self.assertIsNone(temporal_gateway.stage_from_exec_status(None))
        self.assertIsNone(temporal_gateway.stage_from_exec_status(""))

    def test_case_insensitive(self):
        self.assertEqual(temporal_gateway.stage_from_exec_status("failed"), "FAILED")


class RequiredRoleTest(unittest.TestCase):
    """集中式角色策略：读接口 viewer，配置写入 admin，流程写入 operator。"""

    def test_get_endpoints_need_viewer(self):
        for path in ("/api/overview", "/api/flows", "/api/system", "/api/audit", "/api/health"):
            self.assertEqual(app.required_role("GET", path), "viewer", path)

    def test_monitored_apps_writes_need_admin(self):
        self.assertEqual(app.required_role("POST", "/api/monitored-apps"), "admin")
        self.assertEqual(app.required_role("PUT", "/api/monitored-apps/app-1"), "admin")
        self.assertEqual(app.required_role("DELETE", "/api/monitored-apps/app-1"), "admin")
        self.assertEqual(app.required_role("POST", "/api/monitored-apps/app-1/toggle"), "admin")

    def test_flow_writes_need_operator(self):
        self.assertEqual(app.required_role("POST", "/api/flows/w-1/approval"), "operator")
        self.assertEqual(app.required_role("POST", "/api/flows/w-1/deploy-command"), "operator")
        self.assertEqual(app.required_role("POST", "/api/flows/w-1/queue-patch"), "operator")

    def test_unknown_write_defaults_to_admin(self):
        """未知写接口按最高要求（安全默认），避免新增端点漏配。"""
        self.assertEqual(app.required_role("POST", "/api/something-new"), "admin")


class AuditRecordTest(unittest.TestCase):
    """WP1：配置类审计写 target、wf_id 置空，result 不再一律 signaled。"""

    def _write(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                return audit.write_audit(**kwargs), audit.read_audit()

    def test_config_action_uses_target_and_empty_wf_id(self):
        record, rows = self._write(actor="li", action="monitored-app:update", target="app-1")
        self.assertEqual(record["target"], "app-1")
        self.assertEqual(record["wf_id"], "")
        self.assertEqual(record["result"], "ok")  # 配置操作不再是 signaled
        self.assertEqual(rows[0]["target"], "app-1")

    def test_workflow_action_keeps_wf_id_and_signaled(self):
        record, _ = self._write(
            actor="zhang", action="approval:approve", wf_id="aiops-fix-order-a1", result="signaled"
        )
        self.assertEqual(record["wf_id"], "aiops-fix-order-a1")
        self.assertIsNone(record["target"])
        self.assertEqual(record["result"], "signaled")


class ConfigItemsTest(unittest.TestCase):
    """WP4：配置项元数据——来源 / 生效方式 / 对运行中流程是否生效；敏感项脱敏。"""

    def _collect(self, **overrides):
        env = {
            "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps({"t1": {"user": "u", "role": "viewer"}}),
            **overrides,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return config_items.collect(
                policy_source="demo-policy.yaml", policy_summary="threshold=0.8", monitored_app_count=2
            )

    def test_item_shape(self):
        for item in self._collect():
            for key in (
                "key", "label", "value", "source", "effect", "owner", "applies_to_running", "note",
            ):
                self.assertIn(key, item)
            self.assertIn(item["effect"], {"hot", "restart", "snapshot"})

    def test_policy_is_snapshot_and_not_applied_to_running(self):
        item = next(i for i in self._collect() if i["key"] == "release-gate-policy.yaml")
        self.assertEqual(item["effect"], config_items.SNAPSHOT)
        self.assertFalse(item["applies_to_running"])
        self.assertIn("demo-policy.yaml", item["source"])

    def test_monitored_apps_is_hot(self):
        item = next(i for i in self._collect() if i["key"] == "data/monitored_apps.json")
        self.assertEqual(item["effect"], config_items.HOT)
        self.assertTrue(item["applies_to_running"])
        self.assertEqual(item["value"], "2 个应用")

    def test_secret_value_not_leaked(self):
        item = next(i for i in self._collect() if i["key"] == "AIOPS_CONSOLE_AUTH_TOKENS")
        self.assertEqual(item["effect"], config_items.HOT)
        self.assertIn("1 个令牌", item["value"])
        self.assertNotIn("t1", item["value"])  # 令牌值绝不回显

    def test_unset_secret_reports_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIOPS_CONSOLE_AUTH_TOKENS", None)
            item = next(
                i for i in config_items.collect() if i["key"] == "AIOPS_CONSOLE_AUTH_TOKENS"
            )
        self.assertIn("fail-closed", item["value"])

    def test_invalid_secret_json_reports_fail_closed(self):
        item = next(i for i in self._collect(AIOPS_CONSOLE_AUTH_TOKENS="{ not json") if i["key"] == "AIOPS_CONSOLE_AUTH_TOKENS")
        self.assertIn("fail-closed", item["value"])

    def test_worker_configs_require_restart(self):
        for key in ("AIOPS_FIX_PROVIDER", "AIOPS_QODER_MODEL", "AIOPS_SANDBOX_IMAGE"):
            item = next(i for i in self._collect() if i["key"] == key)
            self.assertEqual(item["effect"], config_items.RESTART, key)
            self.assertFalse(item["applies_to_running"], key)


class DependencyProbeTest(unittest.TestCase):
    """WP9：修复链路依赖探测（降级横幅数据源）。"""

    def test_http_reachable_ok(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        with mock.patch("bff.aggregator.urllib.request.urlopen", return_value=resp):
            ok, detail = aggregator._http_reachable("http://x/ready")
        self.assertTrue(ok)
        self.assertIn("200", detail)

    def test_http_error_still_counts_as_reachable(self):
        import urllib.error

        with mock.patch(
            "bff.aggregator.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("http://x/", 404, "nf", None, None),
        ):
            ok, detail = aggregator._http_reachable("http://x/ready")
        self.assertTrue(ok)
        self.assertIn("404", detail)

    def test_http_unreachable(self):
        with mock.patch(
            "bff.aggregator.urllib.request.urlopen", side_effect=ConnectionRefusedError("no")
        ):
            ok, detail = aggregator._http_reachable("http://x/ready")
        self.assertFalse(ok)
        self.assertIn("ConnectionRefusedError", detail)

    def test_probe_docker_ok(self):
        proc = mock.MagicMock(returncode=0, stdout="29.2.1\n", stderr="")
        with mock.patch("bff.aggregator.subprocess.run", return_value=proc):
            ok, detail = aggregator.probe_docker()
        self.assertTrue(ok)
        self.assertEqual(detail, "29.2.1")

    def test_probe_docker_missing_cli(self):
        with mock.patch("bff.aggregator.subprocess.run", side_effect=FileNotFoundError):
            ok, detail = aggregator.probe_docker()
        self.assertFalse(ok)
        self.assertIn("docker", detail)

    def test_probe_docker_daemon_down(self):
        proc = mock.MagicMock(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon\n")
        with mock.patch("bff.aggregator.subprocess.run", return_value=proc):
            ok, detail = aggregator.probe_docker()
        self.assertFalse(ok)
        self.assertIn("daemon", detail)

    def test_probe_dependencies_shape(self):
        with mock.patch.object(aggregator, "_http_reachable", return_value=(True, "HTTP 200")), mock.patch.object(
            aggregator, "probe_docker", return_value=(True, "29.2.1")
        ):
            deps = aggregator.probe_dependencies()
        self.assertEqual(sorted(deps), ["docker", "loki", "ollama"])
        self.assertTrue(all(info["ok"] for info in deps.values()))


class RollbackSourceTest(unittest.TestCase):
    """WP6：回滚依据来自审计记录（target 匹配 + update/toggle 动作）。"""

    def test_finds_latest_update_for_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(
                    actor="li", action="monitored-app:create", target="app-1", params={"name": "A"}
                )
                audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-1",
                    params={"changed": {"url": {"from": "http://old/", "to": "http://new/"}}},
                )
                audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-2",
                    params={"changed": {"url": {"from": "x", "to": "y"}}},
                )
                record = _find_rollback_source("app-1", None)
        self.assertIsNotNone(record)
        self.assertEqual(record["action"], "monitored-app:update")
        self.assertEqual(record["params"]["changed"]["url"]["from"], "http://old/")

    def test_returns_none_when_no_change_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                audit.write_audit(actor="li", action="monitored-app:create", target="app-1")
                self.assertIsNone(_find_rollback_source("app-1", None))

    def test_exact_ts_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(audit, "DATA_DIR", Path(tmp)):
                first = audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-1", params={"changed": {}}
                )
                audit.write_audit(
                    actor="li", action="monitored-app:update", target="app-1", params={"changed": {}}
                )
                record = _find_rollback_source("app-1", first["ts"])
        self.assertEqual(record["ts"], first["ts"])


class MonitoredAppProbeTest(unittest.TestCase):
    """被监控应用探测（AIOPS_MONITOR_URL，默认 http://localhost:3000/）。"""

    def test_default_url(self):
        self.assertEqual(aggregator.DEFAULT_MONITOR_URL, "http://localhost:3000/")

    def test_env_overrides_default(self):
        with mock.patch.dict("os.environ", {"AIOPS_MONITOR_URL": "http://example.test:8080/"}), \
             mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app()
        self.assertEqual(result["target"], "http://example.test:8080/")
        self.assertTrue(result["running"])
        self.assertEqual(result["status_code"], 200)
        self.assertIn("latency_ms", result)

    def test_explicit_url_wins_over_env(self):
        with mock.patch.dict("os.environ", {"AIOPS_MONITOR_URL": "http://env.test/"}), \
             mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 204
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://explicit.test/")
        self.assertEqual(result["target"], "http://explicit.test/")
        self.assertEqual(result["status_code"], 204)

    def test_http_error_still_means_online(self):
        """根路径返回 4xx/5xx 说明端口有服务在响应 → 视为在线（不解析 JSON）。"""
        import urllib.error

        with mock.patch("bff.aggregator.urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            url="http://x/", code=404, msg="Not Found", hdrs=None, fp=None)):
            result = aggregator.probe_monitored_app("http://x/")
        self.assertTrue(result["running"])
        self.assertEqual(result["status_code"], 404)
        self.assertIn("note", result)

    def test_keyword_match_means_online(self):
        """配置页面关键字且响应内容包含 → 在线（正常页面）。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'<div id="root"></div>'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/", keyword='<div id="root"')
        self.assertTrue(result["running"])
        self.assertEqual(result["status_code"], 200)

    def test_keyword_missing_marks_down(self):
        """HTTP 200 但内容缺少关键字（白屏类故障）→ 判失败，交由巡检计数/告警。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'<div id="rroot"></div>'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/", keyword='<div id="root"')
        self.assertFalse(result["running"])
        self.assertEqual(result["status_code"], 200)
        self.assertIn("关键字", result["error"])

    def test_health_path_appended_to_target(self):
        """Manifest 预填的健康路径拼接到探测地址（关键字在健康页上校验）。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = b'{"status": "ok"}'
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app(
                "http://x:8000/", keyword='"status": "ok"', health_path="/health"
            )
        self.assertTrue(result["running"])
        self.assertEqual(result["target"], "http://x:8000/health")
        self.assertEqual(urlopen.call_args.args[0], "http://x:8000/health")

    def test_health_path_root_keeps_original_target(self):
        """health_path 为空或 "/" 时探测根地址（旧语义不变）。"""
        with mock.patch("bff.aggregator.urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            urlopen.return_value = resp
            result = aggregator.probe_monitored_app("http://x/", health_path="/")
        self.assertEqual(result["target"], "http://x/")

    def test_unreachable_reports_error(self):
        with mock.patch("bff.aggregator.urllib.request.urlopen",
                        side_effect=ConnectionRefusedError("connection refused")):
            result = aggregator.probe_monitored_app("http://down.test/", timeout=0.2)
        self.assertFalse(result["running"])
        self.assertIn("ConnectionRefusedError", result["error"])

    def test_default_scheme_when_unset(self):
        """未设 AIOPS_MONITOR_URL 时回落默认地址。"""
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("AIOPS_MONITOR_URL", None)
            with mock.patch("bff.aggregator.urllib.request.urlopen",
                            side_effect=ConnectionRefusedError("nope")):
                result = aggregator.probe_monitored_app(timeout=0.2)
        self.assertEqual(result["target"], aggregator.DEFAULT_MONITOR_URL)


if __name__ == "__main__":
    unittest.main()
