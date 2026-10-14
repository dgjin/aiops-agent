# AIOps 自动运维智能体系统部署手册

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.1 |
| 日期 | 2026-10-12 |
| 适用对象 | SRE / 实施工程师 / 环境维护人员 |
| 关联文档 | 《系统设计说明书》、《用户操作手册》 |
| 修订记录 | v1.7（2026-10-14）：飞书卡片回调网关（审批交互落地）——`POST /api/im/card-callback` 接收 `card.action.trigger` 回调并直通 Temporal 审批信号（Verification Token 验签、挑战握手、加密载荷拒绝、200+toast 响应含处置结果卡）；新增环境变量 `AIOPS_FEISHU_VERIFY_TOKEN`（未配置＝通道关闭）与飞书后台配置步骤（见 3.9，原 3.9 MCP 顺延为 3.10）；测试基线 1331 用例；v1.1（2026-10-12）：同步最新工程现状——BFF 控制台（0.0.0.0:8600，支持局域网访问）、可用性巡检 / 日志采集 / 组件守护、需求基线闭环、Qoder 修复引擎、模型默认值切换（qwen3.8:27b-mlx）、测试基线 836 用例；v1.2（2026-10-12）：运维治理（沙箱清理 / 日志轮转 / 数据备份与恢复演练 / launchd 开机自启）、watchdog 单实例锁与档位锁定；v1.3（2026-10-12）：P1 生产就绪续轮——Temporal dev server 持久化语义实测文档化（§2.4.1）、watchdog RSS 内存守护（AIOPS_WATCHDOG_RSS_MB，doctor「组件内存」检查项）、沙箱产物独立卷 env 化（AIOPS_SANDBOX_ROOT / AIOPS_QODER_ROOT，代码/清理/doctor/K8s 同源）、测试基线 857 用例；v1.4（2026-10-13）：IM 流程通知补齐（接收/需求接收/进入修复卡片，activity 13→16）、测试基线 1289 用例；v1.5（2026-10-13）：生产档切换全链路执行（`release-gate-policy.yaml` 显式 `shadow_mode: false`；生产档「必须显式声明档位」校验保留），策略变更后须 `./start.sh restart` 受控重启、测试基线 1290 用例；v1.6（2026-10-13）：影子建议续接（`resume-shadow` 端点复用影子档补丁续接完整链路）；发布层门禁按策略档位判定（影子档禁止真实发布 / 全链路放行；已注册直连型应用 `deploy_canary` 放行）、测试基线 1305 用例 |

---

## 1 部署形态总览

系统支持两种部署形态，接口与业务语义完全一致，仅外部依赖的接线方式不同：

| 维度 | 本机演示形态（本文第 2-4 章） | 生产形态（第 5 章） |
| --- | --- | --- |
| 工作流编排 | Temporal dev server（`localhost:7233`） | Temporal 集群 + 持久化存储 |
| LLM | Ollama 本机（`localhost:11434`） | Ollama / 推理服务 |
| 日志/告警 | 复用本机 Loki + Alertmanager（可选） | Prometheus + Alertmanager + Loki/ELK |
| 沙箱 | Docker 容器隔离（`--network none` 等） | K8s Job + gVisor（manifest 已就绪） |
| 发布 | ArgoCD 留痕 + Docker 真实滚动 | ArgoCD 真实 API + Argo Rollouts |
| 通知 | 留痕模式 / 真实群机器人 webhook | 飞书/钉钉 + 回调网关 |

> 本手册第 2-4 章为**默认路径**：一台 macOS/Linux 机器即可完成全链路部署与验证；第 5 章给出生产切换的接线点清单。

### 1.1 运行时组件清单

| 组件 | 必需性 | 版本要求 | 默认端口 |
| --- | --- | --- | --- |
| Python | 必需 | 3.12（实测 3.12.0） | — |
| Temporal 服务端 | 必需 | temporal CLI 或 Docker 镜像 | 7233 |
| Ollama | 必需（LLM 链路） | 最新稳定版 | 11434 |
| Docker Engine / Desktop | 必需（沙箱与发布链路） | 支持 `docker build/run/port` | — |
| Loki | 可选（真实日志链路） | 任意 2.x | 3101（本机栈避让 Grafana 的 3100） |
| Alertmanager | 可选（告警自动触发） | 任意 0.2x | 9093 |
| 稳定容器 | 发布链路必需 | 由 demo-app 构建 | 18080 |

> 此外，系统自身有 **6 个常驻组件**（worker、BFF 控制台 `:8600`、告警接入 `:8099`、可用性巡检、日志采集、突增检测），并由 `scripts/aiops-watchdog.py` 组件守护看护（每 30 秒巡检、崩溃自动拉起，可经 launchd 托管实现登录自启，见 7.6）；日常演示建议用 `scripts/demo-up.sh` 一键启动（见 3.1）。

---

## 2 本机演示环境部署（完整步骤）

### 2.0 快速安装清单（TL;DR）

```bash
# ① 前置：Python 3.12 / Docker / temporal CLI / Ollama 就绪（见 2.1）
cd "aiops-agent"
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install faiss-cpu            # 可选：FAISS 加速后端（未装则自动回退纯 Python）

# ② Temporal（独立终端常驻）
temporal server start-dev

# ③ Ollama 模型
ollama pull qwen3.8:27b-mlx   # triage / 修复默认模型（约 18GB；轻量替代 qwen3:8b）
ollama pull bge-m3:latest

# ④ 索引构建
.venv/bin/python index_codebase.py

# ⑤ 稳定容器初始化
docker build --label aiops.version=v0.0.0 -t aiops-demo-app:base demo-app
docker run -d --name aiops-stable-order -e APP_VERSION=v0.0.0 \
  -p 127.0.0.1:18080:8000 aiops-demo-app:base

# ⑥ 前端构建（控制台 SPA；产出 web/dist 由 BFF 同源托管）
cd web && npm install && npm run build && cd ..

# ⑦ Worker 启动（演示策略）
export AIOPS_POLICY_PATH=./demo-policy.yaml
.venv/bin/python -m aiops_agent.worker

# ⑧ 冒烟（另开终端，同样 export 策略）
.venv/bin/python -m unittest discover -s tests -t . -v       # 期望 1331 用例全绿
.venv/bin/python demo_cli.py start --service order --alert-id smoke-001
```

> **更省事的方式**：`bash scripts/demo-up.sh` 一键拉起全部 6 个常驻组件与组件守护（幂等，已运行组件自动跳过；演示播种用 `demo` 子命令，仅 demo 档）——详见 3.1。

### 2.1 前置检查

```bash
python3 --version            # 期望 3.12.x
docker info                  # Docker 守护进程在运行
temporal --version           # 若未安装：brew install temporal（macOS）
ollama --version             # 若未安装：见 https://ollama.com/download
curl -s localhost:11434/api/tags | head -c 200   # Ollama 服务可用
```

### 2.2 代码与虚拟环境

```bash
cd "aiops-agent"                  # 工程根（含 aiops_agent/、demo_cli.py、requirements.txt）
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
```

### 2.3 依赖安装

```bash
.venv/bin/pip install -r requirements.txt      # 单一清单：temporalio / pydantic / fastapi / uvicorn / python-dotenv 等
.venv/bin/pip install faiss-cpu                # 可选：向量检索加速后端
```

| 依赖 | 必要性 | 说明 |
| --- | --- | --- |
| `temporalio>=1.7.0` | 必需 | 工作流 SDK |
| `pydantic>=2.5.0` | 必需 | 策略与 LLM 输出强校验 |
| `PyYAML>=6.0.1` | 必需 | 策略解析 |
| `drain3>=0.9.11` | 必需 | 日志模板聚类 |
| `fastapi` / `uvicorn` / `pymysql` / `redis` / `python-dotenv` | 必需（控制台/生产链路） | BFF 控制台、MySQL/Redis 后端、`.env` 自动加载 |
| `faiss-cpu` | 可选 | 向量检索 FAISS 后端；未安装自动回退纯 Python（功能等价，仅规模性能差异） |

> 说明：沙箱与金丝雀发布使用 Docker CLI（subprocess 调用），无需 Python 侧额外依赖；沙箱镜像 `python:3.12-slim` 首次运行时自动拉取或提前 `docker pull python:3.12-slim`。

### 2.4 Temporal 服务端

**方式 A：本地 CLI（推荐用于演示）**

```bash
temporal server start-dev
# 控制台 http://localhost:8233
```

**方式 B：Docker**

```bash
docker run --rm -p 7233:7233 -p 8233:8233 temporalio/temporal \
  server start-dev --ip 0.0.0.0
```

远程/自定义地址通过 `TEMPORAL_ADDRESS` 指定（worker 与 CLI 均生效，默认 `localhost:7233`）。

#### 2.4.1 dev server 语义（2026-10-12 实测确认）

本机演示形态接受 `temporal server start-dev`（Docker 版落 SQLite）。以下边界均为本机实测结论，作为「可接受的本地语义」明示：

| 语义 | 实测结论 | 说明与命令 |
| --- | --- | --- |
| 重启持久化 | ✅ 历史不丢 | SQLite 落命名卷（`/home/temporal/temporal.db`）；`docker restart aiops-temporal` 前后工作流清单逐条一致（实测 5/5） |
| 历史保留期 | ⚠️ 默认 24h 静默清理 | dev server 默认命名空间 `WorkflowExecutionRetentionTtl=24h`，超期历史会被清理；`scripts/demo-up.sh` 环境准备阶段**幂等校准为 168h（7 天）**，与仓库数据清理默认对齐。手动校准：`docker exec aiops-temporal temporal operator namespace update -n default --retention 168h`（目标值可用 `AIOPS_TEMPORAL_RETENTION` 覆盖） |
| 高可用 | ❌ 单节点 | 无副本 / 无故障转移；生产切第 5 章集群形态（`deploy/k8s/temporal.yaml`：MySQL 后端 + retention 720h） |
| 归档（Archival） | ❌ 未启用 | 超期历史直接清理、无冷归档；长期审计以 BFF 磁盘留痕为准（`data/web-audit`、`data/argocd`、`data/notify` 等，不依赖 Temporal 历史） |
| 备份 / 恢复 | 卷或文件级 | 备份：`docker cp aiops-temporal:/home/temporal/temporal.db backups/`（容器停止更稳妥）；恢复：容器停止后回填 `temporal.db` 再启动 |

> **结论**：本地形态的语义基线＝「重启不丢、保留 7 天、单点可接受」。演示与单机试点足够；对多副本 / 长保留 / 归档有要求的场景，按第 5 章切生产形态。

### 2.5 Ollama 与模型

```bash
# 启动服务（若未常驻）
ollama serve &

# 必需模型
ollama pull qwen3.8:27b-mlx   # triage / 修复默认模型（约 18GB，Apple Silicon；实测沙箱通过率 1.0）
ollama pull bge-m3:latest     # 代码嵌入模型（约 1.2GB，1024 维）

# 可选：轻量模型（内存有限或追求速度时切换；小模型 diff 语义质量下降）
ollama pull qwen3:8b          # 约 5.2GB；export AIOPS_TRIAGE_MODEL=qwen3:8b 等切换
```

模型清单可用 `ollama list` 核验。模型可通过环境变量切换（见第 4 章）。

### 2.6 策略配置

工程内置两份策略（结构完全一致，仅时长不同）：

| 文件 | 用途 | 关键时长 |
| --- | --- | --- |
| `release-gate-policy.yaml` | 生产/默认策略 | 审批超时 30m、倒计时 15m、观测 5m；`shadow_mode` 档位（当前 `false` 全链路；`true`=影子档禁止真实发布） |
| `demo-policy.yaml` | 演示加速策略 | 审批超时 300s、倒计时 30s、观测 10s |

**使用规则（重要）**：

1. 策略在进程启动时加载为**只读快照**，运行期不热修改；
2. 演示时 worker、BFF、告警接入服务、CLI 必须使用**同一份**策略：
   ```bash
   export AIOPS_POLICY_PATH=./demo-policy.yaml    # 未设置时使用默认 release-gate-policy.yaml
   ```
3. 非法配置会在启动阶段直接报错退出（绝不带病启动）。锁定项受校验器强制，示例错误：
   ```text
   锁定策略：审批超时仅允许自动驳回（on_timeout 必须为 reject），默认安全侧
   锁定策略：窗口内新补丁仅允许排队（strategy 必须为 queue）
   并发底线：同一时刻全局仅允许一个发布执行（max_active_deployments 必须为 1）
   ```

### 2.7 索引构建（Code RAG）

```bash
.venv/bin/python index_codebase.py                     # 默认：demo-app → data/code_index.*
.venv/bin/python index_codebase.py --query "coupon null 空指针" --top-k 3   # 建库并立即冒烟检索
```

预期输出（实测）：`索引完成：files=8 chunks=33 tickets=6 dim=1024 model=bge-m3:latest`，产物：

- `data/code_index.json`（全量元数据 + 向量）
- `data/code_index.faiss`（FAISS 后端可用时生成；未装 faiss-cpu 则不生成，检索自动走纯 Python）

**重建时机**：demo-app 源码或历史工单库（`data/historical_tickets.json`）变更后。

### 2.8 稳定容器初始化（发布链路前置）

金丝雀达标后会滚动到稳定容器 `aiops-stable-order`（`:18080`）。首次部署需初始化一个基线版本：

```bash
docker build --label aiops.version=v0.0.0 -t aiops-demo-app:base demo-app
docker run -d --name aiops-stable-order \
  -e APP_VERSION=v0.0.0 -p 127.0.0.1:18080:8000 aiops-demo-app:base
curl -s http://127.0.0.1:18080/health    # {"status":"ok","version":"v0.0.0",...}
```

> 之后每次「全量发布」由系统自动停旧容器、以金丝雀镜像启动新稳定容器；无需手工干预。

### 2.9 启动 Worker

```bash
cd aiops-agent
export AIOPS_POLICY_PATH=./demo-policy.yaml     # 演示；生产用默认策略时可不设
nohup .venv/bin/python -m aiops_agent.worker > worker.log 2>&1 &
```

预期日志：

```text
策略快照已加载（./demo-policy.yaml），confidence_threshold=0.8, max_fix_retries=2, approval_timeout=0:05:00, ...
Worker 已启动：task_queue=aiops-tasks, temporal=localhost:7233
```

> 后台启动时可能出现 macOS 的 `nice(5) failed: operation not permitted` 提示——无害，worker 正常工作。
> 重启 worker：`pkill -f aiops_agent.worker` 后重新启动。

### 2.10 启动 BFF 控制台（图形界面）

Web 控制台（流程看板 / 审批中心 / 发布窗口 / 审计 / 被监控应用管理 / 需求基线等）由 BFF（FastAPI + SPA 同源托管）提供；未启动 BFF 时全部操作仍可用 CLI 完成。

