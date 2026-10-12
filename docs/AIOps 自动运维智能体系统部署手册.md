# AIOps 自动运维智能体系统部署手册

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.1 |
| 日期 | 2026-10-12 |
| 适用对象 | SRE / 实施工程师 / 环境维护人员 |
| 关联文档 | 《系统设计说明书》、《用户操作手册》 |
| 修订记录 | v1.1（2026-10-12）：同步最新工程现状——BFF 控制台（0.0.0.0:8600，支持局域网访问）、可用性巡检 / 日志采集 / 组件守护、需求基线闭环、Qoder 修复引擎、模型默认值切换（qwen3.8:27b-mlx）、测试基线 832 用例 |

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

> 此外，系统自身有 **6 个常驻组件**（worker、BFF 控制台 `:8600`、告警接入 `:8099`、可用性巡检、日志采集、突增检测），并由 `scripts/aiops-watchdog.py` 组件守护看护（每 30 秒巡检、崩溃自动拉起）；日常演示建议用 `scripts/demo-up.sh` 一键启动（见 3.1）。

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
.venv/bin/python -m unittest discover -s tests -t . -v       # 期望 832 用例全绿
.venv/bin/python demo_cli.py start --service order --alert-id smoke-001
```

> **更省事的方式**：`bash scripts/demo-up.sh` 一键完成环境准备（Colima/Temporal/Loki 容器）、拉起全部 6 个常驻组件与组件守护、灌入演示日志并发起流程（幂等，已运行组件自动跳过）——详见 3.1。

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
| `release-gate-policy.yaml` | 生产/默认策略 | 审批超时 30m、倒计时 15m、观测 5m |
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
# ① 单元测试（期望：Ran 832 tests ... OK；必须带 -t .，防止误收集 data/ 下演示样例）
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

演示环境推荐用一键脚本编排全部常驻组件（替代逐一手工启动 3.2-3.5 各组件）：

```bash
bash scripts/demo-up.sh            # 一键启动（幂等：已运行组件自动跳过）
bash scripts/demo-up.sh status     # 查看 6 组件 + Temporal/Loki 状态（含局域网控制台地址）
bash scripts/demo-up.sh down       # 停止全部常驻组件（含组件守护）
bash scripts/demo-up.sh down-all   # 停止应用 + 依赖容器（保留数据卷，下次启动更快）
bash scripts/demo-up.sh env        # 仅环境准备（Colima + Temporal/Loki 容器）
```

脚本强制 `AIOPS_MODE=demo`（绝不触达生产 MySQL），按序启动 worker → BFF(:8600) → 告警接入(:8099) → 可用性巡检 → 日志采集 → 突增检测 → **组件守护** → 灌入演示日志 → 发起修复流程。

组件守护 `scripts/aiops-watchdog.py` 每 30 秒巡检 6 个常驻组件，异常自动拉起；崩溃风暴（窗口内 5 次）自动冷却 10 分钟。参数：`AIOPS_WATCHDOG_INTERVAL`（巡检周期）、`AIOPS_WATCHDOG_SURGE_ARGS`（突增检测器拉起参数）。

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

> 通知发送为「先留痕、后 POST」，失败自动降级（`failed`）不阻断修复流程。

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
| `AIOPS_METRICS_PORT` | `9090` | worker | worker 独立指标服务端口（0 关闭）；BFF 指标固定 `:8600/metrics` |
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
| `AIOPS_WATCHDOG_INTERVAL` / `AIOPS_WATCHDOG_SURGE_ARGS` | `30` / 演示灵敏度 | aiops-watchdog | 组件守护巡检周期 / 突增检测器拉起参数 |
| `AIOPS_MONITOR_URL` | `http://localhost:3000/` | prober / BFF | 被监控应用根地址（首次播种，之后在控制台维护） |
| `AIOPS_MONITOR_SERVICE` / `AIOPS_MONITOR_LOG_PATH` | `nl2sql` / 空 | ship_app_logs | 采集 service 标签与日志路径（首次播种） |
| `AIOPS_CODE_INDEX` | `data/code_index.json` | worker | Code RAG 索引路径（仅对未注册清单的 service 生效；已注册自动用专属索引） |
| `AIOPS_EMBED_BATCH_SIZE` | `64` | code_rag | 嵌入分批大小（大仓库拆分请求） |
| `AIOPS_FIX_PROVIDER` | `ollama` | worker | 修复提供者：`ollama`（本地 LLM）/ `qoder`（CLI 无头，见 4.3） |
| `AIOPS_QODER_MODEL` / `_TIMEOUT` / `_MAX_TURNS` | `DeepSeek-Flash` / `720` / `100` | worker（qoder） | Qoder 修复引擎参数（模型 ID 区分大小写，见 4.3） |
| `AIOPS_QODER_ROOT` | `./data/qoder` | worker（qoder） | Qoder 隔离工作区根目录 |

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
| 监控 | servicemonitor + prometheusrule | 抓取 worker `:9090` / BFF `:8600` 指标；6 条告警与 `deploy/prometheus/aiops-alerts.yml` 同步（有测试校验名称集合一致） |

