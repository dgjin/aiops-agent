"""Independent, destructive-to-disposable-fixtures-only CI entrypoint.

--check is offline. --run requires a fresh GitHub-hosted Linux runner, exact
project, ports, bind mounts and digest-pinned images. Never imports tests/ or
weakens its no-real-services audit hook. Never loads repository .env.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/ci/compose.yml"
PROJECT = "aiops-ci-isolated-i"
NAMESPACE = "aiops-production"  # same name, DIFFERENT isolated Server
QUEUE = "aiops-production-tasks"
PORTS = {"mysql": (23316, 3306), "redis": (26380, 6379), "temporal": (27233, 7233)}
HOOK_PORTS = (28099, 28100)
IMAGES = {
    "mysql": "mysql@sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b",
    "redis": "redis@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99",
    "temporal": "temporalio/server:1.32.1@sha256:acecbab5b0b8ae9cf4012b508866ca97feda3709577bf157b537239f40029049",
    "schema": "temporalio/admin-tools:1.32.1@sha256:9ef0dc5b063bf4ffd5c1512de72e0e73e225bd2c7006a18ce69bd592a8862a83",
}
DB_URL = "mysql+pymysql://ci_app:ci-only-app@127.0.0.1:23316/aiops_ci_app?charset=utf8mb4"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def static_check():
    import yaml
    doc = yaml.safe_load(COMPOSE.read_text())
    require(doc.get("name") == PROJECT and set(doc["services"]) == set(IMAGES), "wrong CI project/services")
    require(set(doc) == {"name", "services"}, "external resources/includes forbidden")
    mounts = {
        "mysql": ["${AIOPS_CI_ROOT:?CI preflight required}/bootstrap.sql:/docker-entrypoint-initdb.d/ci.sql:ro"],
        "temporal": ["${AIOPS_CI_ROOT:?CI preflight required}/server.yaml:/etc/aiops-ci/server.yaml:ro"],
    }
    for name, service in doc["services"].items():
        require(service["image"] == IMAGES[name] and re.search(r"@sha256:[a-f0-9]{64}$", service["image"]), "image drift")
        require(service.get("labels") == {"io.aiops.ci": "batch-i"}, "missing CI label")
        require(set(service) <= {"image", "labels", "environment", "ports", "tmpfs", "volumes", "healthcheck",
                                 "entrypoint", "command", "depends_on", "restart", "read_only", "cap_drop", "security_opt"},
                "unsafe or unknown service option")
        require(service.get("tmpfs", []) == {"mysql": ["/var/lib/mysql"], "redis": ["/data"],
                                              "temporal": ["/tmp"]}.get(name, []), "unexpected tmpfs")
        expected = [f"127.0.0.1:{PORTS[name][0]}:{PORTS[name][1]}"] if name in PORTS else []
        require(service.get("ports", []) == expected, "wrong CI port mapping")
        require(service.get("volumes", []) == mounts.get(name, []), "unexpected mount")
    require(doc["services"]["temporal"]["entrypoint"] == ["temporal-server"], "must use formal Temporal Server")
    print("CI static target contract OK (no containers invoked)")


def docker(*args):
    # Fixed local Docker daemon. No context inherited from the workstation.
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
           "DOCKER_HOST": "unix:///var/run/docker.sock", "COMPOSE_DISABLE_ENV_FILE": "1",
           "AIOPS_CI_ROOT": os.environ.get("AIOPS_CI_ROOT", "")}
    return subprocess.check_output(["docker", *args], cwd=ROOT, env=env, text=True, timeout=360)


def compose(*args):
    return docker("compose", "--project-name", PROJECT, "--env-file", "/dev/null", "-f", str(COMPOSE), *args)


def running_targets():
    ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}").split()
    require(bool(ids), "isolated containers missing")
    containers = json.loads(docker("inspect", *ids))
    seen = set()
    root = Path(os.environ["AIOPS_CI_ROOT"])
    require(not root.is_symlink() and (root / "marker").read_text() == PROJECT, "invalid CI ownership root")
    for container in containers:
        labels = container["Config"]["Labels"]
        name = labels.get("com.docker.compose.service")
        require(name in IMAGES and name not in seen and labels.get("io.aiops.ci") == "batch-i", "foreign container in CI project")
        require(labels.get("com.docker.compose.project") == PROJECT, "wrong container project")
        require(container["Config"]["Image"] == IMAGES[name], "running image mismatch")
        bindings = {k: v for k, v in (container["HostConfig"].get("PortBindings") or {}).items() if v}
        expected = {}
        if name in PORTS:
            host, internal = PORTS[name]
            expected = {f"{internal}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(host)}]}
        require(bindings == expected, "running port mismatch")
        binds = {"mysql": ("bootstrap.sql", "/docker-entrypoint-initdb.d/ci.sql"),
                 "temporal": ("server.yaml", "/etc/aiops-ci/server.yaml")}
        seen_binds = []
        for mount in container["Mounts"]:
            if mount["Type"] == "tmpfs":
                require(mount["Destination"] in {"mysql": ["/var/lib/mysql"], "redis": ["/data"],
                                                  "temporal": ["/tmp"]}.get(name, []), "foreign tmpfs")
            else:
                require(name in binds and mount["Type"] == "bind" and not mount["RW"], "foreign volume/mount")
                filename, destination = binds[name]
                source = Path(mount["Source"])
                require(source == root / filename and not source.is_symlink() and
                        mount["Destination"] == destination, "foreign bind source/destination")
                seen_binds.append(filename)
        require(seen_binds == ([binds[name][0]] if name in binds else []), "missing/repeated bind")
        seen.add(name)
    require(seen == set(IMAGES), "incomplete CI services")


def prepare():
    static_check()
    require(sys.platform == "linux" and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("CI") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "execution requires a fresh GitHub-hosted Linux CI runner")
    require(Path(os.environ.get("GITHUB_WORKSPACE", "/")).resolve() == ROOT, "wrong workspace")
    require(not (ROOT / ".env").exists(), "repository .env forbidden")
    require(not os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"), "inherited Docker target forbidden")
    require(not docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}").strip(), "refusing existing CI project")
    for port in [v[0] for v in PORTS.values()] + list(HOOK_PORTS):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))  # occupied => refuse, never kill an owner
    root = Path(tempfile.mkdtemp(prefix="aiops-ci-isolated-i-", dir="/tmp"))
    root.chmod(0o700)
    os.environ["AIOPS_CI_ROOT"] = str(root)
    (root / "marker").write_text(PROJECT)
    sql = []
    for database in ("aiops_ci_app", "aiops_ci_temporal", "aiops_ci_visibility"):
        sql.append(f"CREATE DATABASE {database} CHARACTER SET utf8mb4;")
    for user, password in (("ci_app", "ci-only-app"), ("ci_migration", "ci-only-migration"), ("ci_runtime", "ci-only-runtime")):
        sql.append(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password}';")
    sql.append("GRANT ALL ON aiops_ci_app.* TO 'ci_app'@'%';")
    for database in ("aiops_ci_temporal", "aiops_ci_visibility"):
        sql.extend([f"GRANT ALL ON {database}.* TO 'ci_migration'@'%';",
                    f"GRANT SELECT,INSERT,UPDATE,DELETE ON {database}.* TO 'ci_runtime'@'%';"])
    (root / "bootstrap.sql").write_text("\n".join(sql))
    server = (ROOT / "deploy/temporal/production.yaml").read_text()
    server = server.replace("temporal_visibility_prod", "aiops_ci_visibility").replace("temporal_prod", "aiops_ci_temporal")
    server = server.replace("temporal_runtime", "ci_runtime").replace("__RUNTIME_PASSWORD_JSON__", '"ci-only-runtime"')
    require("temporal_prod" not in server and "__RUNTIME" not in server, "server target rendering failed")
    import yaml
    rendered = yaml.safe_load(server)
    stores = rendered["persistence"]["datastores"]
    require(set(stores) == {"primary", "visibility"}, "foreign Temporal datastore")
    for name, database in (("primary", "aiops_ci_temporal"), ("visibility", "aiops_ci_visibility")):
        sql = stores[name]["sql"]
        require(all(sql.get(k) == v for k, v in {"connectAddr": "mysql:3306", "databaseName": database,
                    "user": "ci_runtime", "password": "ci-only-runtime", "pluginName": "mysql8"}.items()),
                "foreign Temporal persistence target")
    require(rendered["clusterMetadata"]["clusterInformation"] == {"active": {
        "enabled": True, "initialFailoverVersion": 1, "rpcName": "frontend", "rpcAddress": "127.0.0.1:7233"}},
        "foreign Temporal cluster")
    (root / "server.yaml").write_text(server)
    (root / "policy.yaml").write_text("release_gate:\n  shadow_mode: true\n")
    return root


def application_environment(root):
    # Whitelist environment: no workstation credentials, external integration URLs or app repositories.
    keep = {key: os.environ[key] for key in ("PATH", "HOME")}
    os.environ.clear()
    os.environ.update(keep | {"PYTHON_DOTENV_DISABLED": "1", "AIOPS_CI_ROOT": str(root),
        "AIOPS_MODE": "production", "AIOPS_DATABASE_URL": DB_URL,
        "AIOPS_REDIS_URL": "redis://127.0.0.1:26380/15", "TEMPORAL_ADDRESS": "127.0.0.1:27233",
        "TEMPORAL_NAMESPACE": NAMESPACE, "TEMPORAL_TASK_QUEUE": QUEUE,
        "AIOPS_DATA_ROOT": str(root / "data"), "AIOPS_POLICY_PATH": str(root / "policy.yaml"),
        "AIOPS_WEBHOOK_TOKEN": "ci-only-webhook", "AIOPS_WEBHOOK_ALLOW_IPS": "127.0.0.1",
        "AIOPS_CONSOLE_AUTH_TOKENS": json.dumps({"ci-only-viewer": {"user": "ci-viewer", "role": "viewer"}}),
        "AIOPS_IM_TOKEN": "ci-only-im-channel", "AIOPS_IM_USERS": '{"ci-admin":"admin"}',
        "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": "ci-only-admin-password", "AIOPS_METRICS_PORT": "9091"})
    validate_application_target()
    sys.path.insert(0, str(ROOT))


def validate_application_target():
    root = Path(os.environ["AIOPS_CI_ROOT"])
    require(root.parent == Path("/tmp") and root.name.startswith(PROJECT + "-") and not root.is_symlink(), "invalid CI root")
    require((root / "marker").read_text() == PROJECT, "missing CI marker")
    exact = {"AIOPS_MODE": "production", "AIOPS_DATABASE_URL": DB_URL,
             "AIOPS_REDIS_URL": "redis://127.0.0.1:26380/15", "TEMPORAL_ADDRESS": "127.0.0.1:27233",
             "TEMPORAL_NAMESPACE": NAMESPACE, "TEMPORAL_TASK_QUEUE": QUEUE,
             "AIOPS_DATA_ROOT": str(root / "data"), "AIOPS_POLICY_PATH": str(root / "policy.yaml")}
    require(all(os.environ.get(k) == v for k, v in exact.items()), "unsafe application target")


def webhook_child(port):
    require(port in HOOK_PORTS, "unexpected webhook port")
    validate_application_target()
    from http.server import ThreadingHTTPServer
    import alert_webhook as hook
    from aiops_agent.config import load_policy, snapshot_for_workflow
    hook._POLICY_SNAPSHOT = snapshot_for_workflow(load_policy())
    hook._AUTH_CONFIG = hook.load_auth_config()
    ThreadingHTTPServer(("127.0.0.1", port), hook._WebhookHandler).serve_forever()


def send_alert(index):
    body = {"alerts": [{"status": "firing", "labels": {"service": "ci-isolated-fixture", "alert_id": f"ci-{index}"}}]}
    opener = build_opener(ProxyHandler({}))
    request = Request(f"http://127.0.0.1:{HOOK_PORTS[index % 2]}/webhook", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer ci-only-webhook"})
    for _ in range(40):
        try:
            with opener.open(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code < 500:
                raise
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError("webhook failed to confirm event")


async def integration():
    from datetime import timedelta
    from sqlalchemy import MetaData, inspect, select, func
    from google.protobuf.duration_pb2 import Duration
    from temporalio import activity
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, RegisterNamespaceRequest
    from temporalio.api.enums.v1 import NamespaceState
    from temporalio.service import RPCError, RPCStatusCode
    from temporalio.client import Client
    from temporalio.worker import Worker
    from aiops_agent import db, readiness
    from aiops_agent.models import Patch, RootCause, TestReport
    from aiops_agent.workflows import AIOpsFixWorkflow
    from bff.temporal_gateway import TemporalGateway

    validate_application_target()
    require(callable(getattr(db, "ensure_schema", None)), "batch C ensure_schema missing: integration blocked, not skipped")
    # Explicit migration is allowed ONLY after the exact isolated target checks above.
    engine = readiness.create_engine(DB_URL)
    try:
        require(not inspect(engine).get_table_names(), "refusing nonempty integration database")
        old = db.monitored_apps.to_metadata(MetaData())
        old._columns.remove(old.c.depends_on)  # legacy fixture, not application metadata
        old.create(engine)
        db.ensure_schema(engine)
        db.ensure_schema(engine)
        require(readiness.inspect_schema(engine)["ok"], "schema upgrade/idempotence failed")
        require(readiness.probe_redis()["ok"], "Redis PING failed")
        client = None
        registered = False
        for _ in range(90):
            try:
                client = await asyncio.wait_for(Client.connect("127.0.0.1:27233", namespace=NAMESPACE), 5)
                try:
                    await client.workflow_service.register_namespace(RegisterNamespaceRequest(
                        namespace=NAMESPACE, workflow_execution_retention_period=Duration(seconds=30 * 86400)),
                        timeout=timedelta(seconds=3))
                except RPCError as exc:
                    if exc.status != RPCStatusCode.ALREADY_EXISTS:
                        raise
                namespace = await client.workflow_service.describe_namespace(
                    DescribeNamespaceRequest(namespace=NAMESPACE), timeout=timedelta(seconds=3))
                registered = (namespace.namespace_info.name == NAMESPACE and
                              namespace.namespace_info.state == NamespaceState.NAMESPACE_STATE_REGISTERED)
                if registered:
                    break
            except Exception:
                pass
            await asyncio.sleep(2)
        require(registered, "isolated formal Temporal namespace RPC failed")

        # Actual business Workflow, external/sandbox/notification activities replaced by pure fixtures.
        # No release/MR activities registered: any accidental scheduling causes failure, never real effects.
        @activity.defn(name="collect_evidence")
        async def evidence(alert): return {"trace_ids": []}
        @activity.defn(name="cluster_logs")
        async def cluster(evidence): return {"templates": [], "compression_ratio": 0.0, "total_lines": 0}
        @activity.defn(name="analyze_root_cause")
        async def analyze(alert, clustered, trace_ids): return RootCause("ci", [], 0.99, "fixture")
        @activity.defn(name="retrieve_similar_fixes")
        async def retrieve(alert, cause): return []
        @activity.defn(name="generate_patch")
        async def generate(alert, cause, refs, attempt):
            return Patch("ci-patch", alert["alert_id"], [], "", "fixture", "low", "fixture", 0.99)
        @activity.defn(name="run_tests_in_sandbox")
        async def sandbox(alert, patch, attempt): return TestReport("ci-patch", True)
        @activity.defn(name="notify_shadow_suggestion")
        async def notify(alert, cause, patch, report): return {"recorded": True}

        async with Worker(client, task_queue=QUEUE, workflows=[AIOpsFixWorkflow],
                          activities=[evidence, cluster, analyze, retrieve, generate, sandbox, notify]):
            gw = TemporalGateway()
            for _ in range(30):
                report = await readiness.check(gw)
                if report["ok"]: break
                await asyncio.sleep(1)
            require(report["ok"], "critical readiness failed")
            from fastapi.testclient import TestClient
            from bff.app import app
            def identity_check():
                # No lifespan: do not run periodic rotation/sweep jobs for an API boundary check.
                api = TestClient(app, client=("127.0.0.1", 40000))
                require(api.get("/api/subsystems").status_code == 401, "anonymous detail leak")
                headers = {"Authorization": "Bearer ci-only-viewer"}
                require(api.get("/api/auth/status", headers=headers).status_code == 200, "viewer fixture not authenticated")
                response = api.post("/api/im/command", headers=headers,
                                    json={"sender": "ci-admin", "text": "approve ci-forged", "auth_source": "im"})
                require(response.status_code in (401, 403), "ordinary console identity entered IM channel")
            await asyncio.to_thread(identity_check)
            processes = [multiprocessing.get_context("spawn").Process(target=webhook_child, args=(port,)) for port in HOOK_PORTS]
            try:
                for process in processes: process.start()
                def concurrent_alerts():
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                        return list(pool.map(send_alert, list(range(8)) * 2))
                await asyncio.to_thread(concurrent_alerts)
                # C schema is the source of truth; all fixture events must share one representative.
                with engine.connect() as connection:
                    events = connection.execute(select(db.metadata.tables["alert_events"])).mappings().all()
                    members = connection.execute(select(func.count()).select_from(db.metadata.tables["alert_members"])).scalar_one()
                require(len(events) == 1 and members == 8, "cross-process aggregation/dedup failed")
                flows = []
                for _ in range(30):
                    flows = [item async for item in client.list_workflows('WorkflowType = "AIOpsFixWorkflow"')]
                    if flows:
                        break
                    await asyncio.sleep(1)
                require(len(flows) == 1, "expected one representative Workflow")
                handle = client.get_workflow_handle(flows[0].id)
                result = await asyncio.wait_for(handle.result(), 60)
                require(result["stage"] == "SHADOWED", "shadow workflow did not stop before release")
                history = await handle.fetch_history()
                scheduled = [e.activity_task_scheduled_event_attributes.activity_type.name for e in history.events
                             if e.HasField("activity_task_scheduled_event_attributes")]
                require(set(scheduled) == {"collect_evidence", "cluster_logs", "analyze_root_cause", "retrieve_similar_fixes",
                                           "generate_patch", "run_tests_in_sandbox", "notify_shadow_suggestion"}, "unexpected side-effect activity")
                require(not any(e.HasField("workflow_execution_signaled_event_attributes") for e in history.events), "forged signal accepted")
            finally:
                for process in processes:
                    if process.pid and process.is_alive(): process.terminate()
                    if process.pid: process.join(timeout=5)
        print("Isolated MySQL schema/concurrency, Redis PING, formal Temporal RPC/pollers/shadow and API boundary passed")
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.run:
        static_check()
        return
    root = prepare()
    try:
        compose("up", "-d", "mysql", "redis", "schema", "temporal")
        running_targets()
        application_environment(root)
        asyncio.run(integration())
    finally:
        # Only remove this fresh disposable project. No -v, prune, broad stop or production overlay.
        # On an incomplete startup, leave resources for runner disposal instead of guessing ownership.
        try:
            running_targets()
        except Exception:
            print("Cleanup ownership not fully verified; leave to ephemeral runner disposal", file=sys.stderr)
        else:
            compose("down")


if __name__ == "__main__":
    main()