```bash
cd aiops-agent
# 最小配置：播种控制台访问令牌（fail-closed：未配置则 /api 全部 503）
export AIOPS_CONSOLE_AUTH_TOKENS='{"tok-view":{"user":"viewer01","role":"viewer"},"tok-ops":{"user":"zhang","role":"operator"},"tok-adm":{"user":"li","role":"admin"}}'
nohup .venv/bin/python -m uvicorn bff.app:app --host 0.0.0.0 --port 8600 > bff.log 2>&1 &
```

- 访问：本机 **http://127.0.0.1:8600**；局域网设备 **http://<本机IP>:8600**（`--host 0.0.0.0` 监听全部网卡，无需额外做端口转发）；
- 前置：`web/dist` 已构建（见 2.0 ⑥），否则首页提示缺前端产物；
- 令牌：`AIOPS_CONSOLE_AUTH_TOKENS` 仅作**首次播种**，运行期注册表在 `data/console_tokens.json`（demo）或 MySQL（production，仅存 sha256 指纹）；自动轮换与宽限期见 4.1；
- 实测（2026-10-12）：局域网地址访问首页 200、静态资源 200、`/api` 无令牌 401（鉴权正常；静态页与 `/assets` 无需鉴权）；`/metrics` 无鉴权，生产以网络策略限制暴露面（见 5.2）。

> 建议直接用 `bash scripts/demo-up.sh` 一键启动（自动含 BFF，见 3.1）。

### 2.11 冒烟验证

```bash
# ① 单元测试（期望：Ran 1331 tests ... OK；必须带 -t .，防止误收集 data/ 下演示样例）
.venv/bin/python -m unittest discover -s tests -t . -v

# ② 向量检索冒烟（FAISS/Python 双后端一致性）
.venv/bin/python index_codebase.py --query "空指针 coupon" --top-k 3

# ③ 端到端冒烟（流程命令见《用户操作手册》第 2 章）
export AIOPS_POLICY_PATH=./demo-policy.yaml
.venv/bin/python demo_cli.py start --service order --alert-id smoke-001
.venv/bin/python demo_cli.py status --wf-id aiops-fix-order-smoke-001
.venv/bin/python demo_cli.py approve --wf-id aiops-fix-order-smoke-001
.venv/bin/python demo_cli.py deploy-now --wf-id aiops-fix-order-smoke-001
.venv/bin/python demo_cli.py result --wf-id aiops-fix-order-smoke-001   # stage=DONE
```

---

## 3 可选组件部署

### 3.1 一键启动与组件守护（推荐）

本地（production / demo 档均可）推荐用一键脚本编排全部常驻组件（替代逐一手工启动 3.2-3.5 各组件）：

```bash
./start.sh                         # 受控启动（推荐入口）：依赖与端口预检后拉起 6 常驻组件与组件守护（无隐式动作）
./start.sh web                     # 额外在前台启动前端 vite dev（热更，5273；Ctrl+C 只停前端）
./start.sh install                 # 将组件守护交 launchd 托管（登录自启；受控重装同命令）
./start.sh sync-ip                 # 按当前网络地址同步生产部署（Caddyfile/settings/监控片段 + 重签 TLS、reload Caddy；--dry-run 预览）
bash scripts/demo-up.sh            # 等价底层脚本（start.sh 即其薄封装）
bash scripts/demo-up.sh status     # 查看组件与守护状态、生效档位快照
bash scripts/demo-up.sh down       # 停止全部常驻组件（含组件守护；保留 launchd 注册）
bash scripts/demo-up.sh env        # 仅依赖与端口预检（不启动任何服务）
```

脚本档位=环境变量 > .env > production（缺省 production），启动日志如实打印有效配置快照；按序启动 worker → BFF(:8600) → 告警接入(:8099) → 可用性巡检 → 日志采集 → 突增检测 → **组件守护**（不自动灌日志/起流程；演示播种用 `bash scripts/demo-up.sh demo`，仅 demo 档）。

组件守护 `scripts/aiops-watchdog.py` 每 30 秒巡检 6 个常驻组件，异常自动拉起；崩溃风暴（窗口内 5 次）自动冷却 10 分钟。守护启动时获取单实例锁（`data/aiops-watchdog.lock`，防双守护）并锁定生效档位（环境变量 > .env > production），显式传给被拉起组件。参数：`AIOPS_WATCHDOG_INTERVAL`（巡检周期）、`AIOPS_WATCHDOG_SURGE_ARGS`（突增检测器拉起参数）。另按 RSS 内存水位守护（默认 1024MB，`AIOPS_WATCHDOG_RSS_MB` 可调，0=关闭）：超限即就地重启——macOS 无 cgroup 场景的组件软限额（P1，2026-10-12；`demo_cli.py doctor` 的「组件内存」检查项同口径）。

开机自启：`./start.sh install`（等价 `bash scripts/launchd-install.sh install`）将守护交由 launchd 托管（登录自启、崩溃自动重启，详见 7.6）。

### 3.2 告警接入服务（Alertmanager 自动触发）

```bash
cd aiops-agent
export AIOPS_POLICY_PATH=./demo-policy.yaml     # 与 worker 一致
export AIOPS_WEBHOOK_TOKEN="$(openssl rand -hex 24)"   # 推荐：/webhook 防伪造（production 未配置时 fail-closed，全部 503）
nohup .venv/bin/python alert_webhook.py --port 8099 > alert_webhook.log 2>&1 &
```

- 监听 `0.0.0.0:8099`，接口 `POST /webhook`（Alertmanager v4 载荷）；
- 鉴权：共享密钥 `AIOPS_WEBHOOK_TOKEN`——接入服务读该变量，Alertmanager 侧执行 `monitoring/apply_alertmanager_route.py --set-webhook-token <同一令牌>` 写入 receiver 后重启容器生效；另支持 `AIOPS_WEBHOOK_HMAC_SECRET`（HMAC 签名）与 `AIOPS_WEBHOOK_ALLOW_IPS`（来源 IP 白名单）；
- 限速：默认 120 次/分钟/来源 IP（`AIOPS_WEBHOOK_RATE_LIMIT`，0 关闭）；
- 手工验证：向 8099 投递一条 firing 载荷，观察日志「已启动 workflow」与重复投递「幂等拒绝」；
- 端口可用 `--port` 或 `AIOPS_WEBHOOK_PORT` 调整。

### 3.3 可用性巡检（页面白屏也能发现）

```bash
.venv/bin/python app_prober.py --once      # 单轮（调试/验证）
nohup .venv/bin/python app_prober.py > app_prober.log 2>&1 &   # 守护运行（默认 15s 一轮）
```

- 巡检「被监控应用」清单（控制台维护，热生效）：探测 `url` + `health_path`；连接失败/超时判不可达；页面级校验默认用**通用白屏判据**（响应非空 + HTML 完整闭合 + body 含可渲染容器），条目配置了 `probe_keyword` 时改为关键字精确校验；
- 连续失败达阈值（默认 3 次）→ 向 Alertmanager 注入标准告警 `AppUnreachable` → 经路由自动发起修复流程；恢复后注入 resolved 并复位（同一故障周期只告警一次，恢复后再次故障自动生成新 alert_id）；
- 状态落盘 `data/probe_status.json`，控制台「被监控应用」页透出巡检结果；
- 参数：`AIOPS_PROBE_INTERVAL_SECONDS` / `AIOPS_PROBE_FAILURE_THRESHOLD` / `AIOPS_PROBE_TIMEOUT_SECONDS` / `AIOPS_ALERTMANAGER_URL`（CLI 参数优先）。

### 3.4 日志实时采集（被监控应用 → Loki）

```bash
.venv/bin/python ship_app_logs.py --once               # 单轮增量
.venv/bin/python ship_app_logs.py --from-start --once  # 首次全量
nohup .venv/bin/python ship_app_logs.py --follow > ship_app_logs.log 2>&1 &   # 持续采集（每 2 秒一轮）
```

- 扮演 Filebeat / Fluent Bit 角色：按「被监控应用」清单取 `enabled` 且配置了 `log_path` 的条目，把应用日志规范化为 `service`/`level`（error/warn/info 三档）流标签并注入 `trace_id=`，推送 AIOps Loki（`AIOPS_LOKI_URL`）；
- 每轮重读清单 → 控制台新增/停用条目热生效；`log_path` 支持通配（覆盖轮转归档）；
- 位点落盘 `data/log_ship_positions.json`（按文件记录字节偏移），重启不重复推送；
- 单文件模式（兼容旧用法）：`--file <路径> --service <svc> --once`。

### 3.5 日志突增检测器（WP2 真实链路）

```bash
# 单轮验证（调试）
.venv/bin/python log_surge_detector.py --services nl2sql --once

# 守护运行（默认 15s 一轮）
nohup .venv/bin/python log_surge_detector.py --services nl2sql > log_surge_detector.log 2>&1 &
```

参数：`--interval 15`、`--window 1`、`--min-lines 100`、`--factor 3.0`、`--cooldown 300`、`--alertmanager http://localhost:9093`。
依赖 Loki（`AIOPS_LOKI_URL`）与 Alertmanager 可达；Loki 不可用时检测器会报错退出，属预期。

### 3.6 监控栈接入（复用现有 Prometheus/Alertmanager）

本系统默认复用本机既有监控栈（Loki 3101 / Alertmanager 9093）。接入两件事：

```bash
# ① 注入 Alertmanager 转发路由（幂等；自动备份原文件；锚点不符时拒绝修改）
.venv/bin/python monitoring/apply_alertmanager_route.py
docker restart nl2sql-monitoring-alertmanager-1      # 容器启动时重新渲染模板生效

# ② （可选）挂载告警规则样例：prometheus/aiops-demo-rules.yml
#    规则统一携带 aiops_demo="true" 标签，经子路由转发到 8099
#    挂载步骤见该文件头部注释（复制 → rule_files 追加 → volumes 追加 → 重启 prometheus）
```

### 3.7 通知真实投递（飞书 / 钉钉）

未配置 webhook 时通知为留痕模式（`data/notify/*.json`，`delivery=recorded`）。配置后真实投递：

```bash
# 飞书（默认 provider）
export AIOPS_NOTIFY_PROVIDER=feishu
export AIOPS_FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
export AIOPS_NOTIFY_SECRET="<加签密钥>"        # 可选（机器人开启签名校验时必填）

# 钉钉
export AIOPS_NOTIFY_PROVIDER=dingtalk
export AIOPS_DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxxx"
export AIOPS_NOTIFY_SECRET="<加签密钥>"

# 通用回退（两个 provider 都未配专用变量时使用）
export AIOPS_NOTIFY_WEBHOOK="https://..."
# 钉钉按钮回调基址（默认 http://127.0.0.1:8000/aiops/callback）
export AIOPS_NOTIFY_CALLBACK_BASE="https://<你的回调网关>/aiops/callback"
```

> 通知发送为「先留痕、后 POST」，投递结果以机器人应答码为准（飞书 `code` / 钉钉 `errcode` 非零 = `failed`，如加签不符 `19021`；网络异常同理）；任何失败均自动降级、不阻断修复流程。
> 本机生产环境推荐直接写入工程根 `.env`（`AIOPS_FEISHU_WEBHOOK=...` / `AIOPS_NOTIFY_SECRET=...`），修改后执行 `./start.sh restart` 受控重启生效（环境指纹变化触发）。

### 3.8 IM 通道（ChatOps：群内查询与审批）

把「看待办、查状态、批准/驳回」前移到飞书/钉钉工作群（操作入口前移）。BFF 内置消息入口 `POST /api/im/command`，**无需新增进程**：IM 机器人出口（网桥）把群消息 POST 到该端点，再把响应中的 `reply` 字段原样发回群即可。

```bash
# .env（或进程环境）：通道令牌 + 发送者角色映射（JSON）
AIOPS_IM_TOKEN=<openssl rand -hex 24>   # 通道鉴权；未配置 = 通道关闭（请求回落常规鉴权，fail-closed）
AIOPS_IM_USERS='{"ou_xxxxxxxx":"operator","ou_yyyyyyyy":{"role":"admin"}}'   # 未映射发送者 = viewer（仅可查询）

# 手工验证（通道令牌鉴权）
curl -X POST http://127.0.0.1:8600/api/im/command \
  -H "Authorization: Bearer $AIOPS_IM_TOKEN" -H "Content-Type: application/json" \
  -d '{"sender":"ou_xxxxxxxx","text":"待办"}'
# → {"ok":true,"reply":"待审批流程（…）…","server_time":…}
```

入站载荷兼容三种结构（自动规范化 + 去 @提及）：通用 `{"sender","text"}`、飞书事件 v2（`event.sender.sender_id.open_id` + `message.content`）、钉钉机器人回调（`senderStaffId`/`senderNick` + `text.content`）。

回复契约：命令级失败（权限不足 / 未找到流程 / 前置校验不过）也返回 200 + `ok:false`（`reply` 含原因，由网桥转回群）；仅「载荷无法解析 = 400」「系统级故障（kill switch / Temporal 不可达）= 5xx」走错误状态码。审批执行复用流程域同款写守卫；写命令受 kill switch 阻断（查询命令保留可用），操作审计 actor 记为 `im:<sender>`。

### 3.9 飞书卡片回调网关（审批交互，2026-10-14 落地）

审批卡片上的「批准 / 驳回」按钮点击后由飞书服务器回调本系统内置网关（**无需新增进程**，BFF 域 `bff/routes/im_callback.py`），全链：验签 → 解析按钮载荷与操作者身份 → 角色判定 → 写守卫 → Temporal 审批信号 → 响应 toast + 处置结果卡（就地替换按钮区，防重复点击）。

```bash
# .env：Verification Token（必填；未配置＝通道关闭 503）。
# 取值 = 飞书开放平台：事件与回调 → 加密策略 → Verification Token（卡片回调需明文模式，Encrypt Key 不启用）
AIOPS_FEISHU_VERIFY_TOKEN=<openssl rand -hex 24 或飞书后台既有 Token>
```

**飞书后台配置（开发者后台 → 应用 → 事件与回调 → 回调配置）**：

1. 订阅方式选「将回调发送至开发者服务器」，请求地址填 `https://<本机IP>:8643/api/im/card-callback`（生产档经 Caddy，见第 9 章；本机演示可填 `http://127.0.0.1:8600/api/im/card-callback`）；
2. 加密策略保持**明文模式**（启用 Encrypt Key 会被网关明确拒绝 400）；
3. 保存时飞书发起 `url_verification` 挑战握手，网关校验 Token 后回显 `challenge`——若报校验失败，先核对 `AIOPS_FEISHU_VERIFY_TOKEN` 与后台一致性再重启 BFF；
4. 仅需订阅新版卡片回调（`card.action.trigger`）；飞书后台「回调订阅」自动对卡片生效，无需额外订阅事件。

