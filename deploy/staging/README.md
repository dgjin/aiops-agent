# 预生产环境（staging）清单

M4 交付物：把「沙箱真实执行 + 集群金丝雀发布」所需的 Linux 预生产环境固化为可审阅的清单。
本目录只包含声明式清单与操作步骤，**不含任何真实凭证**；`REPLACE_ME` 必须替换为真实值后
才能用于验收，占位值下的任何"通过"结论都不作数。

## 0. 已验证 / 未验证边界

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| 本目录 YAML 语法与 kustomize 渲染 | 已验证 | `tests/test_staging_manifests.py`（`yaml.safe_load` + 资源清单断言） |
| 沙箱加固项（非 root、禁提权、只读、配额、超时回收、零测试拦截） | 已验证 | `tests/test_sandbox_hardening.py`（22 例） |
| K8s Job 六类路径（成功/测试失败/镜像失败/超时/日志读取/清理） | 已验证（fake client） | `tests/test_k8s_sandbox.py`（16 例） |
| ArgoCD 状态机与集群发布判定 | 已验证（fake client） | `tests/test_argo_status.py`、`tests/test_release_cluster.py` |
| k3s + runsc 真实内核隔离、真实 ArgoCD 同步、真实金丝雀回滚 | **未验证** | 需在预生产主机执行下文第 6 节，并把结果回填到验收文档 |

无预生产主机时，不要把第 6 节标记为完成；代码侧能力已由单测覆盖，环境侧必须实机核对。

## 1. 资源基线与拆分（单机 k3s）

计划口径 **8 vCPU / 16 GiB / 80 GiB** 磁盘，按职责拆分，避免沙箱与平台互相抢资源：

| 组件 | CPU 预留 | 内存预留 | 磁盘 | 说明 |
| --- | --- | --- | --- | --- |
| k3s 控制面 + 节点组件 | 1 | 1.5 GiB | 10 GiB | 单节点 server |
| Temporal + MySQL（独立实例） | 2 | 4 GiB | 30 GiB | 与生产库物理隔离，禁止共用生产 DSN |
| BFF + Worker + 控制台静态资源 | 2 | 3 GiB | 10 GiB | Worker 副本数 1，M6 再做并发压测 |
| Loki + Prometheus（预生产独享） | 1 | 4 GiB | 20 GiB | 指标/日志不与生产后端混用 |
| ArgoCD + Rollouts 控制器 | 1 | 2 GiB | 5 GiB | 只管理 `aiops-staging` 与 `demo-app-staging` |
| 沙箱（Job，按需） | 1 | 1 GiB | 5 GiB | 上限由 `sandbox-quota.yaml` 硬性封顶 |

沙箱配额（`sandbox-quota.yaml`）：并发 Job ≤20、Pod ≤40、Request 合计 4 CPU / 8 GiB；
超出时 Job 直接 Pending，Worker 侧按超时失败处理，不降级到「跳过测试」。

## 2. 前置安装

### 2.1 k3s + gVisor（runsc）

```bash
# 1) 安装 gVisor：优先 apt 仓库（官方推荐，避免手工拼装 runsc 与 containerd-shim-runsc-v1）
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] \
https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list >/dev/null
sudo apt-get update && sudo apt-get install -y runsc
runsc --version   # 记录版本号，作为本环境的基线；升级需重新走一次第 6 节验收

# 无法使用 apt 时改为官方 tar 包（内含 runsc、containerd-shim-runsc-v1 与 gvisor-bin/，三者必须同目录）：
#   URL=https://storage.googleapis.com/gvisor/releases/release/latest/$(uname -m)
#   wget ${URL}/gvisor.tar.zstd ${URL}/gvisor.tar.zstd.sha512 && sha512sum -c gvisor.tar.zstd.sha512
#   sudo tar --zstd -xf gvisor.tar.zstd -C /usr/local/bin

# 2) 让 k3s 的 containerd 注册 runsc（k3s 每次启动都会按模板重新渲染 config.toml）
sudo tee /var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl >/dev/null <<'EOF'
{{ template "base" . }}

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
EOF

# 3) 启动 k3s（版本号以 https://github.com/k3s-io/k3s/releases 为准）并确认节点 Ready
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.30.4+k3s1 sh -
sudo systemctl restart k3s
sudo k3s kubectl get node
```

RuntimeClass 由 `runtimeclass.yaml` 声明为 `gvisor`，与 `sandbox.py` 的
`runtimeClassName: gvisor` 一致；handler 名必须与上面 `runtime_type` 的
`io.containerd.runsc.v1` 对应，改名前先确认两边同名，否则 Job 会被 API 拒绝。
containerd 2.x 需把模板头改成 `version = 3` 的等价写法，升级前先在节点上
`crictl runp --runtime runsc` 验证。

### 2.2 ArgoCD 与 Argo Rollouts

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.12.3/manifests/install.yaml
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts -f https://raw.githubusercontent.com/argoproj/argo-rollouts/v1.7.2/manifests/install.yaml