> **探针口径（重要）**：BFF `/api` 前缀全量要求 Bearer 凭证，httpGet 探针无法携带令牌——因此 BFF 探针走**无鉴权的 `/metrics`（8600）**，worker 走 `:9090/metrics`，webhook 用 tcpSocket 8099。生产环境以 NetworkPolicy / Ingress 白名单限制 `/metrics` 暴露面。

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
- 全量单测零回归（b9 验收时 471 用例；当前全量基线 832 用例，含部署清单 39 项静态校验）。

### 5.5 生产接线点清单（按需启用）

| # | 组件 | 实施状态（2026-10） | 生产动作 |
| --- | --- | --- | --- |
| 1 | 沙箱 | Docker 容器真实隔离已落地；K8s Job manifest 渲染保留 | 集群内切换 gVisor runtimeClass 提交 `batch/v1 Job` |
| 2 | 发布 | ArgoCD 真实提交 + Docker 滚动金丝雀已落地 | 配置 `AIOPS_ARGOCD_URL/TOKEN`；镜像前缀指向制品仓库 |
| 3 | 通知 | 飞书/钉钉交互卡片 + 回调解析已落地 | 部署回调网关（`notify.parse_callback` → 校验 → Temporal 信号） |
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
| 1 | 单元测试 | `.venv/bin/python -m unittest discover -s tests -t .` | 832 用例全绿 |
| 2 | 策略校验 | 启动 worker | 打印策略快照；非法 YAML 启动即失败 |
| 3 | 向量检索 | `index_codebase.py --query ...` | 返回相似度有序的代码块/工单 |
| 4 | 双后端一致性（可选） | 分别以 `AIOPS_VECTOR_BACKEND=faiss/python` 检索同一 query | 结果与相似度一致 |
| 5 | 沙箱真实执行 | E2E 流程观察 `[sandbox]` 日志 | `隔离执行[docker-container] Ran N tests` |
| 6 | 金丝雀真实探活 | E2E 流程观察 `[canary]` 日志 | `观测窗口实测 错误率=x% P99=xms`（非模拟值） |
| 7 | 发布滚动 | `docker exec aiops-stable-order python -c "import os;print(os.environ.get('APP_VERSION'))"` 或 `curl :18080/health` | 版本号与最后发布的 `v1.0.x` 一致 |
| 8 | 通知留痕 | `ls data/notify/` | 每次审批/公告产生 `approval-*/broadcast-*.json` |
| 9 | 端到端 | 按《用户操作手册》第 2 章跑通 | `stage=DONE`，`gate_events` 完整 |
| 10 | 异常分支 | `low-conf` / `canary-bad` 关键词各跑一次 | 分别 `ESCALATED`（闸门 1 / 回滚） |
| 11 | 生产冒烟（production 模式） | `.venv/bin/python scripts/production-smoke.py` | 9 项检查全绿（MySQL 建表/审计/令牌指纹/应用 CRUD/workflow_runs/kill switch/清理预演） |
| 12 | K8s 清单静态校验 | `.venv/bin/python -m unittest tests.test_deploy_manifests` | 39 用例（含 144 子断言）全绿 |
| 13 | 组件守护自愈 | `pkill -f app_prober.py` 后等待 ≤30 秒 | 巡检被自动拉起（`bash scripts/demo-up.sh status` 全绿） |

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
| 通知留痕 | `data/notify/*.json` | `approval-*` / `broadcast-*` / `escalation-*` |
| ArgoCD 留痕 | `data/argocd/*.json` | 每次渲染的 Application manifest |
| 沙箱工作区 | `data/sandbox/<patch_id>/` | 应用补丁后的源码（同时作为镜像构建上下文，**请勿手工清理正在使用的目录**） |
| 评测报告 | `data/triage_eval_report.json` / `data/fix_eval_report.json` | 离线评测结果 |
| 索引 | `data/code_index.json` / `.faiss` | 由 `index_codebase.py` 生成/重建；专属索引 `data/code_index_<service>.json` 由修复检索自动构建 |
| BFF 日志 | `bff.log` | 控制台 HTTP 访问/错误、流程同步 |
| 巡检状态 | `app_prober.log` / `data/probe_status.json` | 可用性巡检结果（控制台透出） |
| 采集位点 | `ship_app_logs.log` / `data/log_ship_positions.json` | 日志采集进度（重启不重复推送） |
| 突增检测 | `log_surge_detector.log` | 每轮统计与告警注入 |
| 组件守护 | `aiops_watchdog.log` | 组件巡检与自动拉起记录 |
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