审批人身份取自回调载荷 `event.operator`（open_id/user_id/union_id 依次尝试），角色按 `AIOPS_IM_USERS` 映射判定——**未映射＝viewer（仅可查询），仅 operator 可审批**；审批只认发卡时嵌入的运行中真实流程 ID（旧卡片含公式 ID 的，走控制台/ChatOps 兼容）。手工验证：

```bash
TOKEN=$AIOPS_FEISHU_VERIFY_TOKEN; WF=aiops-fix-nl2sql-demo-1
curl -X POST http://127.0.0.1:8600/api/im/card-callback -H "Content-Type: application/json" -d "{
  \"schema\":\"2.0\",\"header\":{\"event_type\":\"card.action.trigger\",\"token\":\"$TOKEN\"},
  \"event\":{\"operator\":{\"open_id\":\"ou_xxx\"},\"action\":{\"tag\":\"button\",\"value\":{\"wf_id\":\"$WF\",\"gate\":\"approval\",\"decision\":\"approve\"}}}}"
# → {"toast":{"type":"success","content":"一级审批已批准（操作者 ou_xxx）"},"card":{"type":"raw",...}}（业务拒绝为 toast.error + HTTP 200）
```

> 响应策略：除凭证/格式错误（401/400）与通道关闭（503）外一律 HTTP 200 + toast（协议要求 3 秒内响应，5xx 会触发飞书重试）；业务拒绝（权限不足 / 阶段不符 / 重复提交 / kill switch）在 toast 中说明。操作审计 `actor=im:<operator>`、`action=card-callback:*`。

### 3.10 MCP server（能力对外暴露，可选）

把控制台能力以 MCP 工具形态暴露给外部 Agent 平台（Claude Desktop / Cursor 等）。实现为薄 HTTP 适配层（仓库根 `mcp_server.py`），全部调用转发 BFF `/api/*`——**不绕过**控制台鉴权 / 写守卫 / 审计 / kill switch；独立可选依赖，不装不影响主链路。

```bash
# 依赖（独立 requirements 文件）
.venv/bin/pip install -r requirements-mcp.txt

# 环境变量
export AIOPS_MCP_BASE_URL=http://127.0.0.1:8600   # 可选，默认即此值（指向 BFF）
export AIOPS_MCP_TOKEN=<控制台令牌>                 # 透传 BFF 鉴权；未配置时工具调用在 BFF 侧 401

# 运行方式 A：stdio（主模式；由 MCP 客户端本地拉起进程）
.venv/bin/python mcp_server.py
# 运行方式 B：streamable-http（默认 127.0.0.1:8700/mcp；--host/--port 可改）
.venv/bin/python mcp_server.py --http
```

客户端配置示例（stdio；`mcpServers` 通用形态）：

```json
{"mcpServers": {"aiops": {"command": "/绝对路径/aiops-agent/.venv/bin/python", "args": ["/绝对路径/aiops-agent/mcp_server.py"], "env": {"AIOPS_MCP_BASE_URL": "http://127.0.0.1:8600", "AIOPS_MCP_TOKEN": "<控制台令牌>"}}}}
```

> 安全语义（重要）：
> - `AIOPS_MCP_TOKEN` 直接决定能力边界：**只读工具用 viewer 令牌即可**，`approve_flow` / `second_approve_flow` 两个写工具要求 **operator 及以上**令牌（BFF 侧仍有写守卫与操作审计兜底）；
> - 9 个工具：`ops_overview` / `list_flows` / `get_flow` / `list_pending_approvals` / `list_escalations` / `query_audit` / `check_system_health`（只读）+ `approve_flow` / `second_approve_flow`（写）；
> - 错误语义：BFF 不可达 / 鉴权失败 / 写守卫拒绝均以 `is_error` 结果携带原因返回（不会静默失败）。

---

## 4 配置参考

### 4.1 环境变量全表

| 变量 | 默认值 | 生效组件 | 说明 |
| --- | --- | --- | --- |
| `TEMPORAL_ADDRESS` | `localhost:7233` | worker / webhook / CLI | Temporal 前端地址 |
| `AIOPS_POLICY_PATH` | `release-gate-policy.yaml` | worker / webhook / CLI | 策略文件路径（三者需一致） |
| `AIOPS_OLLAMA_URL` | `http://localhost:11434` | triage / code_rag | Ollama 地址 |
| `AIOPS_TRIAGE_MODEL` | `qwen3.8:27b-mlx` | triage / 需求分析 | 根因模型（import 时快照；轻量替代 `qwen3:8b`） |
| `AIOPS_FIX_MODEL` | 跟随 triage | fix_agent | 修复模型（import 时快照） |
| `AIOPS_EMBED_MODEL` | `bge-m3:latest` | code_rag | 嵌入模型（改后需重建索引） |
| `AIOPS_VECTOR_BACKEND` | `auto` | code_rag | `auto` / `faiss` / `python` |
| `AIOPS_SANDBOX_IMAGE` | `python:3.12-slim` | sandbox | 沙箱镜像 |
| `AIOPS_SANDBOX_ROOT` | `data/sandbox` | sandbox / cleanup / release | 沙箱产物根目录（生产建议指向独立卷/tmpfs，与 data/ 持久数据物理隔离；清理侧 `cleanup-sandbox.sh` 同源取值） |
| `AIOPS_LOKI_URL` | `http://localhost:3101` | logs / 检测器 | Loki 地址 |
| `AIOPS_WEBHOOK_PORT` | `8099` | alert_webhook | 接入服务端口 |
| `AIOPS_ARGOCD_URL` / `AIOPS_ARGOCD_TOKEN` | 空（留痕模式） | release | ArgoCD API 地址与令牌 |
| `AIOPS_CANARY_IMAGE_PREFIX` | `aiops-demo-app` | release | 镜像名前缀 |
| `AIOPS_STABLE_CONTAINER` / `AIOPS_STABLE_PORT` | `aiops-stable-order` / `18080` | release | 稳定容器名与端口 |
| `AIOPS_NOTIFY_PROVIDER` | `feishu` | notify | `feishu` / `dingtalk` |
| `AIOPS_FEISHU_WEBHOOK` / `AIOPS_DINGTALK_WEBHOOK` | 空 | notify | provider 专用 webhook（优先） |
| `AIOPS_NOTIFY_WEBHOOK` | 空 | notify | 通用回退 webhook |
| `AIOPS_NOTIFY_SECRET` | 空 | notify | 加签密钥（飞书秒级 / 钉钉毫秒级） |
| `AIOPS_NOTIFY_CALLBACK_BASE` | `http://127.0.0.1:8000/aiops/callback` | notify | 钉钉按钮回调基址 |
| `AIOPS_MODE` | `production` | 全部组件 | `production`（默认，fail-fast）/ `demo`（文件后端） |
| `AIOPS_DATABASE_URL` | 空（production 未配置则拒绝启动） | 全部组件 | MySQL 连接串（`mysql+pymysql://...?charset=utf8mb4`） |
| `AIOPS_DATA_ROOT` | 按模式（demo=`data/`，production=`data/prod/`） | 全部组件 | 数据根目录覆盖 |
| `AIOPS_CONSOLE_AUTH_TOKENS` | 空（fail-closed，`/api` 全部 503） | BFF | Bearer 令牌注册表 JSON（仅首次播种；运行期在 DB/文件注册表） |
| `AIOPS_REDIS_URL` | 空（退化为进程内限速窗口） | BFF | 限速窗口存储（多副本共享配额）；本机 compose 映射 6380 |
| `AIOPS_RATE_LIMIT_ENABLED` | 按模式（production=1 / demo=0） | BFF | 限速总开关（1/0 显式覆盖） |
| `AIOPS_RATE_LIMIT_READ_PER_MIN` / `_WRITE_PER_MIN` / `_AUTH_FAIL_PER_MIN` | `240` / `20` / `10` | BFF | 已认证读 / 已认证写 / 认证失败（按 IP）配额 |
| `AIOPS_METRICS_PORT` | `9091`（production）/ `9090`（demo） | worker | worker 独立指标服务端口（默认仅 loopback，0 关闭）；BFF 指标固定 `127.0.0.1:8600/metrics`（仅回环） |
| `AIOPS_OTEL_ENDPOINT` | 空（no-op） | worker / BFF | OpenTelemetry OTLP HTTP 端点（如 `http://localhost:4318`） |
| `AIOPS_GIT_TOKEN` | 空（MR 返回 recorded 桩） | worker | Git 平台令牌（配合策略 `git:` 段；未配置/失败自动降级不中断流程） |
| `AIOPS_CONSOLE_TOKEN_FILE` | `data/console_tokens.json`（demo） | BFF | 运行期令牌注册表（权限 600，含明文令牌，勿提交） |
| `AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS` / `_GRACE_SECONDS` / `_ROTATE_CHECK_SECONDS` | `2592000` / `604800` / `300` | BFF | 令牌轮换间隔（0=关闭）/ 旧令牌宽限 / 到期检查周期 |
| `AIOPS_TOKEN_ALLOW_REVEAL` | `false` | BFF | 是否允许经接口回显令牌值 |
| `NL2SQL_OPS_TOKEN` | 空（需求基线页给出中文指引） | BFF / analyze_requirements | 被监控系统「需求导出接口」访问令牌 |
| `AIOPS_TRACK_SERVICE_TOKEN` | 空（该通道关闭） | BFF | 需求进度跟踪只读令牌（独立注册表，不随轮换失效） |
| `AIOPS_WEBHOOK_TOKEN` | 空（production fail-closed） | alert_webhook | 告警接入共享密钥鉴权（P0-1） |
| `AIOPS_WEBHOOK_HMAC_SECRET` / `AIOPS_WEBHOOK_ALLOW_IPS` | 空 | alert_webhook | 可选 HMAC 签名密钥 / 来源 IP 白名单 |
| `AIOPS_WEBHOOK_RATE_LIMIT` | `120` | alert_webhook | 接入限速（次/分钟/来源 IP，0 关闭） |
| `AIOPS_PROBE_INTERVAL_SECONDS` / `_FAILURE_THRESHOLD` / `_TIMEOUT_SECONDS` | `15` / `3` / `2` | app_prober | 巡检间隔 / 连续失败阈值 / 单次探测超时 |
| `AIOPS_ALERTMANAGER_URL` | `http://localhost:9093` | app_prober | 巡检告警注入地址 |
| `AIOPS_WATCHDOG_INTERVAL` / `AIOPS_WATCHDOG_SURGE_ARGS` / `AIOPS_WATCHDOG_RSS_MB` | `30` / 演示灵敏度 / `1024` | aiops-watchdog | 组件守护巡检周期 / 突增检测器拉起参数 / 组件内存上限 MB（RSS 超限自动重启，0=关闭） |
| `AIOPS_MONITOR_URL` | `http://localhost:3000/` | prober / BFF | 被监控应用根地址（首次播种，之后在控制台维护） |
| `AIOPS_MONITOR_SERVICE` / `AIOPS_MONITOR_LOG_PATH` | `nl2sql` / 空 | ship_app_logs | 采集 service 标签与日志路径（首次播种） |
| `AIOPS_CODE_INDEX` | `data/code_index.json` | worker | Code RAG 索引路径（仅对未注册清单的 service 生效；已注册自动用专属索引） |
| `AIOPS_EMBED_BATCH_SIZE` | `64` | code_rag | 嵌入分批大小（大仓库拆分请求） |
| `AIOPS_FIX_PROVIDER` | `ollama` | worker | 修复提供者：`ollama`（本地 LLM）/ `qoder`（CLI 无头，见 4.3） |
| `AIOPS_QODER_MODEL` / `_TIMEOUT` / `_MAX_TURNS` | `DeepSeek-Flash` / `720` / `100` | worker（qoder） | Qoder 修复引擎参数（模型 ID 区分大小写，见 4.3） |
| `AIOPS_QODER_ROOT` | `./data/qoder` | worker（qoder） | Qoder 隔离工作区根目录 |
| `AIOPS_IM_TOKEN` | 空（通道关闭） | BFF | IM 通道（ChatOps）鉴权令牌，见 3.8 |
| `AIOPS_IM_USERS` | 空（全员 viewer） | BFF | 发送者角色映射 JSON（`{"<sender>":"operator"}` 或 `{"<sender>":{"role":"admin"}}`） |
| `AIOPS_FEISHU_VERIFY_TOKEN` | 空（通道关闭 503） | BFF | 飞书卡片回调验签 Token（= 飞书后台 Verification Token，明文模式），见 3.9 |
| `AIOPS_MCP_BASE_URL` | `http://127.0.0.1:8600` | mcp_server | MCP server 转发的 BFF 地址，见 3.10 |
| `AIOPS_MCP_TOKEN` | 空（工具调用 401） | mcp_server | MCP 侧透传的控制台令牌（决定能力边界），见 3.10 |

> 快照语义提醒：`AIOPS_TRIAGE_MODEL` / `AIOPS_FIX_MODEL` / `AIOPS_EMBED_MODEL` / `AIOPS_LOKI_URL` 在模块 import 时读取，**修改后需重启 worker**。
>
> 上表为常用变量摘编；**完整清单（含分组注释与生成示例）见仓库根 [`.env.example`](../.env.example)**——config.py 启动时自动加载仓库根 `.env`（显式 export 优先于 .env）。

### 4.2 策略文件键位

```yaml
release_gate:
  triage:
    confidence_threshold: 0.8    # 闸门 1：低于此值转人工
    max_fix_retries: 2           # 补丁回炉上限（总尝试 = 1 + 2）
  approval:
    timeout: 30m                 # 超时自动驳回（锁定：on_timeout 必须 reject）
    on_timeout: reject
    on_timeout_escalation: phone
    second_approval_dirs: [auth/**, payment/**, crypto/**]   # 受保护目录（前缀语义）
  notify_window:
    countdown: 15m               # 公告后延迟发布倒计时
    deploy_on_expiry: true       # 锁定：到期必须自动部署
    allow_deploy_now: true
    allow_cancel: true
    new_patch_during_window:     # 锁定：仅 FIFO 排队
      strategy: queue
      reset_timer: false
      merge_with_current: false
      queue_limit: 0
      next_cycle: auto
    feedback_circuit_breaker:    # 锁定：不熔断
      enabled: false
    concurrency:                 # 锁定：全局并发 1
      max_active_deployments: 1
  canary:
    traffic_percent: 5
    observe_duration: 5m
    health_metrics: [error_rate, p99_latency]
    auto_rollback: true
```

### 4.3 Qoder 修复引擎接入（可选）

修复默认由本地 Ollama 生成补丁；可切换为 **Qoder CLI 无头模式**（隔离工作区内自主修复，适合大范围改动；设计详见《Qoder 修复引擎接入设计》）：