# 只读服务账号：Worker 用它查询 Application 与 Rollout，回滚走同一个 Application API
argocd account create aiops-worker --auth-token
```

Worker 需要的最小权限：Application 的 `get/list`、`sync`、`rollback`。令牌写入
`Secret/aiops-secrets` 的 `AIOPS_ARGOCD_TOKEN`，不进镜像、不进日志。

### 2.3 多节点扩展（单节点可跳过）

单节点 k3s 上沙箱与控制面共用内核，gVisor 只隔离系统调用、不隔离 CPU 争抢。
节点数 ≥2 时，把沙箱固定到独立节点池：

```bash
kubectl label node <sandbox-node> aiops.io/runtime=gvisor
kubectl taint node <sandbox-node> aiops.io/sandbox=true:NoSchedule
```

`runtimeclass.yaml` 不写 nodeSelector（单节点会无法调度）；固定节点池需要同时在
`aiops_agent/sandbox.py` 的 `render_manifest` 增加 `nodeSelector`/`tolerations`，
改动后必须补单测并跑全量回归，不允许只改集群侧。

## 3. 应用顺序

```bash
# 3.1 命名空间 / RuntimeClass / RBAC / NetworkPolicy / 沙箱配额 / ArgoCD Application
kubectl apply -k deploy/staging/

# 3.2 真实凭证（从 secret.example.yaml 派生，禁止提交）
kubectl -n aiops-staging create secret generic aiops-secrets \
  --from-literal=AIOPS_DATABASE_URL='mysql+pymysql://...' \
  --from-literal=AIOPS_CONSOLE_AUTH_TOKENS='...' \
  --from-literal=AIOPS_ARGOCD_TOKEN='...' \
  --from-literal=AIOPS_GIT_TOKEN='...' \
  --from-literal=AIOPS_QODER_TOKEN='...'

# 3.3 策略文件：作为 ConfigMap 挂载后由 AIOPS_POLICY_PATH 指向
kubectl -n aiops-staging create configmap aiops-policy \
  --from-file=policy.staging.yaml=deploy/staging/policy.staging.yaml

# 3.4 演示应用（由 ArgoCD 按 Git 同步，不要手动 kubectl apply）
kubectl -n argocd get application demo-app-staging
```

`kustomization.yaml` 故意不包含 `secret.example.yaml`、`policy.staging.yaml` 与 `app/`：
凭证走命令行注入，策略走 ConfigMap 挂载，`app/` 由 ArgoCD 独占同步，
避免 kubectl 与 ArgoCD 双控制器抢同一份工作负载。

## 4. Worker 配置（环境变量）

Worker Deployment（本仓库 `deploy/k8s/worker.yaml`）在 staging 需覆盖为：

| 变量 | 值 | 作用 |
| --- | --- | --- |
| `AIOPS_SANDBOX_RUNTIME` | `k8s` | 选择 `K8sJobSandboxRunner`（默认 `docker`） |
| `AIOPS_SANDBOX_NAMESPACE` | `aiops-sandbox` | 与 `sandbox-quota.yaml` / RoleBinding 一致 |
| `AIOPS_SANDBOX_IMAGE` | `python:3.12-slim@sha256:<digest>` | production 强制摘要，缺 `@sha256:` 直接失败 |
| `AIOPS_SANDBOX_LOADER_IMAGE` | `busybox:1.36@sha256:<digest>` | initContainer 解包工作区 |
| `AIOPS_RELEASE_EXECUTOR` | `argocd` | 显式选择集群发布（缺省会因 `AIOPS_ARGOCD_URL` 自动选择） |
| `AIOPS_ARGOCD_URL` | `https://argocd.aiops-staging.internal` | 未配置时发布退化为留痕模式，`_run_cluster_canary` 直接拒绝 |
| `AIOPS_ROLLOUT_NAME` | `demo-app` | 配置后金丝雀观测附带 Rollout 进度 |
| `AIOPS_ROLLOUT_NAMESPACE` | `aiops-staging` | 同上 |
| `AIOPS_POLICY_PATH` | `/etc/aiops/policy.staging.yaml` | 指向 3.3 的 ConfigMap 挂载 |
| `AIOPS_POLICY_SHA256` | 策略文件摘要 | 固定快照，防止运行中被改 |

Worker Pod 必须 `serviceAccountName: aiops-worker` 且与 `rbac.yaml` 同命名空间
（`aiops-staging`）。若把 Worker 放在 `aiops-system`，需同步修改 RoleBinding 的
`subjects[].namespace`，否则沙箱 RBAC 不生效（表现为 403，而不是"测试通过"）。

沙箱 Pod 自身不需要 ServiceAccount：清单已设 `automountServiceAccountToken: false`
且未指定 `serviceAccountName`，默认 SA 无任何权限。

## 5. 沙箱侧自检