### 7.4 数据清理建议

- 可安全清理：`data/notify/`、`data/argocd/`（审计归档后）、`data/sandbox-eval/`；旧 `data/sandbox/<patch_id>/`（确认无镜像构建引用后）；
- 谨慎清理：`data/code_index.*`（清理后检索降级为空引用，需重建）；`data/sandbox/` 根目录勿整体删除（发布链路会复用最近工作区）。

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
| 端口冲突 | 7233（Temporal）/ 11434（Ollama）/ 8600（BFF）/ 9090（worker 指标）/ 8099（webhook）/ 18080（stable）/ 3101（Loki）/ 9093（Alertmanager）/ 3316（MySQL）/ 6380（Redis）；用 `lsof -i :<port>` 定位 |

---

## 附录 A 工程目录结构

```text
aiops-agent/
├── aiops_agent/                # Python 包（业务核心，27 模块）
│   ├── workflows.py            # 主工作流（状态机/闸门/队列/审计）
│   ├── activities.py           # 13 个 Temporal activity
│   ├── worker.py / config.py / models.py / mode.py
│   ├── triage.py / logs.py / page_health.py       # 分析层（含通用白屏判据）
│   ├── code_rag.py / fix_agent.py / qoder_fix.py  # 决策层（Ollama / Qoder 双修复提供者）
│   ├── sandbox.py / release.py                    # 验证层 / 执行层（Docker 沙箱、金丝雀滚动）
│   ├── notify.py / git_integration.py / requirements_client.py / requirement_agent.py
│   ├── app_manifest.py / app_registry.py / changes.py
│   └── db.py / kill_switch.py / metrics.py / cleanup.py / tracing.py
├── bff/                        # 控制台 BFF（FastAPI :8600；聚合 Temporal + 磁盘留痕）
│   └── routes/                 # 11 域路由：flows / approvals / audit / health / monitored_apps /
│                               # system / tokens / escalations / requirements / users / ops_metrics
├── web/                        # React SPA（vite build → web/dist，由 BFF 同源托管）
├── demo-app/                   # 演示被修复的服务（源码 + tests + Dockerfile + http_server）
├── scripts/                    # demo-up.sh（一键编排）/ aiops-watchdog.py（组件守护）/
│                               # production-smoke.py / security-scan.sh / scenarios/**（8 场景剧本）
├── tests/                      # 单元测试（37 个测试文件 / 832 用例）
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
| 9090 | worker 指标服务 | `AIOPS_METRICS_PORT`（0 关闭） |
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