```bash
curl -fsSL https://qoder.com/install | bash     # 安装 Qoder CLI（验证：qoder --version）
export AIOPS_FIX_PROVIDER=qoder
export AIOPS_QODER_TOKEN=<PAT>                  # 或 QODER_PERSONAL_ACCESS_TOKEN（CI/无人值守鉴权）
```

- 关键变量：`AIOPS_QODER_MODEL`（默认 `DeepSeek-Flash`，**模型 ID 区分大小写**，写错不报错且静默回退 auto——接入层写入 `meta.model_warning` 留痕）、`AIOPS_QODER_MAX_TURNS`（默认 100）、`AIOPS_QODER_TIMEOUT`（默认 720s）、`AIOPS_QODER_PERMISSION_MODE=accept_edits`、`AIOPS_QODER_ROOT`（隔离工作区，默认 `./data/qoder`）；
- 约束：`TIMEOUT + 快速重试预算 180s` 必须小于修复活动 Temporal 超时（960s，代码 `_FIX_ACTIVITY_TIMEOUT`），否则会被硬取消、无法降级；
- 目标文件缺失时 Qoder 走仓库级自主定位模式（不直接降级）；未安装/调用失败时安全侧兜底（等同既有降级路径），流程不中断。

---

## 5 生产化部署（企业级）

> 本章对应生产化改造（批次 b1-b9，已实施并验收）：单一代码库**双模运行**——`production`（默认）走 MySQL/Redis 持久化 + 全量鉴权/限速/指标，`demo` 保留零依赖文件后端与既有演示数据，两档互不影响、历史数据零迁移。

### 5.1 双模运行与数据面

| 维度 | production（默认） | demo |
| --- | --- | --- |
| 启用方式 | 不设变量或 `AIOPS_MODE=production` | `AIOPS_MODE=demo`（`scripts/demo-up.sh` 自动导出） |
| 关系数据 | MySQL 8.0 五表：`system_kv` / `console_tokens` / `monitored_apps` / `audit_events` / `workflow_runs` | 文件后端：`data/` 下 JSON / JSONL |
| 令牌注册表 | DB 存储 **sha256 指纹**（鉴权时二次匹配） | 文件明文键（文件优先于环境变量播种） |
| 数据根目录 | `data/prod/`（`AIOPS_DATA_ROOT` 可覆盖） | `data/` |
| 未配置 `AIOPS_DATABASE_URL` | **fail-fast 拒绝启动**（防静默丢数据） | 不触达数据库 |
| 限速 | 开（读 240/min、写 20/min、认证失败 10/min） | 关（可显式设 `AIOPS_RATE_LIMIT_ENABLED=1` 验证） |

- 建表：首次连接引擎自动执行 `metadata.create_all`（幂等）；MySQL 严格模式下 `TEXT` 列不允许 `DEFAULT`，`monitored_apps.note` 由写入路径显式赋空串（已适配）。
- 连接串固定驱动前缀 `mysql+pymysql`，含 `charset=utf8mb4`；连接池已内置 `pool_recycle=3600` + `pool_pre_ping` 防 MySQL 断连。
- 审计落库：production 写 `audit_events` 表（索引 ts / workflow_id / actor）；demo 写 `data/web-audit/audit-YYYYMMDD.jsonl`（两后端记录结构一致）。控制台「审计报表」支持按 actor / 动作 / 时间窗聚合。
- kill switch（紧急停止）：`POST /api/system/kill-switch`（admin）；`system_kv` 跨副本一致，激活后 BFF 拒绝一切写操作、webhook 拒绝新流程，状态不可读时 fail-closed。

### 5.2 Kubernetes 部署（推荐路径）

```bash
# ① 构建镜像（deploy/Dockerfile：多阶段构建、非 root、无内置密钥）
docker build -f deploy/Dockerfile -t aiops-agent:1.0.0 .
#    网络受限环境可换依赖源：追加 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# ② 准备密钥（复制后填入真实值；secret.yaml 已被 .gitignore 排除）
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml
#    需填：MySQL 口令 / Temporal / AIOPS_CONSOLE_AUTH_TOKENS / AIOPS_DATABASE_URL /
#         AIOPS_REDIS_URL /（可选）AIOPS_GIT_TOKEN

# ③ 一键部署（清单平铺于 deploy/k8s/，勿放入子目录——kubectl apply -f 不递归）
kubectl apply -f deploy/k8s/
kubectl -n aiops-system get pods -w
```

清单结构（12 文件 / 38 资源）与关键设计：

| 类别 | 资源 | 要点 |
| --- | --- | --- |
| 基础 | namespace / configmap / secret.example | 命名空间 `aiops-system`；非敏感配置入 ConfigMap |
| 数据面 | mysql（StatefulSet）/ redis | MySQL 含 initdb 脚本（建 `temporal` / `temporal_visibility` 库并授权）；10Gi volumeClaimTemplate |
| 编排 | temporal（auto-setup） | `DB=mysql8`、`DEFAULT_NAMESPACE_RETENTION=720h`（30 天）；保守单副本，可横向扩展 |
| 工作负载 | worker / bff / webhook | 各 2 副本 + PDB（minAvailable 1）+ HPA（CPU 70%，2→6）；镜像统一 `aiops-agent:1.0.0` |
| 共享存储 | PVC `aiops-data`（RWX 10Gi） | worker 与 cleanup CronJob 共享（索引/沙箱/留痕）；集群仅支持 RWO 时将 worker 降为 1 副本 |
| 入口 | ingress | `aiops.example.com` + cert-manager TLS（`aiops-bff-tls`）；TLS 在 Ingress 终结，BFF 按 `X-Forwarded-Proto` 下发 HSTS |
| 网络 | networkpolicy（8 条） | default-deny 起步 + DNS + 同命名空间互访 + ingress-nginx→BFF + monitoring→webhook/指标 + egress 80/443 + observability 3100/4318 |
| 定时 | cronjob `aiops-cleanup` | `0 3 * * *` 执行 `python -m aiops_agent.cleanup`；错误以非零退出码暴露在 Job 状态 |
| 监控 | servicemonitor + prometheusrule | worker 指标已收敛为容器内 loopback `:9091`，ServiceMonitor 旧直抓模板**未接线**（需另行设计同 Pod 私有 collector）；6 条告警与 `deploy/prometheus/aiops-alerts.yml` 同步（有测试校验名称集合一致） |

> **探针口径（批次 I 修订）**：`/metrics`、`/readyz`、`/livez` 只接受 loopback 对端，kubelet httpGet（非回环来源）会被拒——worker / BFF 探针已全部改为 **exec**：worker readiness 调用 `aiops_agent.readiness.check`（校验共享任务队列活跃 pollers）、liveness 仅检主进程存活；BFF readiness 容器内请求 `127.0.0.1:8600/readyz`、liveness `/livez`；webhook 保持 tcpSocket 8099。指标若需被抓取，只能通过同 Pod 私有 collector/代理，不能为抓取改回公开监听。**本轮未部署集群，探针契约仅经清单静态测试校验，未在集群实测。**

升级：`kubectl -n aiops-system set image deployment/aiops-worker worker=aiops-agent:<新版本>`（bff/webhook 同理），滚动更新由 PDB 保障可用性；回滚 `kubectl rollout undo`。

### 5.3 本机生产化运行（compose profiles）

```bash
cd aiops-agent
docker compose --profile prod up -d   # MySQL(3316) + Redis(6380)
# 端口 3316/6380 为避让宿主机常见 3306/6379 的约定值（宿主机已有同名服务时端口映射会被遮蔽）

export AIOPS_MODE=production
export AIOPS_DATABASE_URL='mysql+pymysql://aiops:aiops-demo-pass@127.0.0.1:3316/aiops?charset=utf8mb4'
export AIOPS_REDIS_URL='redis://127.0.0.1:6380/0'
export AIOPS_POLICY_PATH=./release-gate-policy.yaml
# 启动 worker / BFF 后执行生产冒烟（9 项：模式判定 / 建表 / 审计读写 / 令牌指纹鉴权 /
# 应用 CRUD / workflow_runs / kill switch / 清理预演，全部真实走 MySQL 链路）
.venv/bin/python scripts/production-smoke.py
```

可观测（Prometheus + Grafana，独立 profile；默认不启动、不影响演示栈）：

```bash
docker compose --profile obs up -d    # Prometheus http://127.0.0.1:9095 / Grafana http://127.0.0.1:3200（admin/aiops-demo）
```

自动加载 `deploy/grafana/aiops-overview.json`（9 面板：成功率 / 各 stage 速率 / 耗时 p95 / 闸门决策分布 / 修复提供者 / 沙箱通过率 / 金丝雀健康 / BFF 速率 / BFF p95，含 `mode` 模板变量）与 6 条告警规则。

### 5.4 生产化实测结论（批次 b9 验收记录）

- 生产冒烟：**9/9 通过**（真实 MySQL 建表、审计、令牌指纹鉴权、应用 CRUD、workflow_runs 落库、kill switch 读写、清理预演）；
- Production BFF 实测：带令牌 `health/flows` 200；无令牌 401；连续 10 次失败鉴权后第 11 次起 429；`X-Forwarded-Proto: https` 时下发 `Strict-Transport-Security: max-age=31536000; includeSubDomains`；Redis 限速键 `aiops:rl:tok:*` / `aiops:rl:authfail:*` 实际生成；
- 一致性：`workflow_runs` 表与 Temporal 工作流记录 1:1（同步任务实测）；
- SAST：Bandit HIGH=0（16 个 Medium 均为已知误报 / 设计需要，见 `scripts/security-scan.sh` 头注）；
- 镜像构建实测：`docker build` 成功（753MB / 压缩 168MB）；容器内：非 root（uid=10001）、`cleanup --dry-run` 正常、BFF `/metrics` 200 / 无令牌 401 / 带令牌 200（Temporal connected=true）、SPA 首页 200；
- 全量单测零回归（b9 验收时 471 用例；当前全量基线 1331 用例 / 49 个测试文件，含部署清单 39 项静态校验）。

### 5.5 生产接线点清单（按需启用）

| # | 组件 | 实施状态（2026-10） | 生产动作 |
| --- | --- | --- | --- |
| 1 | 沙箱 | Docker 容器真实隔离已落地；K8s Job manifest 渲染保留 | 集群内切换 gVisor runtimeClass 提交 `batch/v1 Job` |
| 2 | 发布 | ArgoCD 真实提交 + Docker 滚动金丝雀已落地 | 配置 `AIOPS_ARGOCD_URL/TOKEN`；镜像前缀指向制品仓库 |
| 3 | 通知 | 飞书/钉钉交互卡片 + **钉钉**回调解析已落地 | 飞书卡片回调网关**已落地**（`POST /api/im/card-callback`，见 3.9）；钉钉按钮回调需外置网关转发等价信号 |
| 4 | 公告 | 卡片投递 + 留痕已落地 | 接 WebSocket 网关推送站内弹窗 |
| 5 | 感知层 | 本机 Loki/Alertmanager 实测链路 | 接生产 Prometheus/Alertmanager/Loki/OTel；`aiops_demo` 标签改专用 receiver |
| 6 | MR | **已实现**三平台真实创建（GitLab/GitHub/Gitee，`aiops_agent/git_integration.py`） | 策略 `git:` 段配置 `repo_url` + `AIOPS_GIT_TOKEN` 注入 |
| 7 | 向量库 | FAISS 后端已落地（双后端一致） | 数据规模增大后升级 Milvus（接口同构） |
| 8 | 工作流 | K8s Temporal auto-setup（MySQL 后端，retention 720h） | 大规模场景换 Temporal Cloud / Cassandra |
| 9 | 时钟与时区 | 倒计时以服务端时间为准 | 集群 NTP 同步 |

---

## 6 验证清单（部署完成标准）

| # | 验证项 | 方法 | 通过标准 |
| --- | --- | --- | --- |
| 1 | 单元测试 | `.venv/bin/python -m unittest discover -s tests -t .` | 1331 用例全绿 |
| 2 | 策略校验 | 启动 worker | 打印策略快照；非法 YAML 启动即失败 |
| 3 | 向量检索 | `index_codebase.py --query ...` | 返回相似度有序的代码块/工单 |
| 4 | 双后端一致性（可选） | 分别以 `AIOPS_VECTOR_BACKEND=faiss/python` 检索同一 query | 结果与相似度一致 |
| 5 | 沙箱真实执行 | E2E 流程观察 `[sandbox]` 日志 | `隔离执行[docker-container] Ran N tests` |
| 6 | 金丝雀真实探活 | E2E 流程观察 `[canary]` 日志 | `观测窗口实测 错误率=x% P99=xms`（非模拟值） |
| 7 | 发布滚动 | `docker exec aiops-stable-order python -c "import os;print(os.environ.get('APP_VERSION'))"` 或 `curl :18080/health` | 版本号与最后发布的 `v1.0.x` 一致 |
| 8 | 通知留痕 | `ls data/notify/` | 流程各阶段产生 `intake-*/fix-start-*/approval-*/broadcast-*` 等留痕（含需求 `req-intake-*`、影子档 `shadow-*`、升级 `escalation-*`） |
| 9 | 端到端 | 按《用户操作手册》第 2 章跑通 | `stage=DONE`，`gate_events` 完整 |
| 10 | 异常分支 | `low-conf` / `canary-bad` 关键词各跑一次 | 分别 `ESCALATED`（闸门 1 / 回滚） |
| 11 | 生产冒烟（production 模式） | `.venv/bin/python scripts/production-smoke.py` | 9 项检查全绿（MySQL 建表/审计/令牌指纹/应用 CRUD/workflow_runs/kill switch/清理预演） |
| 12 | K8s 清单静态校验 | `.venv/bin/python -m unittest tests.test_deploy_manifests` | 39 用例（含 144 子断言）全绿 |
| 13 | 组件守护自愈 | `pkill -f app_prober.py` 后等待 ≤30 秒 | 巡检被自动拉起（`bash scripts/demo-up.sh status` 全绿） |
| 14 | 备份可恢复 | `.venv/bin/python scripts/backup-data.py && .venv/bin/python scripts/backup-data.py --restore-test` | 备份生成；演练临时库表数与源库一致 |
| 15 | 开机自启（可选） | `bash scripts/launchd-install.sh install` → `status` | `state = running` 且守护进程唯一 |

---

## 7 日常运维操作

### 7.1 启动顺序（推荐）

```text
1. Temporal 服务端
2. Ollama（及模型）
3. Docker（沙箱/发布依赖）
4. 稳定容器 aiops-stable-order（缺失则重建，见 2.8）
5. （可选）Loki / Alertmanager 栈
6. Worker（确保依赖就绪）
7. （可选）BFF 控制台 / 告警接入 / 可用性巡检 / 日志采集 / 突增检测 / 组件守护
```

> 一键替代：`bash scripts/demo-up.sh` 按此顺序自动编排全部组件并看护（见 3.1）。

### 7.2 日志与产物位置

