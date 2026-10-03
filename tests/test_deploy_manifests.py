"""部署交付物结构校验（批次 8：K8s manifests / compose profiles / Grafana / Prometheus）。

只做静态解析与结构断言，不依赖 kubectl / docker / 集群：
    - deploy/k8s/*.yaml：一键部署前提（平铺、命名空间一致、镜像统一、副本/PDB/探针/策略）；
    - deploy/prometheus/：scrape 目标与告警规则（与 servicemonitor.yaml 的 PrometheusRule 同步）；
    - deploy/grafana/：dashboard JSON 与 provisioning（datasource uid 一致性）；
    - docker-compose.yml：profiles 语义（默认行为不变）与 obs/profile 挂载；
    - deploy/Dockerfile：多阶段构建与非 root 运行。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = REPO_ROOT / "deploy" / "k8s"
PROM_DIR = REPO_ROOT / "deploy" / "prometheus"
GRAFANA_DIR = REPO_ROOT / "deploy" / "grafana"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"

NAMESPACE = "aiops-system"
IMAGE = "aiops-agent:1.0.0"


def _load_all(path: Path) -> list[dict]:
    """解析多文档 YAML；返回全部非空文档。"""
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    assert docs, f"{path} 未解析出任何文档"
    return docs


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def _k8s_docs() -> list[tuple[str, dict]]:
    """deploy/k8s 下全部 YAML 文档，标记来源文件。"""
    result: list[tuple[str, dict]] = []
    for path in sorted(K8S_DIR.glob("*.yaml")):
        for doc in _load_all(path):
            result.append((path.name, doc))
    return result


def _find_doc(kind: str, name: str) -> dict:
    for _fname, doc in _k8s_docs():
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"未找到 {kind}/{name}")


def _container_of(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["containers"][0]


class K8sManifestBasicsTest(unittest.TestCase):
    """平铺结构 / 命名空间 / 文档基本字段。"""

    def test_k8s_dir_is_flat(self):
        """kubectl apply -f deploy/k8s/ 只读取首层文件：不得有子目录。"""
        children = list(K8S_DIR.iterdir())
        self.assertTrue(children, "deploy/k8s 为空")
        dirs = [c for c in children if c.is_dir()]
        self.assertEqual(dirs, [], f"deploy/k8s 存在子目录（破坏一键部署）：{dirs}")

    def test_all_docs_have_basic_fields(self):
        for fname, doc in _k8s_docs():
            with self.subTest(file=fname, kind=doc.get("kind")):
                self.assertIn("apiVersion", doc)
                self.assertIn("kind", doc)
                self.assertTrue(doc.get("metadata", {}).get("name"))

    def test_namespace_consistency(self):
        ns_doc = _find_doc("Namespace", NAMESPACE)
        self.assertEqual(ns_doc["metadata"]["name"], NAMESPACE)
        for fname, doc in _k8s_docs():
            if doc["kind"] == "Namespace":
                continue
            with self.subTest(file=fname, kind=doc["kind"], name=doc["metadata"]["name"]):
                self.assertEqual(doc["metadata"].get("namespace"), NAMESPACE)

    def test_expected_kinds_present(self):
        kinds = {doc["kind"] for _f, doc in _k8s_docs()}
        for expected in [
            "Namespace", "ConfigMap", "Secret", "StatefulSet", "Deployment",
            "Service", "Ingress", "PodDisruptionBudget", "HorizontalPodAutoscaler",
            "CronJob", "NetworkPolicy", "ServiceMonitor", "PrometheusRule",
            "PersistentVolumeClaim", "ServiceAccount",
        ]:
            with self.subTest(kind=expected):
                self.assertIn(expected, kinds)


class K8sWorkloadsTest(unittest.TestCase):
    """业务工作负载：镜像统一、副本数、探针、资源限额、安全上下文。"""

    BUSINESS = ["aiops-worker", "aiops-bff", "aiops-webhook"]

    def test_business_images_unified(self):
        for name in self.BUSINESS:
            doc = _find_doc("Deployment", name)
            with self.subTest(deployment=name):
                self.assertEqual(_container_of(doc)["image"], IMAGE)
        cron = _find_doc("CronJob", "aiops-cleanup")
        self.assertEqual(
            cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"],
            IMAGE,
        )

    def test_replicas_at_least_two(self):
        for name in self.BUSINESS:
            with self.subTest(deployment=name):
                self.assertGreaterEqual(_find_doc("Deployment", name)["spec"]["replicas"], 2)

    def test_pdb_min_available_one(self):
        for name in ["aiops-worker", "aiops-bff"]:
            with self.subTest(pdb=name):
                self.assertEqual(_find_doc("PodDisruptionBudget", name)["spec"]["minAvailable"], 1)

    def test_hpa_targets_and_cpu70(self):
        for name in ["aiops-worker", "aiops-bff"]:
            hpa = _find_doc("HorizontalPodAutoscaler", name)
            with self.subTest(hpa=name):
                self.assertEqual(hpa["spec"]["scaleTargetRef"]["name"], name)
                self.assertGreaterEqual(hpa["spec"]["minReplicas"], 2)
                metric = hpa["spec"]["metrics"][0]
                self.assertEqual(metric["type"], "Resource")
                self.assertEqual(metric["resource"]["name"], "cpu")
                self.assertEqual(metric["resource"]["target"]["averageUtilization"], 70)

    def test_probes(self):
        worker = _container_of(_find_doc("Deployment", "aiops-worker"))
        self.assertEqual(worker["readinessProbe"]["httpGet"], {"path": "/metrics", "port": 9090})
        bff = _container_of(_find_doc("Deployment", "aiops-bff"))
        # /api 前缀全部要求 Bearer 凭证（bff/middleware.py），httpGet 探针无法携带，
        # 必须使用无鉴权的 /metrics（Prometheus 抓取约定）
        self.assertEqual(bff["readinessProbe"]["httpGet"], {"path": "/metrics", "port": 8600})
        webhook = _container_of(_find_doc("Deployment", "aiops-webhook"))
        self.assertEqual(webhook["readinessProbe"]["tcpSocket"], {"port": 8099})
        for doc in [_find_doc("Deployment", k) for k in self.BUSINESS]:
            container = _container_of(doc)
            with self.subTest(deployment=doc["metadata"]["name"]):
                self.assertIn("livenessProbe", container)
                self.assertIn("resources", container)

    def test_security_context_non_root(self):
        for name in self.BUSINESS:
            pod_spec = _find_doc("Deployment", name)["spec"]["template"]["spec"]
            with self.subTest(deployment=name):
                self.assertTrue(pod_spec["securityContext"]["runAsNonRoot"])
        cron_spec = _find_doc("CronJob", "aiops-cleanup")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertTrue(cron_spec["securityContext"]["runAsNonRoot"])

    def test_worker_uses_shared_data_pvc(self):
        worker = _find_doc("Deployment", "aiops-worker")
        pod_spec = worker["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod_spec["volumes"]}
        self.assertEqual(volumes["data"]["persistentVolumeClaim"]["claimName"], "aiops-data")
        pvc = _find_doc("PersistentVolumeClaim", "aiops-data")
        self.assertIn("ReadWriteMany", pvc["spec"]["accessModes"])
        # CronJob 复用同一数据卷（清理沙箱 / Qoder 工作区）
        cron_pod = _find_doc("CronJob", "aiops-cleanup")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        cron_volumes = {v["name"]: v for v in cron_pod["volumes"]}
        self.assertEqual(cron_volumes["data"]["persistentVolumeClaim"]["claimName"], "aiops-data")

    def test_bff_launch_flags(self):
        command = _container_of(_find_doc("Deployment", "aiops-bff"))["command"]
        self.assertIn("bff.app:app", command)
        self.assertIn("--proxy-headers", command)

    def test_webhook_launch(self):
        command = _container_of(_find_doc("Deployment", "aiops-webhook"))["command"]
        self.assertIn("alert_webhook.py", command)
        self.assertIn("0.0.0.0", command)

    def test_env_from_configmap_and_secret(self):
        for name in self.BUSINESS:
            container = _container_of(_find_doc("Deployment", name))
            refs = container["envFrom"]
            with self.subTest(deployment=name):
                self.assertIn({"configMapRef": {"name": "aiops-config"}}, refs)
                self.assertIn({"secretRef": {"name": "aiops-secrets"}}, refs)


class K8sConfigTest(unittest.TestCase):
    """ConfigMap / Secret / Ingress / Temporal / MySQL 关键配置。"""

    def test_configmap_keys(self):
        data = _find_doc("ConfigMap", "aiops-config")["data"]
        self.assertEqual(data["AIOPS_MODE"], "production")
        self.assertEqual(data["TEMPORAL_ADDRESS"], "aiops-temporal:7233")
        self.assertEqual(data["AIOPS_METRICS_PORT"], "9090")
        self.assertEqual(data["AIOPS_WEBHOOK_PORT"], "8099")
        self.assertEqual(data["AIOPS_DATA_ROOT"], "/var/lib/aiops")

    def test_secret_is_placeholder_only(self):
        secret = _find_doc("Secret", "aiops-secrets")
        self.assertEqual(secret["type"], "Opaque")
        values = secret["stringData"]
        self.assertTrue(values)
        for key, value in values.items():
            with self.subTest(key=key):
                self.assertIn("REPLACE_ME", value, f"{key} 必须为 REPLACE_ME 占位")

    def test_ingress_tls_and_cert_manager(self):
        ingress = _find_doc("Ingress", "aiops-bff")
        self.assertEqual(ingress["spec"]["ingressClassName"], "nginx")
        self.assertTrue(ingress["spec"]["tls"], "Ingress 缺少 TLS 配置")
        self.assertEqual(ingress["spec"]["tls"][0]["secretName"], "aiops-bff-tls")
        annotations = ingress["metadata"]["annotations"]
        self.assertIn("cert-manager.io/cluster-issuer", annotations)
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/ssl-redirect"], "true")

    def test_temporal_mysql_backend_and_retention(self):
        env = {e["name"]: e.get("value") for e in _container_of(_find_doc("Deployment", "aiops-temporal"))["env"]}
        self.assertEqual(env["DB"], "mysql8")
        self.assertEqual(env["MYSQL_SEEDS"], "aiops-mysql")
        self.assertEqual(env["DEFAULT_NAMESPACE_RETENTION"], "720h")

    def test_mysql_initdb_creates_temporal_databases(self):
        init_sql = _find_doc("ConfigMap", "aiops-mysql-initdb")["data"]["init.sql"]
        self.assertIn("temporal", init_sql)
        self.assertIn("temporal_visibility", init_sql)
        self.assertIn("GRANT ALL PRIVILEGES", init_sql)
        sts = _find_doc("StatefulSet", "aiops-mysql")
        self.assertEqual(sts["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"], "10Gi")


class K8sCronjobTest(unittest.TestCase):
    """数据清理 CronJob（P4-07）。"""

    def test_schedule_and_command(self):
        cron = _find_doc("CronJob", "aiops-cleanup")
        self.assertEqual(cron["spec"]["schedule"], "0 3 * * *")
        self.assertEqual(cron["spec"]["concurrencyPolicy"], "Forbid")
        container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["python", "-m", "aiops_agent.cleanup"])


class K8sNetworkPolicyTest(unittest.TestCase):
    """default-deny + 白名单（P4-06）。"""

    def test_default_deny_all(self):
        doc = _find_doc("NetworkPolicy", "aiops-default-deny")
        self.assertEqual(doc["spec"]["podSelector"], {})
        self.assertEqual(set(doc["spec"]["policyTypes"]), {"Ingress", "Egress"})
        self.assertNotIn("ingress", doc["spec"])
        self.assertNotIn("egress", doc["spec"])

    def test_whitelists(self):
        by_name = {}
        for _f, doc in _k8s_docs():
            if doc["kind"] == "NetworkPolicy":
                by_name[doc["metadata"]["name"]] = doc
        # ingress-nginx → BFF
        nginx = by_name["aiops-allow-ingress-nginx-to-bff"]
        self.assertEqual(nginx["spec"]["podSelector"]["matchLabels"]["app"], "aiops-bff")
        self.assertEqual(nginx["spec"]["ingress"][0]["ports"][0]["port"], 8600)
        # monitoring → webhook
        am = by_name["aiops-allow-alertmanager-to-webhook"]
        self.assertEqual(am["spec"]["ingress"][0]["ports"][0]["port"], 8099)
        # monitoring → 指标抓取（worker 9090 / bff 8600）
        scrape = by_name["aiops-allow-monitoring-scrape"]
        ports = {p["port"] for p in scrape["spec"]["ingress"][0]["ports"]}
        self.assertEqual(ports, {9090, 8600})
        # DNS 与同命名空间互访
        self.assertIn("aiops-allow-dns", by_name)
        self.assertIn("aiops-allow-intra-namespace", by_name)


class K8sMonitoringResourcesTest(unittest.TestCase):
    """ServiceMonitor / PrometheusRule（与静态告警文件同步）。"""

    def test_service_monitors(self):
        docs = _k8s_docs()
        monitors = {d["metadata"]["name"]: d for _f, d in docs if d["kind"] == "ServiceMonitor"}
        self.assertEqual(set(monitors), {"aiops-worker", "aiops-bff"})
        self.assertEqual(monitors["aiops-worker"]["spec"]["endpoints"][0]["port"], "metrics")
        self.assertEqual(monitors["aiops-bff"]["spec"]["endpoints"][0]["path"], "/metrics")

    def test_prometheus_rule_matches_static_alerts(self):
        rule_doc = _find_doc("PrometheusRule", "aiops-self-health")
        k8s_alerts = {
            r["alert"] for g in rule_doc["spec"]["groups"] for r in g["rules"]
        }
        static = yaml.safe_load((PROM_DIR / "aiops-alerts.yml").read_text(encoding="utf-8"))
        file_alerts = {r["alert"] for g in static["groups"] for r in g["rules"]}
        self.assertEqual(k8s_alerts, file_alerts)
        self.assertEqual(len(file_alerts), 6)


class PrometheusConfigTest(unittest.TestCase):
    """deploy/prometheus：scrape 配置与告警规则。"""

    def test_scrape_targets(self):
        cfg = yaml.safe_load((PROM_DIR / "prometheus.yml").read_text(encoding="utf-8"))
        jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
        self.assertIn("aiops-bff", jobs)
        self.assertIn("aiops-worker", jobs)
        self.assertIn("host.docker.internal:8600", jobs["aiops-bff"]["static_configs"][0]["targets"])
        self.assertIn(":9090", jobs["aiops-worker"]["static_configs"][0]["targets"][0])
        self.assertIn("/etc/prometheus/aiops-alerts.yml", cfg["rule_files"])

    def test_alert_rules_wellformed(self):
        static = yaml.safe_load((PROM_DIR / "aiops-alerts.yml").read_text(encoding="utf-8"))
        rules = [r for g in static["groups"] for r in g["rules"]]
        self.assertEqual(len(rules), 6)
        for rule in rules:
            with self.subTest(alert=rule.get("alert")):
                self.assertTrue(rule.get("expr"))
                self.assertTrue(rule.get("for"))
                self.assertIn(rule.get("labels", {}).get("severity"), {"critical", "warning"})


class GrafanaTest(unittest.TestCase):
    """deploy/grafana：dashboard 与 provisioning 一致性。"""

    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads((GRAFANA_DIR / "aiops-overview.json").read_text(encoding="utf-8"))

    @staticmethod
    def _iter_datasource_uids(node):
        if isinstance(node, dict):
            ds = node.get("datasource")
            if isinstance(ds, dict) and ds.get("uid"):
                yield ds["uid"]
            for value in node.values():
                yield from GrafanaTest._iter_datasource_uids(value)
        elif isinstance(node, list):
            for item in node:
                yield from GrafanaTest._iter_datasource_uids(item)

    def test_dashboard_panels(self):
        panels = self.dashboard["panels"]
        self.assertGreaterEqual(len(panels), 8)
        for panel in panels:
            with self.subTest(panel=panel.get("title")):
                self.assertIn(panel["type"], {"stat", "timeseries"})
                self.assertTrue(panel["targets"][0]["expr"])

    def test_all_panels_use_aiops_prom_uid(self):
        uids = set(self._iter_datasource_uids(self.dashboard))
        self.assertEqual(uids, {"aiops-prom"})

    def test_panels_cover_core_metrics(self):
        exprs = " ".join(t["expr"] for p in self.dashboard["panels"] for t in p["targets"])
        for metric in [
            "aiops_workflow_completed_total",   # 工作流成功率 / 终态分布
            "aiops_workflow_duration_seconds_bucket",  # 耗时 p95
            "aiops_gate_events_total",          # 闸门分布
            "aiops_sandbox_runs_total",
            "aiops_canary_deploys_total",
            "aiops_bff_requests_total",
        ]:
            with self.subTest(metric=metric):
                self.assertIn(metric, exprs)

    def test_datasource_provisioning_uid_matches(self):
        ds_cfg = yaml.safe_load(
            (GRAFANA_DIR / "provisioning" / "datasources" / "prometheus.yml").read_text(encoding="utf-8")
        )
        entry = ds_cfg["datasources"][0]
        self.assertEqual(entry["uid"], "aiops-prom")
        self.assertEqual(entry["type"], "prometheus")

    def test_dashboard_provider_path(self):
        prov = yaml.safe_load(
            (GRAFANA_DIR / "provisioning" / "dashboards" / "dashboards.yml").read_text(encoding="utf-8")
        )
        provider = prov["providers"][0]
        self.assertEqual(provider["options"]["path"], "/var/lib/grafana/dashboards")
        self.assertEqual(provider["type"], "file")


class DockerComposeProfilesTest(unittest.TestCase):
    """docker-compose：默认行为不变（temporal+loki），prod/obs 为可选 profile。"""

    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        cls.services = cls.compose["services"]

    def test_default_services_without_profile(self):
        defaults = [name for name, svc in self.services.items() if "profiles" not in svc]
        self.assertEqual(sorted(defaults), ["loki", "temporal"])

    def test_prod_profile(self):
        for name in ["mysql", "redis"]:
            with self.subTest(service=name):
                self.assertEqual(self.services[name]["profiles"], ["prod"])
        self.assertEqual(self.services["mysql"]["image"], "mysql:8.0")
        self.assertIn("healthcheck", self.services["mysql"])
        self.assertIn("redis:7", self.services["redis"]["image"])

    def test_obs_profile_and_mounts(self):
        for name in ["prometheus", "grafana"]:
            with self.subTest(service=name):
                self.assertEqual(self.services[name]["profiles"], ["obs"])
        # Prometheus 挂载规则与 scrape 配置
        prom_volumes = self.services["prometheus"]["volumes"]
        self.assertTrue(any("aiops-alerts.yml" in v for v in prom_volumes))
        self.assertTrue(any("prometheus.yml" in v for v in prom_volumes))
        # Grafana 挂载 provisioning 与面板 JSON（路径需真实存在）
        grafana_volumes = self.services["grafana"]["volumes"]
        self.assertTrue(any("grafana/provisioning" in v for v in grafana_volumes))
        self.assertTrue(any("aiops-overview.json" in v for v in grafana_volumes))

    def test_compose_referenced_files_exist(self):
        checks = {
            "prometheus.yml": PROM_DIR / "prometheus.yml",
            "aiops-alerts.yml": PROM_DIR / "aiops-alerts.yml",
            "aiops-overview.json": GRAFANA_DIR / "aiops-overview.json",
            "provisioning": GRAFANA_DIR / "provisioning",
        }
        for label, path in checks.items():
            with self.subTest(file=label):
                self.assertTrue(path.exists(), f"{label} 不存在：{path}")


class DockerfileTest(unittest.TestCase):
    """构建文件：多阶段 + 非 root + 三入口共用。"""

    @classmethod
    def setUpClass(cls):
        cls.content = DOCKERFILE.read_text(encoding="utf-8")

    def test_multistage_build(self):
        self.assertIn("FROM node:20-slim AS web", self.content)
        self.assertIn("FROM python:3.12-slim AS runtime", self.content)
        self.assertIn("COPY --from=web /build/dist ./web/dist", self.content)

    def test_non_root_user(self):
        self.assertRegex(self.content, r"USER aiops")

    def test_entrypoints_covered(self):
        # worker 默认 CMD + BFF / webhook 入口（注释中给出运行方式）
        self.assertIn('CMD ["python", "-m", "aiops_agent.worker"]', self.content)
        self.assertIn("uvicorn bff.app:app", self.content)
        self.assertIn("alert_webhook.py", self.content)

    def test_no_secrets_baked(self):
        # 镜像构建不得写入真实令牌 / 密码
        for pattern in [r"REPLACE_ME", r"password\s*=\s*['\"]?[a-zA-Z0-9]{8,}"]:
            self.assertIsNone(re.search(pattern, self.content), f"疑似密钥写入 Dockerfile：{pattern}")


if __name__ == "__main__":
    unittest.main()
