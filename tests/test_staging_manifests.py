"""M4 预生产清单校验：YAML 语法、隔离策略、最小权限与占位值边界。

本测试只读 `deploy/staging/` 下的声明式清单，不连集群、不安装任何东西；
它保证「清单本身」自洽（可解析、引用文件存在、默认拒绝、无通配权限、
策略阈值未被放宽、占位值不会漏进真实清单）。真实 k3s + gVisor + ArgoCD
验收必须在预生产主机执行，见 deploy/staging/README.md 第 6 节。
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

STAGING = Path(__file__).resolve().parents[1] / "deploy" / "staging"
# 允许出现占位值的文件：凭证示例、待替换的仓库地址、由发布流程改写镜像的示例应用
PLACEHOLDER_FILES = {"secret.example.yaml", "application.yaml", "rollout.yaml"}
# 非 K8s 对象：策略文件由 ConfigMap 挂载，单独校验其内容
CONFIG_FILES = {"policy.staging.yaml"}


def load_docs(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        docs = [doc for doc in yaml.safe_load_all(handle) if doc]
    for doc in docs:
        if not isinstance(doc, dict):
            raise AssertionError(f"{path.name} 存在非映射文档：{type(doc).__name__}")
    return docs


def all_docs() -> list[tuple[Path, dict]]:
    return [
        (path, doc)
        for path in sorted(STAGING.rglob("*.yaml"))
        if path.name not in CONFIG_FILES
        for doc in load_docs(path)
    ]


def by_kind(kind: str) -> list[tuple[Path, dict]]:
    return [(path, doc) for path, doc in all_docs() if doc.get("kind") == kind]


def named(kind: str, name: str) -> dict:
    for _path, doc in by_kind(kind):
        if doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"未找到 {kind}/{name}")


class ManifestSyntaxTest(unittest.TestCase):
    def test_every_manifest_parses_with_identity_fields(self):
        docs = all_docs()
        self.assertGreaterEqual(len(docs), 15)
        for path, doc in docs:
            self.assertTrue(doc.get("apiVersion"), f"{path.name} 缺 apiVersion")
            self.assertTrue(doc.get("kind"), f"{path.name} 缺 kind")
            if doc["kind"] == "Kustomization":
                continue  # kustomize 配置不是 K8s 对象，无 metadata
            self.assertIn("name", doc.get("metadata") or {}, f"{path.name} 缺 metadata.name")

    def test_kustomization_references_existing_files(self):
        root = STAGING / "kustomization.yaml"
        (doc,) = load_docs(root)
        self.assertEqual(doc["kind"], "Kustomization")
        for entry in doc["resources"]:
            self.assertTrue((STAGING / entry).is_file(), f"kustomization 引用了不存在的 {entry}")
        # app/ 由 ArgoCD 独占同步，secret/policy 走命令行注入
        joined = " ".join(doc["resources"])
        for excluded in ("app/", "secret.example.yaml", "policy.staging.yaml"):
            self.assertNotIn(excluded, joined)

    def test_placeholder_absent_outside_examples(self):
        for path, _doc in all_docs():
            if path.name in PLACEHOLDER_FILES:
                continue
            self.assertNotIn("REPLACE_ME", path.read_text(encoding="utf-8"), f"{path.name} 残留占位值")

    def test_rollout_placeholder_only_in_image(self):
        """示例应用的占位值只允许出现在镜像字段，由发布流程按 patch_id 改写。"""
        (rollout, _stable, _canary) = load_docs(STAGING / "app" / "rollout.yaml")
        container = rollout["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("REPLACE_ME", container["image"])
        others = [
            line
            for line in (STAGING / "app" / "rollout.yaml").read_text(encoding="utf-8").splitlines()
            if "REPLACE_ME" in line and not line.lstrip().startswith("image:")
        ]
        self.assertEqual(others, [])


class IsolationTest(unittest.TestCase):
    def test_namespaces_declared_with_psa_on_sandbox(self):
        names = {doc["metadata"]["name"] for _path, doc in by_kind("Namespace")}
        self.assertEqual(names, {"aiops-staging", "aiops-sandbox"})
        labels = named("Namespace", "aiops-sandbox")["metadata"]["labels"]
        self.assertEqual(labels["pod-security.kubernetes.io/enforce"], "restricted")

    def test_runtimeclass_is_gvisor_runsc(self):
        doc = named("RuntimeClass", "gvisor")
        self.assertEqual(doc["handler"], "runsc")
        self.assertNotIn("scheduling", doc)  # 单节点不得绑定 nodeSelector

    def test_sandbox_denies_all_traffic_including_dns(self):
        doc = named("NetworkPolicy", "sandbox-default-deny")
        self.assertEqual(doc["metadata"]["namespace"], "aiops-sandbox")
        self.assertEqual(doc["spec"]["podSelector"], {})
        self.assertEqual(sorted(doc["spec"]["policyTypes"]), ["Egress", "Ingress"])
        # 默认拒绝：不得出现任何 allow 规则（含 DNS）
        self.assertNotIn("ingress", doc["spec"])
        self.assertNotIn("egress", doc["spec"])

    def test_staging_default_deny_then_allowlist(self):
        denied = named("NetworkPolicy", "staging-default-deny")
        self.assertEqual(sorted(denied["spec"]["policyTypes"]), ["Egress", "Ingress"])
        allows = [
            doc["metadata"]["name"]
            for _path, doc in by_kind("NetworkPolicy")
            if doc["metadata"]["namespace"] == "aiops-staging"
            and doc["metadata"]["name"] != "staging-default-deny"
        ]
        for required in (
            "staging-allow-dns",
            "staging-allow-intra-namespace",
            "staging-allow-egress-web",
            "staging-allow-egress-apiserver",
        ):
            self.assertIn(required, allows)

    def test_egress_allowlist_is_port_scoped(self):
        """出命名空间（ipBlock / 外部 namespaceSelector）的出网必须限定端口。"""
        for _path, doc in by_kind("NetworkPolicy"):
            if doc["metadata"]["namespace"] != "aiops-staging":
                continue
            for rule in doc["spec"].get("egress") or []:
                peers = rule.get("to") or []
                # 同命名空间互访（podSelector 且无 namespaceSelector）不跨边界，不限端口
                cross = [
                    peer
                    for peer in peers
                    if "ipBlock" in peer
                    or (peer.get("namespaceSelector") or {}).get("matchLabels")
                ]
                if not cross:
                    continue
                ports = [port["port"] for port in rule.get("ports") or []]
                self.assertTrue(ports, f"{doc['metadata']['name']} 的出网规则缺少端口限制")
                for port in ports:
                    self.assertIn(port, {53, 443, 3100, 4318})

    def test_apiserver_egress_present_for_sandbox_runner(self):
        doc = named("NetworkPolicy", "staging-allow-egress-apiserver")
        rule = doc["spec"]["egress"][0]
        self.assertEqual(rule["to"][0]["ipBlock"]["cidr"], "10.43.0.0/16")
        self.assertEqual(rule["ports"][0]["port"], 443)


class LeastPrivilegeTest(unittest.TestCase):
    def test_roles_carry_no_wildcards_or_exec(self):
        roles = by_kind("Role") + by_kind("ClusterRole")
        self.assertTrue(roles)
        for path, doc in roles:
            for rule in doc["rules"]:
                self.assertNotIn("*", rule.get("verbs", []), f"{path.name} 使用了通配动词")
                self.assertNotIn("*", rule.get("resources", []), f"{path.name} 使用了通配资源")
                self.assertNotIn("pods/exec", rule.get("resources", []))
                self.assertNotIn("escalate", rule.get("verbs", []))

    def test_sandbox_role_is_namespace_scoped(self):
        role = named("Role", "aiops-sandbox-runner")
        self.assertEqual(role["metadata"]["namespace"], "aiops-sandbox")
        verbs = {verb for rule in role["rules"] for verb in rule["verbs"]}
        self.assertEqual(verbs, {"create", "get", "list", "watch", "delete"})
        resources = {res for rule in role["rules"] for res in rule["resources"]}
        self.assertTrue(resources <= {"jobs", "pods", "pods/log", "secrets"})

    def test_rollout_reader_is_read_only(self):
        role = named("Role", "aiops-rollout-reader")
        verbs = {verb for rule in role["rules"] for verb in rule["verbs"]}
        self.assertEqual(verbs, {"get", "list", "watch"})

    def test_bindings_point_at_worker_service_account(self):
        bindings = by_kind("RoleBinding") + by_kind("ClusterRoleBinding")
        self.assertEqual(len(bindings), 2)
        for path, doc in bindings:
            for subject in doc["subjects"]:
                self.assertEqual(subject["kind"], "ServiceAccount", f"{path.name} 绑定了非 SA 主体")
                self.assertEqual(subject["name"], "aiops-worker")
                self.assertEqual(subject["namespace"], "aiops-staging")
            self.assertNotIn("cluster-admin", doc["roleRef"]["name"])

    def test_no_cluster_scope_binding_anywhere(self):
        """只允许命名空间级 RoleBinding：集群级绑定会越过沙箱边界。"""
        self.assertEqual(by_kind("ClusterRoleBinding"), [])
        self.assertEqual(by_kind("ClusterRole"), [])
        for _path, doc in by_kind("RoleBinding"):
            self.assertNotEqual(doc["roleRef"]["kind"], "ClusterRole")


class QuotaAndPolicyTest(unittest.TestCase):
    def test_sandbox_quota_caps_concurrency_and_resources(self):
        quota = named("ResourceQuota", "sandbox-quota")
        hard = quota["spec"]["hard"]
        self.assertEqual(int(hard["count/jobs.batch"]), 20)
        self.assertEqual(int(hard["pods"]), 40)
        self.assertEqual(hard["requests.cpu"], "4")
        self.assertEqual(hard["requests.memory"], "8Gi")

    def test_limit_range_matches_executor_limits(self):
        limit = named("LimitRange", "sandbox-limits")["spec"]["limits"][0]
        self.assertEqual(limit["default"]["memory"], "256Mi")
        self.assertEqual(limit["default"]["cpu"], "1")
        self.assertEqual(limit["default"]["ephemeral-storage"], "1Gi")

    def test_secret_example_contains_only_placeholders(self):
        doc = named("Secret", "aiops-secrets")
        self.assertEqual(doc["type"], "Opaque")
        for key, value in doc["stringData"].items():
            self.assertIn("REPLACE_ME", str(value), f"{key} 不是占位值")
        self.assertEqual(
            set(doc["stringData"]),
            {
                "AIOPS_DATABASE_URL",
                "AIOPS_CONSOLE_AUTH_TOKENS",
                "AIOPS_ARGOCD_TOKEN",
                "AIOPS_GIT_TOKEN",
                "AIOPS_QODER_TOKEN",
            },
        )

    def test_staging_policy_keeps_quality_gates(self):
        policy = yaml.safe_load((STAGING / "policy.staging.yaml").read_text(encoding="utf-8"))
        gate = policy["release_gate"]
        self.assertIs(gate["shadow_mode"], True)  # 预生产默认只建议不执行
        self.assertIs(gate["require_real_tests_for_high_risk"], True)
        self.assertEqual(gate["triage"]["confidence_threshold"], 0.8)  # 不得放宽
        self.assertEqual(gate["canary"]["traffic_percent"], 5)
        self.assertIs(gate["canary"]["auto_rollback"], True)


class DeliveryTest(unittest.TestCase):
    def test_rollout_steps_are_weight_pause_analysis(self):
        docs = load_docs(STAGING / "app" / "rollout.yaml")
        rollout = docs[0]
        self.assertEqual(rollout["kind"], "Rollout")
        canary = rollout["spec"]["strategy"]["canary"]
        self.assertEqual(canary["stableService"], "demo-app-stable")
        self.assertEqual(canary["canaryService"], "demo-app-canary")
        weights = [step["setWeight"] for step in canary["steps"] if "setWeight" in step]
        self.assertEqual(weights, [5, 50, 100])
        analyses = [step["analysis"] for step in canary["steps"] if "analysis" in step]
        self.assertEqual(len(analyses), 2)
        for analysis in analyses:
            self.assertEqual(analysis["templates"][0]["templateName"], "demo-app-slo")
        services = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Service"}
        self.assertEqual(services, {"demo-app-stable", "demo-app-canary"})

    def test_analysis_template_matches_release_slo(self):
        (doc,) = load_docs(STAGING / "app" / "analysis-template.yaml")
        metrics = {metric["name"]: metric for metric in doc["spec"]["metrics"]}
        self.assertTrue(metrics["error-rate"]["successCondition"].endswith("<= 0.05"))
        self.assertTrue(metrics["p99-latency"]["successCondition"].endswith("<= 500"))
        for metric in metrics.values():
            address = metric["provider"]["prometheus"]["address"]
            self.assertTrue(address.startswith("http://prometheus.observability.svc.cluster.local"))
        self.assertNotIn("REPLACE_ME", (STAGING / "app" / "analysis-template.yaml").read_text(encoding="utf-8"))

    def test_application_targets_staging_app_path(self):
        doc = named("Application", "demo-app-staging")
        self.assertEqual(doc["metadata"]["namespace"], "argocd")
        self.assertEqual(doc["spec"]["source"]["path"], "deploy/staging/app")
        self.assertEqual(doc["spec"]["destination"]["namespace"], "aiops-staging")
        # 自动化同步与「手工同步」注释不得同时存在（避免控制器与人工互相覆盖）
        text = (STAGING / "application.yaml").read_text(encoding="utf-8")
        self.assertNotIn("sync-mode: manual", text)

    def test_readme_states_unverified_real_cluster(self):
        text = (STAGING / "README.md").read_text(encoding="utf-8")
        for required in ("AIOPS_SANDBOX_RUNTIME", "AIOPS_RELEASE_EXECUTOR", "AIOPS_ARGOCD_URL", "未验证", "回滚"):
            self.assertIn(required, text)
        # 不得把真实集群验收写成已完成
        self.assertNotIn("已在预生产验证通过", text)


if __name__ == "__main__":
    unittest.main()