| 产物 | 位置 | 说明 |
| --- | --- | --- |
| Worker 日志 | `worker.log`（若按 2.9 后台启动） | 全链路 `[evidence]/[cluster]/[triage]/[rag]/[fix]/[sandbox]/[canary]/[release]` |
| 通知留痕 | `data/notify/*.json` | `intake-*` / `req-intake-*` / `fix-start-*` / `approval-*` / `broadcast-*` / `escalation-*` / `shadow-*` |
| ArgoCD 留痕 | `data/argocd/*.json` | 每次渲染的 Application manifest |
| 沙箱工作区 | `data/sandbox/<patch_id>/` | 应用补丁后的源码（同时作为镜像构建上下文，**请勿手工清理正在使用的目录**） |
| 评测报告 | `data/triage_eval_report.json` / `data/fix_eval_report.json` | 离线评测结果 |
| 索引 | `data/code_index.json` / `.faiss` | 由 `index_codebase.py` 生成/重建；专属索引 `data/code_index_<service>.json` 由修复检索自动构建 |
| BFF 日志 | `bff.log` | 控制台 HTTP 访问/错误、流程同步 |
| 巡检状态 | `app_prober.log` / `data/probe_status.json` | 可用性巡检结果（控制台透出） |
| 采集位点 | `ship_app_logs.log` / `data/log_ship_positions.json` | 日志采集进度（重启不重复推送） |
| 突增检测 | `log_surge_detector.log` | 每轮统计与告警注入 |
| 组件守护 | `aiops_watchdog.log` | 组件巡检与自动拉起记录 |
| 备份归档 | `backups/aiops-backup-*.tar.gz`（600） | MySQL 导出 + data 关键 JSON + .env；`scripts/backup-data.py` 生成 |
| 令牌注册表 | `data/console_tokens.json`（demo） | 权限 600，含明文令牌，勿提交 |
| Qoder 工作区 | `data/qoder/` | Qoder 修复引擎隔离工作区（保留期内由清理任务回收） |

### 7.3 升级与回滚

| 场景 | 操作 |
| --- | --- |
| 策略变更 | 修改 YAML → 评审 → **重启相关进程**（worker/BFF/webhook/CLI 全部使用新快照）；锁定项变更会被校验器拒绝启动，属预期保护 |
| 模型切换 | export `AIOPS_FIX_MODEL` / `AIOPS_TRIAGE_MODEL` → 重启 worker（import 时快照） |
| 嵌入模型变更 | 改 `AIOPS_EMBED_MODEL` → **重建索引**（`index_codebase.py`）→ 重启 worker |
| 代码升级 | 拉取新代码 → 重装依赖 → 全量单测 → 重启 worker/接入服务/BFF；前端有变更时 `cd web && npm run build` 后再重启 BFF |
| 回滚 | 代码回滚后重启进程；容器侧稳定版本由发布链路自身保障（金丝雀劣化时稳定容器不动） |

### 7.4 数据清理与日志轮转

- 沙箱产物（TTL 自动化）：`bash scripts/cleanup-sandbox.sh`（默认清理 `data/sandbox/`、`data/qoder/` 中 7 天前产物；产物根可经 `AIOPS_SANDBOX_ROOT` / `AIOPS_QODER_ROOT` 指向独立卷，脚本与代码侧同源取值；`--dry-run` 预演 / `--status` 只看占用 / `--days N` 调整保留期）；
- 日志轮转：`bash scripts/rotate-logs.sh`（copytruncate：>1MB 的 `*.log` 压缩为 `.1.gz` 轮转、保留 7 份，进程无需重启）；
- doctor 的「磁盘水位」项低于 20% 时会指向以上两个工具；
- 手工清理参考——可安全清理：`data/notify/`、`data/argocd/`（审计归档后）、`data/sandbox-eval/`；旧 `data/sandbox/<patch_id>/`（确认无镜像构建引用后）；
- 谨慎清理：`data/code_index.*`（清理后检索降级为空引用，需重建）；`data/sandbox/` 根目录勿整体删除（发布链路会复用最近工作区）。

### 7.5 备份与恢复（MySQL + 关键 JSON）

```bash
.venv/bin/python scripts/backup-data.py                # 全量备份 → backups/aiops-backup-<时间>.tar.gz（600 权限，默认保留 7 份）
.venv/bin/python scripts/backup-data.py --list         # 列出已有备份
.venv/bin/python scripts/backup-data.py --restore-test # 恢复演练：最新归档灌入 aiops_restore_drill 临时库并对比表数（自动清理）
```

- 归档内容：MySQL 全库导出（`--single-transaction`，gzip）+ `data/` 下 9 个关键 JSON（应用清单 / 控制台用户与令牌 / 升级记录 / 采集位点等）+ `.env`（含密钥，故归档 600、目录 700，勿外传）；
- 真实恢复：`tar xzf backups/aiops-backup-*.tar.gz` → `gunzip -c mysql/aiops.sql.gz | mysql -u<user> -p`；`data_files/*` 拷回 `data/`，`env/.env` 拷回工程根；
- 恢复演练依赖 `aiops-mysql` 容器 root 通道（在容器内建临时库验证），`--keep-drill` 保留演练库供人工检查。

### 7.6 开机自启（launchd，macOS）

```bash
bash scripts/launchd-install.sh install    # 安装：登录自启 + 崩溃自动重启（仅托管 watchdog 一个进程，6 组件由其巡检拉起）
bash scripts/launchd-install.sh status     # 查看托管状态与守护进程
bash scripts/launchd-install.sh uninstall  # 卸载（常驻组件保持运行）
```

- 安装时自动接管手动启动的守护（组件不中断）；KeepAlive=SuccessfulExit:false + 30s 节流：正常退出（如单实例锁被占）不重启、崩溃才重启；
- `bash scripts/demo-up.sh down` 检测到 launchd 托管会先临时摘除任务（下次登录随 plist 恢复）；永久停用请用 `uninstall`；
- 模板：`deploy/launchd/com.aiops.watchdog.plist`（安装时替换 `__ROOT__` / `__PYTHON__` 占位符写入 `~/Library/LaunchAgents/`）。

---

## 8 故障排查

| 现象 | 排查与处理 |
| --- | --- |
| `Client.connect` 连接失败 / CLI 提示确认 Temporal | Temporal 未启动或地址错误；`temporal server start-dev`；或设 `TEMPORAL_ADDRESS` |
| Worker 启动即报「锁定策略：…」 | 策略 YAML 修改违反锁定项；按报错恢复对应键（见 4.2） |
| `[triage] degraded=True`，流程立刻转人工 | Ollama 未启动/模型缺失/超时。检查 `curl localhost:11434/api/tags`；`ollama pull qwen3.8:27b-mlx`（或切换 `AIOPS_TRIAGE_MODEL`）；大模型调大超时 |
| `[fix] degraded=True: diff 无法应用` | LLM 输出 diff 上下文不匹配（已内置行内空白容错与重试强化）；偶发属正常，流程会回炉重试；频繁出现建议换更大模型（`AIOPS_FIX_MODEL`） |
| `[sandbox] … 沙箱运行时不可用` | Docker 守护进程未运行；启动 Docker 后系统自动回炉重试 |
| 沙箱报「0 tests（未发现用例）」 | 工作区 tests 目录缺失；确认 `demo-app/tests/` 存在，清理 `data/sandbox/<patch_id>` 后重试 |
| `[canary] 金丝雀未就绪` | 镜像构建慢或端口解析失败；观察 `docker ps -a` 残留容器；系统已做失败清理与重试 |
| `ArgoCD 提交失败` | 配置了 `AIOPS_ARGOCD_URL` 但网络/令牌不通；修复后 activity 自动重试，或临时清空该变量回到留痕模式 |
| 检索结果为空 / 索引陈旧回退 | `index_codebase.py` 重建索引；确认嵌入模型可用 |
| `nice(5) failed: operation not permitted`（macOS 后台启动） | 无害提示，进程正常工作 |
| 局域网打不开控制台 | ① 确认 BFF 以 `--host 0.0.0.0` 启动（`lsof -i :8600` 应显示 `*:8600`；`bash scripts/demo-up.sh status` 会打印局域网地址）；② 使用本机当前 IP（`ipconfig getifaddr en0`，DHCP 可能变化）；③ 检查中间防火墙（macOS 应用防火墙开启时需放行 Python） |
| 控制台首页 200，但 `/api` 全部 503 | 未配置 `AIOPS_CONSOLE_AUTH_TOKENS`（fail-closed）；播种令牌并重启 BFF（见 2.10 / 4.1） |
| 控制台 401「凭证无效或已过期」 | 令牌错误或已被轮换吊销；读注册表取当前令牌（demo：`data/console_tokens.json`；production：MySQL `console_tokens` 表） |
| 控制台写操作返回 429 | BFF 限速触发（已认证写 20/min、认证失败 10/min）；稍后重试或按容量调 `AIOPS_RATE_LIMIT_*` |
| `apply_alertmanager_route.py` 报「模板结构与预期不符」 | 目标 `alertmanager.yml` 非预期结构；脚本拒绝盲改；人工核对锚点后修改脚本常量再执行 |
| 突增检测器报连接错误 | Loki（3101）或 Alertmanager（9093）不可达；确认监控栈在运行 |
| 端口冲突 | 7233（Temporal）/ 11434（Ollama）/ 8600（BFF）/ 9091（worker 指标，production；demo 为 9090）/ 8099（webhook）/ 8643（生产 Caddy HTTPS 入口）/ 18080（stable）/ 3101（Loki）/ 9093（Alertmanager）/ 3316（MySQL）/ 6380（Redis）；用 `lsof -i :<port>` 定位 |

---

## 9 单机受控生产：正式 Temporal 与本机 HTTPS（E/F 产物）

> 本章是批准的单机生产化整改计划 E/F 对应实现，覆盖原始部署说明中的单机切换步骤。**代码/隔离测试完成不等于生产验收通过**。第 2–8 章的演示口令、`start-dev`、全网卡 HTTP、旧生产冒烟及 K8s 历史结论，不作为本次切换操作依据。本轮未启动服务、未创建真实凭证、未安装软件、未拉取镜像、未改 launchd 或外部监控栈。

### 9.1 交付边界与版本证据

| 产物 | 用途 |
| --- | --- |
| `docker-compose.production.yml` | 第二层覆盖，精确替换端口、复用现有卷，增加正式 Server 和显式维护作业 |
| `deploy/temporal/production.yaml` | 正式 Server 静态配置模板；仅 runtime 凭证，禁用归档、不提供 UI |
| `deploy/temporal/bootstrap.sql`、`schema.sh` | 两库授权参考及独立官方 schema/namespace 工具入口 |
| `deploy/caddy/Caddyfile`、`deploy/launchd/com.aiops.caddy.plist` | 本机静态 TLS、精确路由与独立用户级 Caddy 托管模板 |
| `scripts/production_deploy.py` | 受限配置渲染、真实 Compose 合并检查、可选只读挂载/digest 预检；不启动服务 |
| `scripts/production_tls.py` | 本地 CA、90 天 leaf、到期检查、提前 30 天续签及失败恢复 |
| `tests/test_production_deploy.py` | 临时目录隔离测试；不导入应用、不读取仓库 `.env` |

2026-10-13 官方资料核对：