```bash
# 5.1 gVisor 是否真的生效（期望内核版本以 gVisor 开头）
kubectl -n aiops-sandbox run probe --rm -it --restart=Never \
  --image=python:3.12-slim --overrides='{"spec":{"runtimeClassName":"gvisor"}}' \
  -- uname -a

# 5.2 网络是否真的被封死（必须超时或被拒，不能返回 200）
kubectl -n aiops-sandbox run probe-egress --rm -it --restart=Never \
  --image=busybox:1.36 --overrides='{"spec":{"runtimeClassName":"gvisor"}}' \
  -- wget -T 3 -qO- http://example.com

# 5.3 配额与清理：并发超过 20 个 Job 时新 Job 应停在 Pending
kubectl -n aiops-sandbox get resourcequota sandbox-quota -o yaml
```

单测覆盖的执行路径（`tests/test_k8s_sandbox.py`）与本机不连集群；上表命令用于
确认「清单声明」与「集群实际」一致，二者都通过才算环境就绪。

## 6. 端到端验收（必须在预生产主机执行）

### 6.1 一次健康发布

1. 用演示应用打一个真实变更补丁（`demo-app`），让流程走到 `release` 阶段；
2. 控制台期望状态序列：`SUBMITTED → SYNCING → SYNCED → HEALTHY`；
3. `kubectl -n argocd get application demo-app-<patch_id> -o jsonpath='{.status.health.status}'` 应为 `Healthy`；
4. `kubectl -n aiops-staging get rollout demo-app -o wide` 应从 5% 推进到 100%，两个 pause 点均留下 AnalysisRun 记录；
5. 控制台该次发布的 `slo_source=argocd-rollout`，错误率/P99 显示为「未知」而不是 0（本层不采集数值）。

### 6.2 一次真实劣化回滚

1. 在 canary 阶段人为注入劣化（例如让 canary Pod 返回 5xx）；
2. `AnalysisRun` 应在 `failureLimit` 内失败，Rollout 自动中止并回滚；
3. 控制台状态应为 `DEGRADED` 后 `ROLLED_BACK`，发布记录带「自动回滚」留痕；
4. `kubectl -n aiops-staging rollout history` 可按 revision 回溯，回滚请求参数为 `revision=0`（上一版本）；
5. 若 `auto_rollback=false`，流程必须停在待人工确认，不允许静默继续。

任一步骤未通过时，在验收文档中记为「未通过 + 现象 + 排查」，不得改写为通过。

## 7. 备份与恢复演练

```bash
# 7.1 预生产库每日逻辑备份（独立 DSN，不触碰生产）
mysqldump --single-transaction --routines --triggers \
  --defaults-extra-file=/etc/aiops/staging.cnf aiops_staging | gzip > /backup/aiops_staging_$(date +%F).sql.gz

# 7.2 精简恢复：只恢复到本次演练的临时实例，验证 schema 与关键表行数
zcat /backup/aiops_staging_$(date +%F).sql.gz | mysql -h 127.0.0.1 -P 3317 staging_restore_check

# 7.3 演练记录：备份耗时、恢复耗时、关键表行数差异、脱敏检查结论
```

恢复目标为「本次演练的临时实例」，不复用生产实例、不自动删库/删卷。
仓库内 `scripts/backup.py` 的受限恢复流程（部署手册第 10 章）仍是唯一自动化入口。

## 8. 常见故障

| 现象 | 判定 | 处置 |
| --- | --- | --- |
| Job `Pending`，事件 `exceeded quota` | 配额到顶（并发 >20） | 等前序 Job 清理；不接受调大配额来"通过" |
| Job `Failed`，事件 `no runtime for "gvisor"` | 节点未注册 runsc | 回到 2.1 检查 `config.toml.tmpl` 与 `systemctl restart k3s` |
| 沙箱 403 `cannot create resource "jobs"` | RoleBinding 主体命名空间不匹配 | 按第 4 节核对 `aiops-worker` 所在命名空间 |
| 发布停在 `SUBMITTED` | ArgoCD 未配置或 Application 不存在 | 检查 `AIOPS_ARGOCD_URL`、`kubectl -n argocd get application` |
| `STATIC_ONLY` 被判为通过 | 策略未开 `require_real_tests_for_high_risk` | 以 `policy.staging.yaml` 覆盖，且不得放宽阈值到 0.8 以下 |
| 金丝雀一直 `Progressing` | `AIOPS_ROLLOUT_NAME` 未配或 AnalysisRun 卡住 | 查 `kubectl -n aiops-staging get analysisrun` |

## 9. 与代码的对应关系

| 清单文件 | 代码位置 |
| --- | --- |
| `sandbox-quota.yaml` / `runtimeclass.yaml` / `networkpolicy.yaml` | `aiops_agent/sandbox.py` 的 `K8sJobSandboxRunner` |
| `rbac.yaml`（sandbox Role） | `sandbox.py` 的 `_create_secret` / `_ensure_job` / `_read_logs` / `_cleanup` |
| `rbac.yaml`（rollout-reader） | `aiops_agent/argo_status.py` 的 `rollout_state` |
| `application.yaml` + `app/rollout.yaml` | `aiops_agent/release.py` 的 `_cluster_app_name` / `_run_cluster_canary` |
| `policy.staging.yaml` | `aiops_agent/config.py` 的策略加载与 `workflows._static_verification_block` |

改动清单时同步跑：

```bash
.venv/bin/python -m unittest discover -s tests -t .
```