- [Temporal v1.32.1 官方稳定发布](https://github.com/temporalio/temporal/releases/tag/v1.32.1)，发布页显示为稳定 Latest，而不是使用 `latest` 镜像标签。
- [Server 1.32.1 镜像元数据](https://hub.docker.com/v2/repositories/temporalio/server/tags/1.32.1)：锁定索引 digest `sha256:acecbab5b0b8ae9cf4012b508866ca97feda3709577bf157b537239f40029049`。
- [Admin Tools 1.32.1 镜像元数据](https://hub.docker.com/v2/repositories/temporalio/admin-tools/tags/1.32.1)：锁定索引 digest `sha256:9ef0dc5b063bf4ffd5c1512de72e0e73e225bd2c7006a18ce69bd592a8862a83`。两镜像索引均含 Linux arm64/amd64；当前既有基础镜像为 arm64。
- [官方 MySQL Visibility 支持与 schema 命令](https://docs.temporal.io/self-hosted-guide/visibility/mysql)：Advanced Visibility 要求 MySQL ≥8.0.17、Server ≥1.20，使用 `mysql8` 插件、`schema/mysql/v8/{temporal,visibility}/versioned`。运行前仍须用获准的只读连接核实实际 MySQL patch 版本。
- [1.32.1 Server CLI 源码](https://github.com/temporalio/temporal/blob/v1.32.1/cmd/server/main.go)核对使用 `--config-file`；[SQL 工具源码](https://github.com/temporalio/temporal/blob/v1.32.1/tools/sql/main.go)核对 `SQL_PASSWORD`。schema 工具与 Server 同版本，禁止套用旧 K8s 版本。
- 本机 Python SDK 包元数据为 `temporalio 1.33.0`，**已于 2026-10-13 维护窗口与新 Server 联调**（影子 workflow 实际执行成功，见 10.6）；SDK/Workflow 重放兼容性的完整门禁尚未逐项验收，不宣称已通过。固定版本只是候选部署清单；镜像漏洞扫描、隔离 Workflow/查询/信号/计时器/待审批重启恢复均是放行前门禁。后续升级重新核实官方支持与 digest，不移除 digest、不回退 `latest`。

现有 MySQL/Loki/Redis 只读镜像及挂载盘点用于保持原版本：

| 组件 | 固定 digest | 保留要求 |
| --- | --- | --- |
| MySQL | `sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b` | 原 `aiops-agent_mysql-data`，不重建、不自动执行初始化 SQL |
| Loki | `sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193` | 原 `aiops-agent_loki-data` |
| Redis | `sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99` | 当前 `/data` 为匿名卷，必须填入其原始完整卷名，不新建同名替代卷 |
| 旧 Temporal | `sha256:ad4c82c97bd12b417d1ea942610dbcd511afb250c4d5ed26c694009533df447e` | 原 `aiops-agent_temporal-data`，保留 SQLite 与旧 Workflow，不自动迁移或取消 |

上述本机基础镜像 digest 是当前 arm64 基线，不应直接当作其他架构的可移植清单。可选 Prometheus/Grafana 不在生产服务列表，默认使用不可启动的 `requires-approved-digest` 标记；若以后启用，必须另行锁定镜像并治理其旧凭证，不使用本轮默认值上线。

### 9.2 配置准备与无副作用预检

要求已安装 Compose ≥2.24.4（支持 `!override`）、OpenSSL、仓库 `.venv`；Caddy 要求支持 `basic_auth` 的 2.8+，安装路径须显式指定。本轮发现独立 `docker-compose 5.5.1` 可用，但 `docker compose` 插件和 Caddy 不可用，未执行安装（2026-10-13 维护窗口增补：Caddy 已安装并纳入 launchd 托管运行、8643 生产入口已投用，见 10.6）。

**先单独确认**：局域网 IP/8643 端口、旧流程处置、一致性备份及恢复演练、凭证创建/轮换、客户端 CA 分发、实际 Prometheus/Alertmanager 接线。未确认外部接线时不要先应用 loopback 覆盖而造成告警中断。

用户确认后，在权限为 700 的 `data/production-deploy-input/` 准备以下非敏感 `settings.json`（值必须人工填实；不要从 `.env` 自动抽取）：

```json
{
  "project": "aiops-agent",
  "lan_ip": "<已确认的局域网IPv4>",
  "dns_names": ["host.docker.internal"],
  "volumes": {
    "mysql": "aiops-agent_mysql-data",
    "redis": "<inspect得到的原Redis完整卷名>",
    "loki": "aiops-agent_loki-data",
    "temporal_dev": "aiops-agent_temporal-data"
  },
  "caddy_binary": "/opt/homebrew/bin/caddy"
}
```

`dns_names` 只填写实际容器访问名称，自动补入 `localhost`；不允许通配域名。SAN 还固定包含确认的 LAN IP、127.0.0.1、::1。每个卷必须不同且已存在；不允许用新空卷通过门禁。项目名必须与旧容器的 `com.docker.compose.project` 标签一致。

同一受限输入目录还需三个当前用户拥有的 600 单行文件：

- `runtime-password`：DBA 已创建的 `temporal_runtime` 密码。
- `migration-password`：独立 `temporal_migration` 密码，必须与 runtime 不同。
- `metrics-bcrypt`：专用指标服务账号 `aiops_metrics` 的 bcrypt 哈希（成本 10–16；可在授权后用 Caddy 的交互式 `hash-password` 工具获得）。不使用控制台、IM、webhook 或数据库口令；Prometheus 使用对应的独立原始服务密码文件。

渲染器**不创建账号、不修改已有数据库密码、不生成服务令牌**。只读取明确提供的受限文件，拒绝 `.env`、符号链接、宽松权限、换行注入、复用数据库密码及覆盖已有输出目录。真实账号是否具有继承角色/全局权限、跨系统凭证是否复用仍需用户审计，工具不会为此读取现有秘密。

以下命令均是**用户授权后的操作参考，本轮未对真实目录执行**：

```bash
DEPLOY="$PWD/data/production-deploy-config"
INPUT="$PWD/data/production-deploy-input"
.venv/bin/python scripts/production_deploy.py render --output "$DEPLOY" \
  --settings "$INPUT/settings.json" \
  --runtime-password-file "$INPUT/runtime-password" \
  --migration-password-file "$INPUT/migration-password" \
  --metrics-hash-file "$INPUT/metrics-bcrypt" --approved-credential-use

# 只读配置合并；不启动容器、不检查业务就绪。
.venv/bin/python scripts/production_deploy.py preflight --output "$DEPLOY"
# 增加只读 Docker 挂载、项目标签及镜像 digest 对照；不会读取容器 Env。
.venv/bin/python scripts/production_deploy.py preflight --output "$DEPLOY" --inspect-existing
```

输出整体原子发布，文件 600、目录 700，仅允许 Git 已忽略的 `data/production-deploy*` 路径；无需修改 `.gitignore`。产物包括 Server 配置、迁移密码文件、Caddyfile、未安装的 plist、非秘密 `compose.env`、配置快照及两个监控配置片段。`compose.env` 只含项目名、路径、UID/GID、卷名，不含数据库/指标密码。Server 容器显式用非 root 的宿主文件所有者 UID/GID 读取 600 配置，Colima 挂载权限仍需隔离验收。

预检固定显式 `--env-file` 并清除继承的 Compose 文件/profile/自动 env 设置，不会加载仓库 `.env`。它验证真实 Compose 合并后仅有 `mysql redis loki temporal-prod` 四个默认服务、原卷 external 映射、精确 loopback 端口及镜像 digest；退出 0 **仅代表所选预检通过，不代表 G2/G3 或 readyz 已通过**。

### 9.3 正式 Temporal：两库、独立迁移身份、生产 namespace

- 数据库固定为 `temporal_prod` / `temporal_visibility_prod`；AIOps 原库不变。runtime 仅具有两库的 SELECT/INSERT/UPDATE/DELETE；migration 仅额外具有这两库所需 DDL 权限，无全局权限、无 `GRANT OPTION`。SQL 文件不创建账号、不含口令，必须由 DBA 审计新账号及既有库后人工执行。
- `mysql` 的 overlay 清除了原演示初始化凭证。已存在卷不会因此轮换原账户密码；外部卷缺失直接失败。`mysqladmin ping` 仅为进程存活检查，不能替代带权限/schema 校验的 readiness。
- `temporal-prod` 直接启动 `temporal-server --config-file ... start`，不使用 `auto-setup`，不挂载 migration 密码，不在重启时执行 DDL。
- `temporal-schema` 是 `maintenance` profile 的显式作业，必须 `AIOPS_SCHEMA_APPROVED=YES` 才执行。官方工具按版本表初始化/升级，不使用 `--overwrite`，任何步骤失败立即停止。DDL 非整套事务，失败时保留现场，不假设可自动回滚；排障日志仅在该作业受限 `/tmp` 中，不回显原始连接错误。
- `temporal-namespace` 单独维护 `aiops-production`、retention `720h`（30 天）。已存在时先核对，再显式执行 `namespace-update`；不会把网络失败误判成需要覆盖 namespace。
- 生产任务队列约定 `aiops-production-tasks`，由 worker/client 的统一应用配置使用；队列不是 Server 的建表项。BFF、worker、webhook、CLI 的配置统一由应用启动整改接入，本产物不修改应用或既有启动脚本。

**仅在 G1/G2、维护备份、凭证及旧流程处置确认完成后**，按下面次序在窗口内执行。现有 start-dev 必须先由主启动控制器受控停止并备份；profile 只控制选择，**不会停止一个已经运行的旧容器**。不要启动带旧 profile 的全量栈，也不要使用 `down -v`、`--renew-anon-volumes` 或 `--remove-orphans`。

```bash
# 在仓库根运行；此函数只用于已经授权的维护窗口。
unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
DEPLOY="$PWD/data/production-deploy-config"
dc() {
  COMPOSE_DISABLE_ENV_FILE=1 docker-compose --project-name aiops-agent \
    --env-file "$DEPLOY/compose.env" -f docker-compose.yml \
    -f docker-compose.production.yml "$@"
}

# 镜像必须已在另行授权的准备阶段下载、扫描与隔离验证；这里禁止自动拉取。
dc up -d --pull never mysql redis loki
# DBA 已独立创建账号、审计并执行 deploy/temporal/bootstrap.sql 后：
AIOPS_SCHEMA_APPROVED=YES dc --profile maintenance run --rm --no-deps --pull never temporal-schema
# schema 成功后才启动正式 Server。
dc up -d --pull never temporal-prod
AIOPS_NAMESPACE_APPROVED=YES dc --profile maintenance run --rm --no-deps --pull never temporal-namespace
# 已存在且人工确认需要校准保留期时，单独选择：
# AIOPS_NAMESPACE_APPROVED=YES dc --profile maintenance run --rm --no-deps --pull never temporal-namespace namespace-update
```

新库、新服务不修改旧 SQLite。不得将旧 Workflow 自动复制、取消或重新标记为 production。新 Server 接收流程之后不得无条件切回旧 start-dev 写入。部署是同主机单节点、无 HA；Temporal 内网 gRPC 依赖可信本机/Compose 网络边界，不代表多租户鉴权或跨主机 TLS 已实现。

### 9.4 本机 Caddy、专用指标凭证与监控接线

| 入口 | 行为 |
| --- | --- |
| `https://<已确认LAN IP>:8643` | 本机 Caddy；仅绑定确认的 LAN IP、127.0.0.1 和 ::1，无额外 HTTP 重定向监听 |
| 控制台页面、资源及 `/api/*` | 转发到 `127.0.0.1:8600`；保留 BFF 自身鉴权 |
| 精确 `POST /webhook` | 转发到 `127.0.0.1:8099/webhook`；专用 Bearer/HMAC 仍由 webhook 服务验证 |
| 精确 `GET /internal-metrics/bff` | Caddy 专用 `aiops_metrics` Basic 认证后，改写为 BFF `/metrics` |
| 精确 `GET /internal-metrics/worker` | 同样认证后，改写为 `127.0.0.1:9091/metrics` |
| `/metrics`、`/readyz`、`/livez`、`/docs`、`/openapi.json` 及对应子路径 | 拒绝；额外拒绝 redoc/debug、错误方法及带额外后缀的 webhook/指标专用路径 |
| 其他未列入控制台路径白名单的 URL | 404；不反代 Temporal UI、数据库或内部管理端口 |

指标服务密码仅在 HTTPS 使用，Caddy 配置只存 bcrypt；认证成功后剥离 Authorization，不把监控密码发送到 BFF/worker。新前端页面路由若不在白名单，应评审后扩展，不能改为任意上游转发。

Caddy 清除客户端 `Forwarded`、`X-Forwarded-*`、`X-Real-IP`、`X-Original-*`，只用真实 TCP 对端重建转发身份。不配置全网可信代理。应用侧必须由主代理同步收敛 BFF 8600、webhook 8099、worker 9091 为 loopback，且只信任本机代理；本配置不能代替应用信任边界整改。应用对同一头是否二次解析、伪造头不能绕过限速/审计，仍需端到端验证。

渲染器生成 `prometheus.fragment.yml` 与 `alertmanager.fragment.yml`（JSON 语法的合法 YAML）：

- 两个 Prometheus job 使用 HTTPS 8643、精确指标路径、`basic_auth.password_file=/run/secrets/aiops_metrics_password`。
- Alertmanager receiver 使用 HTTPS `/webhook`、独立 `authorization.credentials_file=/run/secrets/aiops_webhook_token`，不生成未知业务的匹配规则或改写原 route。
- 两者显式使用 `tls_config.ca_file=/etc/aiops-tls/ca.crt`，不设置跳过 TLS 校验。需在实际容器中只读挂载 CA 公共证书和各自的凭证文件，并确认访问 `LAN IP:8643` 可达。
- **只生成片段，不编辑或重启现有监控栈。** 用户确认后人工审阅 diff、合并 receiver/job 和业务路由、分发 CA/凭证；从实际 Colima 容器验证抓取和告警链路，再收敛旧入口。未接线时 G2 不通过（**2026-10-13 维护窗口增补：接线已完成**——receiver 指向 `https://10.1.112.39:8643/webhook` 并挂载 ca_file/credentials_file，Prometheus 双 job 接入，容器内实测投递链路与抓取双 UP，见 10.6）。

### 9.5 本地 CA、原子续签与 launchd 边界

下列命令会创建真实密钥或 reload，**每项执行前必须取得用户明确授权**；本轮仅在测试临时目录调用签发逻辑。

```bash
.venv/bin/python scripts/production_tls.py init-ca --deploy-dir "$DEPLOY" --approved-credentials-and-reload
.venv/bin/python scripts/production_tls.py issue --deploy-dir "$DEPLOY" --approved-credentials-and-reload
# 仅检查；不生成文件、不修改信任库。0=尚未到续签窗口，2=剩余不超过30天，1=异常。
.venv/bin/python scripts/production_tls.py check --deploy-dir "$DEPLOY"
# 下列校验不启动服务；端口检查只在旧服务已受控停止、新服务尚未启动的阶段使用。
.venv/bin/python scripts/production_deploy.py preflight --output "$DEPLOY" --validate-caddy
# 可在启动正式 Temporal/BFF/Caddy 前单独运行，冲突即停止，不抢占端口：
# .venv/bin/python scripts/production_deploy.py preflight --output "$DEPLOY" --check-ports
# 获准后可交由每日调度调用；本产物不安装任何调度任务。
.venv/bin/python scripts/production_tls.py renew --deploy-dir "$DEPLOY" --approved-credentials-and-reload
```

证书流程使用 OpenSSL RSA-3072/SHA-256：CA 3650 天、pathlen=0；leaf 90 天，仅 serverAuth，SAN 必须精确匹配已确认 IP/域名。CA 剩余不足 91 天时停止签发，要求人工轮换；IP/SAN 改变即触发受控重签，不沿用错误证书。所有私钥和证书文件 600，目录 700，拒绝路径逃逸和符号链接输入；只有工具维护的 `tls/current` 是指向同目录版本的受控链接。

- CA 在 `tls/ca/`；leaf 写入独立版本目录，校验链、用途、SAN、有效期及公私钥匹配后，原子替换 `tls/current`，不先覆盖活动密钥。
- 剩余 ≤30 天才续签（SAN 变化除外），续签串行加锁。执行 Caddy validate 后，经受限 Unix 管理套接字 `caddy-admin.sock` **强制 reload**，确保静态路径没变时仍重读证书。
- reload 失败或超时，恢复原链接并尝试重新加载旧证书，失败时非零退出且 stderr 告警；原证书/私钥始终保留。不把“磁盘已恢复”当作“进程已恢复”。
- 中断或二次 reload 失败留下 `tls/transition.json`，后续检查失败关闭。人工核对后运行 `production_tls.py recover --deploy-dir "$DEPLOY" --approved-credentials-and-reload`，只恢复记录中的旧版本并重试加载；仍失败则保持维护状态排障。
- 每日 `check/renew` 调度和退出码告警接入需另行授权安装；本次未运行定时续签、未接入外部告警，不宣称已经自动运维。旧版本清理也不自动执行。

**信任分发**：只分发 `tls/ca/ca.crt` 公共证书；`check` 输出 SHA-256 指纹供另一条可信通道核对，绝不分发 `ca.key` 或 leaf 私钥。客户端可先用 `curl --cacert <CA公共证书> https://<IP>:8643/` 做局部信任测试；未导入 CA 的客户端应明确失败，禁止使用 `-k`。浏览器如需系统信任，由用户核对指纹后人工导入。撤销时从各客户端信任库/容器挂载中删除该 CA 并替换证书；CA 私钥泄露必须另行轮换 CA、重新分发且撤销旧信任，静态本地 CA 没有自动在线吊销能力。本工具不调用系统信任安装命令。

`com.aiops.caddy.plist` 只由渲染器输出到受限目录：固定绝对二进制路径、WorkingDirectory、受控 PATH、私有 XDG 目录、日志和 Umask 077；正常停止不自行恢复，异常退出有 30 秒节流。Caddy 使用静态证书、关闭 `auto_https`/配置持久化，Unix 管理接口仅本机同用户可用。安装/注册/停止须与主启动控制器协调并单独授权，本工具不修改 `~/Library/LaunchAgents`。用户级代理只承诺登录后托管，不承诺无人登录可用。

### 9.6 本轮证据与待验收清单

定向测试必须先导入 `tests`，并保留 `-t .`：

```bash
.venv/bin/python -c 'import tests, unittest; unittest.main(module=None, argv=["unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_production_deploy.py", "-v"])'
```

本轮实际结果：32 项定向测试，31 项通过、1 项因未安装 Caddy 跳过。已覆盖真实 Compose 配置合并（含错误端口追加、start-dev 混入、缺失 digest 拒绝）、身份/凭证隔离、输出权限、路径及 `.env` 门禁、真实 OpenSSL 临时 CA/leaf、精确 SAN、90/30 天边界、IP 变化、证书校验失败不切换、reload 成功/失败/二次失败恢复、并发锁、schema 审批门禁与 shell 语法，以及 Caddy/监控片段静态断言。Caddy 官方适配器用例明确跳过，不能据此声称真实 HTTPS/认证路由通过（2026-10-13 维护窗口增补：Caddy 已安装托管、8643 真实 HTTPS 入口与认证路由已投用，该跳过用例已可运行；最近全量复跑 unittest `OK(skipped=6)`、pytest `1261 passed, 6 skipped`）。

另已执行 `sh -n deploy/temporal/schema.sh`、`plutil -lint deploy/launchd/com.aiops.caddy.plist`、`git diff --check`，均通过。对两个新 Python 工具及新测试运行 Bandit：HIGH=0；两项 Medium 均为负向测试中的 `0.0.0.0` 输入（B104），用于断言全网卡绑定被拒绝，不是运行监听配置。未运行会导入应用/环境配置的全量测试，不沿用旧测试数量作为本轮结论。

上线前仍须逐项提供运行证据：

1. 备份可恢复、旧流程清单及处置确认，原四卷与存量数据不变；实际 MySQL 版本、runtime 无 DDL、migration 无全局权限并在维护后回收。
2. 同版本官方 schema/Server 启动、生产 namespace 30 天、worker 只消费生产队列；SDK 联调、查询/信号、重启后的历史/计时器/待审批恢复；镜像漏洞门禁。
3. Caddy 官方解析及实际 TLS 校验、正确/错误/缺失指标凭证、错误方法/近似路径/编码路径拒绝、控制台鉴权、伪造转发头不能改变限速/审计来源。
4. 从另一台客户端与实际 Colima 监控容器验证 CA、采集和 webhook；LAN 无法直连 MySQL/Redis/Loki/Temporal/BFF/webhook/worker 指标及可选 UI 的宿主端口。
5. Caddy/业务启动器联动、受控重启、每日续签/告警调度、真实 reload 回滚、两个 watchdog 周期及后续 24 小时观察。

上述任何项未完成，结论必须保留为“产物/隔离验证完成，G2/G3 待确认或待运行验收”，不得称为生产已部署、HA、异地容灾或真实自动发布准入。

---

## 附录 A 工程目录结构

```text
aiops-agent/
├── aiops_agent/                # Python 包（业务核心，27 模块）
│   ├── workflows.py            # 主工作流（状态机/闸门/队列/审计）
│   ├── activities.py           # 16 个 Temporal activity
│   ├── worker.py / config.py / models.py / mode.py
│   ├── triage.py / logs.py / page_health.py       # 分析层（含通用白屏判据）
│   ├── code_rag.py / fix_agent.py / qoder_fix.py  # 决策层（Ollama / Qoder 双修复提供者）
│   ├── sandbox.py / release.py                    # 验证层 / 执行层（Docker 沙箱、金丝雀滚动）
│   ├── notify.py / git_integration.py / requirements_client.py / requirement_agent.py
│   ├── app_manifest.py / app_registry.py / changes.py
│   └── db.py / kill_switch.py / metrics.py / cleanup.py / tracing.py
├── bff/                        # 控制台 BFF（FastAPI :8600；聚合 Temporal + 磁盘留痕）
│   └── routes/                 # 14 域路由：flows / approvals / audit / health / monitored_apps /
│                               # system / tokens / escalations / requirements / users / ops_metrics /
│                               # chatops / systems / im_callback（飞书卡片回调网关）
├── web/                        # React SPA（vite build → web/dist，由 BFF 同源托管）
├── demo-app/                   # 演示被修复的服务（源码 + tests + Dockerfile + http_server）
├── scripts/                    # demo-up.sh（一键编排）/ aiops-watchdog.py（组件守护）/
│                               # production-smoke.py / security-scan.sh / scenarios/**（8 场景剧本）
├── tests/                      #  单元测试（49 个测试文件 / 1331 用例）
├── monitoring/                 # Alertmanager 路由注入脚本
├── prometheus/                 # 告警规则样例
├── data/                       # 索引 / 留痕 / 沙箱工作区 / 评测报告（运行期生成）
├── release-gate-policy.yaml    # 生产策略
├── demo-policy.yaml            # 演示加速策略
├── demo_cli.py                 # 操作 CLI
├── alert_webhook.py            # 告警接入服务
├── app_prober.py               # 可用性巡检（白屏也能发现）
├── ship_app_logs.py            # 日志采集（被监控应用 → Loki）
├── log_surge_detector.py       # 日志突增检测器
├── analyze_requirements.py     # 需求基线分析 CLI
├── check_aiops_interface.py    # 标准接口校验 CLI
├── demo_log_generator.py       # 演示日志生成器
├── index_codebase.py           # 索引构建/检索 CLI
├── eval_triage.py / eval_fix.py# 离线评测
└── requirements.txt
```

## 附录 B 端口清单

| 端口 | 组件 | 可配置项 |
| --- | --- | --- |
| 7233 | Temporal gRPC | Temporal 服务端参数 / `TEMPORAL_ADDRESS` |
| 8233 | Temporal Web UI | Temporal 服务端参数 |
| 11434 | Ollama | Ollama 服务端参数 / `AIOPS_OLLAMA_URL` |
| 8600 | BFF 控制台（SPA + `/api` + `/metrics`） | uvicorn `--port`（demo-up.sh 固定 8600） |
| 8643 | 生产 HTTPS 入口（本机 Caddy 反代 8600 / 8099 / 指标通道） | `scripts/production_deploy.py` 渲染（E/F 产物，未运行验收） |
| 9090 / 9091 | worker 指标服务（demo / production 默认；production 仅 loopback） | `AIOPS_METRICS_PORT`（0 关闭） |
| 8099 | 告警接入服务 | `--port` / `AIOPS_WEBHOOK_PORT` |
| 18080 | 稳定容器（宿主映射） | `AIOPS_STABLE_PORT` |
| 8000 | demo-app 容器内端口 | demo-app 的 `PORT` 环境变量 |
| 3101 | Loki（本机栈） | `AIOPS_LOKI_URL` |
| 9093 | Alertmanager（本机栈） | 检测器 `--alertmanager` / `AIOPS_ALERTMANAGER_URL` |
| 3316 | MySQL（本机 compose `--profile prod`） | compose 端口映射 / `AIOPS_DATABASE_URL` |
| 6380 | Redis（本机 compose `--profile prod`） | compose 端口映射 / `AIOPS_REDIS_URL` |
| 9095 / 3200 | Prometheus / Grafana（`--profile obs`） | compose 端口映射 |
| 5273 | 前端 Vite 开发服务器（可选，`npm run dev`） | `web/vite.config.ts` |

## 附录 C 模型与数据规格（实测）

| 项 | 规格 |
| --- | --- |
| 嵌入模型 | `bge-m3:latest`，1024 维 |
| 索引规模（demo-app） | 8 个源文件 → 33 代码块 + 6 历史工单 |
| triage 默认模型 | `qwen3.8:27b-mlx`（`temperature=0.2`，JSON 模式，超时 45s；轻量替代 `qwen3:8b`） |
| 修复模型 | Ollama 提供者默认随 triage（27b-mlx 实测沙箱通过率 1.0 vs 8b 的 0.0）；Qoder 提供者默认 `DeepSeek-Flash`（100 轮 / 720s） |
| 沙箱镜像 | `python:3.12-slim`（`AIOPS_SANDBOX_IMAGE`） |
| 金丝雀 SLO | 错误率 ≤ 5%、P99 ≤ 500ms（观测窗口内实测判定） |

## 10 批次 H：加密备份与受限恢复实施（2026-10-13）

本章追加在既有内容之后，第 9 章和历史记录保持原样。本章取代旧备份工具的操作方法：**不再支持无配置备份、未加密 tar.gz 恢复、默认 `aiops-mysql` 容器、root 通道或固定 `aiops_restore_drill` 库**。历史归档保留，不自动删除、转换或灌入生产库。

### 10.1 已实现范围与一致性边界

入口仍为 `scripts/backup-data.py`，核心为 `aiops_agent/backup.py`；仅导入无副作用的 `mode` 模块，不加载应用配置，不自动读取 `.env`。必须提供 600 权限 JSON 配置，`mode` 与进程 `AIOPS_MODE` 一致，`data_root` 与 `mode.data_root()` 的实际结果完全一致。production 缺三库、关键来源、独立密钥或安全门禁即失败，不降级为文件备份；demo 不连接任何 MySQL。

- 收集整个模式数据根，而非旧 JSON 白名单；demo 默认 `data/` 明确排除 `data/prod/`。production 另外自动纳入尚在旧位置的 `data/notify`、`data/argocd`、`data/log_ship_positions.json`（存在时），避免模式迁移前漏掉通知、发布与采集位点。新的独立外置目录必须在 `sources` 中明确列出。
- production 必填来源包括策略、部署配置、受保护环境文件、TLS、通知、审计、发布记录、补丁恢复材料、采集位点。空目录在 manifest 中记录；缺目录不静默跳过。审计数据库内容随业务库导出，文件留痕随数据根/审计来源收集。备份配置和 MySQL 专用客户端配置也进入加密归档；**备份加密密钥不进入归档**。
- 同一个专用 MySQL8 连接先取得 `LOCK INSTANCE FOR BACKUP`，核对三库的基础表全部为 InnoDB；随后仅运行一次 `mysqldump --single-transaction --databases <业务库> temporal_prod temporal_visibility_prod`。DDL 备份锁保持至导出和统计完成，失败释放连接；非事务表、视图、缺库、权限不足均阻断。本版不声称支持任意存储引擎或视图恢复。
- MySQL 凭证只从明确的 600 客户端文件读取；`[client]` 必须显式配置非 root 的 `user`、`password`、loopback `host`（127.0.0.1 或 ::1）和 `port`，禁止隐式 socket。客户端禁止读取登录路径文件，密码不进入参数、状态或日志。需同版本客户端支持这些参数（**2026-10-13 维护窗口增补：mysqldump 9.6 客户端 vs MySQL 8.0 服务器已实测**，保护备份成功产出已认证归档，见 10.6）。为该专用账户审定三库 SELECT、导出 routines/events/triggers 所需权限及 `BACKUP_ADMIN`，不要直接授予生产 root。
- 文件复制前后检查设备/inode/大小/纳秒时间戳，复制结束再次核对整个文件清单；新增、删除、替换或写入变化导致本次失败。production 还要求有效的写入暂停/发布空闲租约，并持有既有公共发布锁。不自动暂停写入或停止服务，不声称应用 DB、Temporal 与文件天然构成全局事务；manifest 明确标记为**组件级一致性**。
- 日常 Loki 持久卷不属于本工具已经完成的日志备份；旧 Temporal SQLite 和 Loki 卷的维护窗口冷备尚须单独实施与验证。可在受控冷备完成后把冷备产物作为额外 `sources` 一起加密，并用 `--protected` 永久保留，但添加文件或使用该开关**不能证明冷备一致性**。

### 10.2 配置、密钥与发布协调门禁

以下仅为字段示例，所有路径必须人工替换为绝对真实路径；本轮没有生成真实配置、口令或密钥。

```json
{
  "mode": "production",
  "data_root": "/ABS/aiops-agent/data/prod",
  "backup_dir": "/ABS/aiops-archives",
  "key_file": "/ABS/private-offline-key/backup.key",
  "quiescence_file": "/ABS/backup-control/quiescence.json",
  "release_lock": "/ABS/SHARED_RELEASE_LOCK",
  "mysql": {
    "defaults_file": "/ABS/backup-control/mysql.cnf",
    "dump_binary": "/ABS/mysql-client/bin/mysqldump",
    "databases": ["aiops", "temporal_prod", "temporal_visibility_prod"]
  },
  "sources": {
    "policy": "/ABS/aiops-agent/release-gate-policy.yaml",
    "deployment": "/ABS/aiops-agent/data/production-deploy",
    "environment": "/ABS/protected-runtime-environment",
    "tls": "/ABS/aiops-agent/data/production-deploy/tls",
    "notifications": "/ABS/ACTUAL_NOTIFICATIONS",
    "audit": "/ABS/ACTUAL_AUDIT_FILES",
    "release_records": "/ABS/ACTUAL_RELEASE_RECORDS",
    "release_recovery": "/ABS/ACTUAL_PATCH_RECOVERY_MATERIALS",
    "positions": "/ABS/ACTUAL_LOG_POSITIONS"
  },
  "offsite": {"enabled": false}
}
```

`mysql.databases` 是实际同实例三库的显式清单，第一项必须由操作者核对为既有业务库，不创建新库冒充原数据。`AIOPS_POLICY_PATH` 已设置时必须与 `sources.policy` 一致。环境文件只是经批准执行真实备份时的加密输入，dry-run 不读取其正文；不要把包含备份密钥的父目录作为来源。

密钥由用户另行批准后安全提供，长度必须为 **32 个原始字节**，不是 64 字符十六进制字符串。密钥文件属于当前用户、权限 600，父目录 700，必须在仓库、备份目录和所有来源之外；不自动生成、不进入 Git、日志或归档。操作者必须另行登记密钥版本与适用归档，并在独立离机介质保管可恢复副本。密钥遗失无法解密；轮换期间保留旧密钥，新旧密钥归档宜分目录管理，当前密钥无法认证旧归档时保留策略会停止，不盲删文件。本机数据、归档、密钥同时丢失时不保证恢复。

production 的 `quiescence_file` 必须是当前用户的 600 JSON，字段如下：

```json
{"mode":"production","data_root":"/ABS/aiops-agent/data/prod","writers_paused":true,"releases_idle":true,"expires_at":0}
```

示例的 `expires_at=0` 故意不可用：协调方确认写入已暂停、发布无跨阶段占用后，才能填入当前时间之后、不超过一小时的 Unix 秒时间戳。备份前后均检查租约。`release_lock` 必须是**发布执行方和备份共同遵守的同一个已有 600 文件**；不得随便创建无关空锁冒充协调完成。

**接线状态（2026-10-13 增补，H 收尾）**：备份与发布共享锁及占用记录的接线**已完成**——`release_lock`、`sources.release_records`、`sources.release_recovery` 三项必须与 `aiops_agent/release_journal.py` 的 `coordination_paths()`（发布控制目录 `data/release-control/`）逐字一致，否则 `load_config` 直接拒绝；备份取得发布锁后立即调用 `assert_idle_locked()`，日志存在非终态发布记录（如崩溃残留）时拒绝拍快照，不再可能用无关空锁冒充“发布空闲”。该约束由 2 项隔离测试守护（`tests/test_backup.py`）。仍待：租约签发/解除自动化、无人值守协调。**真实环境运行验证已于 2026-10-13 维护窗口完成**（首个生产保护归档 `--verify` 认证通过）；调度 plist 已完成占位符替换并以禁用态注册（启用前仍须人工确认租约可正常签发），见 10.6。**每日任务启用前必须人工确认 plist 已替换真实路径且租约可正常签发，不能仅凭模板存在宣称日备份已上线。**

### 10.3 归档格式、权限与状态

受控输出目录及临时目录均为 700，新文件为 600；现存不安全权限直接拒绝，不擅自 chmod 生产材料。拒绝路径穿越、符号链接和硬链接别名。唯一特例为已配置 TLS 根的 `current`：必须指向该 TLS 根内部的真实版本目录，归档中物化为普通文件，同时记录链接目标；要求 CA 证书/私钥以及 current 的 leaf 证书/私钥齐全。恢复到正式部署时需人工核对并重建版本链接，不把物化目录原样覆盖正在使用的 `current`。部署目录中的 Caddy 管理 socket 记为运行期排除项。

SQL stdout 直接写入 600 暂存文件，不捕获到整块内存；逐行 SQL 统计，单条 INSERT 一行，BLOB 使用十六进制。tar/gzip 输出直接流入 `cryptography` 的 AES-256-GCM 增量加密器，随机 96 位 nonce、128 位认证标签，格式头作为附加认证数据。加密后先完整解密认证，再检查 manifest 和每个成员，成功后同文件系统原子改名并 fsync；失败不会发布成功归档。工作目录需要足够空间保存 SQL、文件快照和校验用压缩包；这些临时明文受 700/600 权限保护，正常返回会清理，异常断电遗留需受控核查，不能宣称磁盘取证级安全擦除。

加密内的 `manifest.json` 包含格式/工具版本、模式、实际数据根、来源、创建时间、daily/protected 类型、组件一致性声明、逐文件字节数与 SHA-256，以及三库逐表 schema SHA-256、INSERT 行数和行内容 SHA-256 的模 2²⁵⁶ 求和指纹。内容指纹可发现行数相同但值被改变的情况，不靠表数判断恢复成功。归档成员只允许登记的普通文件，拒绝重复、缺失、链接、绝对路径和 `..`。

- 日备份默认保留最近 **7 份已认证成功**的当前模式归档，按加密 manifest 创建时间排序；同秒执行也保留最新一份。清理仅匹配自有命名，验证身份和 inode 后通过目录句柄删除，`keep<1` 直接拒绝。
- `--protected` 使用独立 protected 命名，不进入日常清理集合。未知旧格式、未完成文件、另一模式和受保护归档不删除；损坏/无法认证的同模式日备份触发失败，不拿它凑成功份数。
- `backup.lock` 防止备份、验证和恢复测试并发重入。状态原子写入 `status-production.json` 或 `status-demo.json`，包含最后成功时间、大小、SHA-256、校验结果以及脱敏失败原因；后续失败保留上一成功时间。
- `--status` 超过 **26 小时**无成功备份、时间倒退、状态损坏或最近失败时非零；`stale` 用于超过时限，`last_failure` 用于最近失败。状态仅证明最近记录，不代替周期性 `--verify`。监控接入方需定期调用并告警，目前未接线到外部监控（2026-10-13 增补：BFF/worker 指标抓取已接入 Prometheus 双 job `health=up`；本项备份状态告警仍未接线）。

### 10.4 命令与独立恢复约束

先设置与实际运行一致的模式/数据根，再使用仓库 `.venv`。下列仅为说明，**本轮只运行第 10.5 节的临时目录测试，没有执行这些真实操作**。

```bash
# 配置结构和路径元数据预检，不读取数据/密钥/环境文件正文，不连接MySQL或Docker。
.venv/bin/python scripts/backup-data.py --config /ABS/backup.json --dry-run
# 输出未完成门禁；运行验收未证实时始终退出2，不报通过。
.venv/bin/python scripts/backup-data.py --config /ABS/backup.json --readiness
# 以下实际备份必须在凭证、协调门禁与风险确认完成之后才能执行。
.venv/bin/python scripts/backup-data.py --config /ABS/backup.json --keep 7
.venv/bin/python scripts/backup-data.py --config /ABS/backup.json --protected
.venv/bin/python scripts/backup-data.py --config /ABS/backup.json --status
.venv/bin/python scripts/backup-data.py --config /ABS/recovery.json --verify /ABS/ARCHIVE.aes
# 只导入人工预先准备的空白独立测试实例，保留实例，不自动删库/容器/卷。
.venv/bin/python scripts/backup-data.py --config /ABS/recovery.json --restore-test /ABS/ARCHIVE.aes --test-target /ABS/test-target.json --approve-test-import
```

灾后 `recovery.json` 只需 `mode`、`backup_dir`、`key_file`；`--verify`、`--restore-test`、`--list`、`--status` 不要求原数据根或源数据库仍存在。运行模式仍须显式一致。`--verify` 的退出 0 **仅表示加密认证及文件校验通过**。`--restore-test` 即使内容对比通过也返回 2，因为业务/Temporal 运行验收未实现自动放行。

测试目标 JSON 字段为 `container`、完整 `container_id`、`volume`、`port`、`test_marker`。必须同时满足：

1. 容器、卷、网络名均匹配 `aiops-backup-test-*`；容器与卷及网络的 `io.aiops.backup-test` 标签等于指定的 32 位十六进制测试标记。标记是隔离身份，不是生产凭证。
2. inspect 返回的名称和完整 ID 完全一致；仅挂载一个命名卷到 `/var/lib/mysql`，不接受任何宿主 bind。该卷只能被此容器引用，包括已停止容器也不能共享。
3. MySQL 只发布 `127.0.0.1:<显式20000以上端口>`；不能使用原 3316 或 3306。网络必须是带相同标签的 internal 网络且没有其他容器，拒绝 host/共享网络、额外网络和 privileged 容器。工具不创建或启动测试设施。
4. 容器本层须由用户预置 `/run/secrets/aiops-backup-test.cnf`，只允许 `aiops_restore_test` 用户，不回退 root。所有客户端命令强制容器内 `127.0.0.1:3306` TCP，不能被 cnf 的 host/socket 改向其他实例。账户需有全局 `SHOW DATABASES` 权限以证明没有隐藏的既有库，以及仅在这个独立实例导入/再导出的权限。实例除 MySQL 系统库外必须完全空白，否则导入前拒绝，不做 DROP DATABASE。
5. 解密认证在解析之前完成；临时解包逐文件校验，SQL 通过文件 stdin 流入已核实容器 ID。随后在同一测试容器重新导出，比较归档 manifest 的每张表结构、行数与内容指纹，不查询原生产库来凑验收结果。测试实例和卷保留供人工调查，本工具不清理它们。

恢复报告写入 `restore-status.json`，只报告实际完成的文件/SQL内容校验。**正式 Temporal Server 的隔离恢复（历史读取、预置等待流程恢复与业务 API 验证）尚未执行，运行验收保持 blocked**（2026-10-13 维护窗口增补：正式 Server 已运行并联调、影子 workflow 已实际执行，见 10.6）；恢复数据到 MySQL 不等于 Temporal 已恢复。下一阶段需另建隔离正式 Server、测试 namespace/worker，验证历史、审批/计时器继续及业务关键行/API，并记录真实耗时；不得将这些模拟单测当成该证据。

### 10.5 调度模板、定向证据与待办

`deploy/launchd/com.aiops.backup.plist` 为独立任务模板：每日本地时间 **03:00**，固定 `__ROOT__/.venv/bin/python`、WorkingDirectory、受控 PATH、production 模式/数据根、`--keep 7`、Umask 077 和固定日志路径。不随登录立即执行，不配置失败无限重启。`__ROOT__`、`__BACKUP_CONFIG__`、`__DATA_ROOT__`、`__BACKUP_DIR__` 须人工替换并检查 XML 转义；日志父目录预置为 700。没有模板渲染/安装副作用，未修改实际 LaunchAgents（**2026-10-13 后续执行：占位符已替换真实路径并以禁用态注册**，见 10.6）。用户级 launchd 只承诺登录后的调度能力，不能据此保证无人登录、主机睡眠或关机时按时备份。

本轮实际运行（工作目录仓库，先由 `tests/__init__.py` 隔离，再加载测试模块）：

```bash
.venv/bin/python -m unittest tests.test_backup -v
```

**40 项定向测试全部通过，无跳过**（含 H 收尾新增 2 项：发布记录非终态时拒绝快照、无关 decoy 锁被配置校验拒绝）。临时目录真实执行 AES-GCM 加解密、tar/gzip、文件校验、权限、互斥和保留清理；MySQL、Docker、导入与再导出均为显式替身，默认禁止任何真实子进程。覆盖三库单次导出参数和 DDL 锁、非事务表/缺配置拒绝、状态 26 小时边界、源变化、TLS/目录/硬链接逃逸、认证篡改、恶意归档成员、7 份与 protected、真实源丢失后的离线校验、错误恢复目标、隐藏库权限、相同行数内容不同拒绝及 Temporal 未证实阻断。`git diff --check` 通过；语言服务器未就绪，未把 IDE 静态诊断称为通过。

本轮**没有**读取真实 `.env` secrets、运行真实备份、启动容器、停止服务、恢复真实数据库、生成真实凭证、安装 launchd、提交或推送。仍待完成：租约签发/解除与无人值守协调（共享锁与占用记录接线已完成并校验，见 10.2）、真实 MySQL8/客户端一致性备份及隔离灌入、正式 Temporal/业务 API 验收、SQLite/Loki 冷备及恢复、实际 03:00 调度与 26 小时告警、RPO≤24h / RTO≤60min 的实测。异地入口只接受 `{"enabled":false}`，输出“未配置”，不提供上传能力；G2/G3、全天日志灾备、异地容灾均未通过。（上述为 H 批次边界；2026-10-13 维护窗口已执行的内容与残余待办见 10.6。）

### 10.6 维护窗口执行结果（2026-10-13 增补）

用户批准的维护窗口（2026-10-13 17:45–18:30）已执行单机切换与运行验收，执行清单与回填证据见 `data/maintenance-window/维护窗口执行清单.md`（含第 5 节证据链）。要点：

- **正式栈投用（影子模式）**：`temporal-prod`（独立 runtime/migration 账户、生产 namespace）、MySQL/Redis/Loki 与 BFF/worker 等组件在 production 档运行，`/readyz` 返回 ok；组件监听收敛 loopback（8600/8099/9091），8643 为本机 Caddy 三向入口（10.1.112.39 / 127.0.0.1 / ::1）。
- **launchd 托管**：`com.aiops.caddy`、`com.aiops.watchdog` 已安装并 running；`com.aiops.backup` 已完成占位符替换（`plutil -lint` 通过）并以**禁用态**注册（`launchctl print-disabled` 确认 disabled）——启用前置=10.2 条款“租约可正常签发”，启用命令 `launchctl enable gui/$(id -u)/com.aiops.backup`。
- **真实备份与冷备**：首个生产保护归档 `aiops-production-protected-20261013T095148-…aes`（112094B，`--verify` 已认证）产出；W1 完成旧 Temporal SQLite 与 Loki 卷冷备（`~/aiops-archives/maintenance-20261013-1749/`，含 `SHA256SUMS`）。`--dry-run` 复核：`configuration=valid`、`last_failure=null`、`stale=false`。
- **监控接线（G2 前置）**：Alertmanager receiver 指向 `https://10.1.112.39:8643/webhook`（`ca_file` + `credentials_file`），投放验证经 Caddy 到达接入层并实际开出 workflow；Prometheus `aiops-production-bff` / `aiops-production-worker` 双 job `health=up`、无 lastError。
- **影子冒烟（W5）**：webhook 告警开出 workflow，链路 evidence（Loki）→ Drain3 聚类 → LLM triage（confidence=0.30）→ 闸门 1 转人工（投递=recorded），未触真实仓库/MR/发布路径。
- **残余待办**：租约签发/解除与无人值守协调（备份调度启用前置）、03:00 实际调度与 26 小时告警接线、真实归档的隔离恢复灌入、SQLite/Loki 恢复演练、RPO≤24h / RTO≤60min 实测。异地容灾与全天日志灾备仍未通过。

> 注（2026-10-13 网络变更后）：主机 LAN 地址由 DHCP 变更为 `192.168.0.6`；入口与监控目标已由 `./start.sh sync-ip` 同步切换，见 10.7。

### 10.7 LAN 地址迁移与动态同步（2026-10-13 增补）

生产入口（Caddy 站点/bind）、TLS 叶证书 SAN 与监控抓取目标均绑定主机 LAN 地址，该地址由 DHCP 分配；网络环境变化后须同步切换。仓库提供一键同步工具（幂等，可反复执行）：

```bash
./start.sh sync-ip              # 探测当前 LAN 地址（en0/en1）并同步全部地址引用
./start.sh sync-ip --dry-run    # 只报告将执行的变更；--ip 可显式指定地址
```

流程（`scripts/sync_lan_ip.py`）：探测并校验新地址（私网 IPv4、8643 可绑定）→ 就地更新 `data/production-deploy/Caddyfile`（站点与 bind）与 `settings.json`（含 `production-deploy-input` 镜像）→ 重渲染监控片段并在 `data/monitoring-wiring-review/lan-ip-<新地址>/` 生成迁移材料 → 调 `scripts/production_tls.py renew` 重签叶证书（SAN 含新地址）并 reload Caddy。地址未变化时仅做证书核查；续签或 reload 失败保留原证书，可重跑本命令重试。

外部监控栈沿用第九节边界（工具不自动改写/重启）：按迁移材料人工替换监控栈 `deploy/prometheus.yml` 两个 targets 与 `deploy/alertmanager.yml` 的 receiver url（替换前保留 `.bak.<时间戳>`），重启两个容器（Alertmanager 由 entrypoint 渲染模板，改模板必须重启容器），以 targets `up` 与 `/-/ready` 验证。

2026-10-13 首次执行记录（DHCP：`10.1.112.39` → `192.168.0.6`）：Caddyfile 2 处、两处 settings 与两个片段已更新；叶证书重签（SAN = host.docker.internal / localhost / 127.0.0.1 / ::1 / 192.168.0.6）并 reload，入口新地址与本机均可达 200；监控栈替换 + 容器重启后 Prometheus 双 job `up`、Alertmanager `/-/ready` 200；被监控应用条目 url 更新为 `http://192.168.0.6:3000/`（生产库 monitored_apps 表），巡检下一轮恢复在线。
