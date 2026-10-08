# AIOps 自动运维智能体

> 告警接入 → 日志聚类 → LLM 根因 → Code RAG 修复 → 沙箱验证 → 三道闸门审批 → 金丝雀发布，由 Temporal 编排的端到端自动运维闭环，含深色运维控制台。

本 README 只讲**最小可跑路径**（10 分钟跑通演示）。完整架构见《自动运维智能体系统设计说明书》，运维操作见《自动运维智能体系统用户操作手册》，生产部署见《自动运维智能体系统部署手册》。

---

## 一、前置依赖

| 依赖 | 用途 | 默认地址 | 启动方式 |
|---|---|---|---|
| Python 3.11+ | 后端运行 | — | 本项目用 `.venv` |
| Node 18+ | 前端构建 | — | 仅首次构建需要 |
| Temporal server | 工作流编排 | `localhost:7233` | `docker compose up -d temporal` |
| Loki | 日志存储 | `localhost:3101` | `docker compose up -d loki` |
| Ollama | 本地 LLM | `localhost:11434` | `ollama pull qwen3.8:27b-mlx && ollama serve` |
| Docker | 沙箱测试 / 金丝雀发布 | — | 本机已装（colima 或 Docker Desktop） |

> 依赖是否就绪，可随时运行 `python demo_cli.py doctor` 一键自检（见下文）。
>
> 若 `docker compose` 子命令不可用（部分环境只装了独立版），用等价的 `docker-compose up -d`。
>
> **数据持久化**：Temporal 工作流历史落在命名卷 `aiops-agent_temporal-data`（SQLite），Loki 日志落在 `aiops-agent_loki-data`。容器重启 / 重建后数据仍在；`docker compose down` 默认保留卷，只有 `docker compose down -v` 才会清空。

---

## 二、快速开始（Quickstart）

```bash
# 0. 进入工程目录
cd aiops-agent

# 1. 安装后端依赖（一次性）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 构建前端（一次性，产出 web/dist 供 BFF 托管）
cd web && npm install && npm run build && cd ..

# 3. 启动基础设施（Temporal + Loki）
docker compose up -d

# 4. 启动 worker（终端 1，保持运行）
AIOPS_POLICY_PATH=./demo-policy.yaml .venv/bin/python -m aiops_agent.worker

# 5. 启动 BFF 控制台（终端 2，保持运行）
AIOPS_POLICY_PATH=./demo-policy.yaml .venv/bin/uvicorn bff.app:app --host 127.0.0.1 --port 8600
```

打开控制台：**http://127.0.0.1:8600**

### 跑一个真实修复流程（终端 3）

```bash
cd aiops-agent
# 关键前置：先灌入故障日志（否则证据为空，会被闸门1 直接转人工）
.venv/bin/python demo_log_generator.py --service order --mode surge --count 800

# 启动流程
AIOPS_POLICY_PATH=./demo-policy.yaml .venv/bin/python demo_cli.py start \
    --service order --alert-id demo-001 --description "首次演示"
```

然后到控制台 **审批中心** 点「批准」→ 进入 **发布窗口** 看 30 秒倒计时 → 点「立即发布」→ 在 **流程详情** 看完整证据链（根因 / 补丁 diff / 测试报告 / 发布结果）。

> **更省事**：第 4–6 步可用一键脚本代替，见下节。

---

## 三、一键演示脚本

```bash
cd aiops-agent
bash scripts/demo-up.sh        # 探活依赖 → 启动 6 个常驻组件 + 组件守护 → 灌日志 → 起流程
bash scripts/demo-up.sh down   # 停止全部（先停组件守护再停组件）
```

脚本会自动完成依赖探活，后台常驻启动 worker / BFF / 告警接入 / 可用性巡检 / **日志采集（ship_app_logs --follow）** / **错误日志突增检测（log_surge_detector）**，再灌入演示日志、启动一个修复流程，并打印控制台地址与后续操作提示。第 8 步额外启动**组件守护（aiops-watchdog.py）**：每 30 秒巡检 6 个组件，任一崩溃自动拉起；同一组件在窗口内反复崩溃时自动冷却（防拉起风暴）。状态可用 `python3 scripts/aiops-watchdog.py --status` 单独查看。

---

## 四、环境自检（doctor）

不确定环境是否就绪？运行：

```bash
cd aiops-agent
.venv/bin/python demo_cli.py doctor
```

输出逐项 ✅/❌ 检查结果（Temporal / Loki / Ollama / Docker / 策略 / 前端构建 / 日志新鲜度），并给出修复建议。

---

## 五、常用命令速查

| 操作 | 命令 |
|---|---|
| 启动流程 | `demo_cli.py start --service order --alert-id <id>` |
| 查询状态 | `demo_cli.py status --wf-id <wf_id>` |
| 一级审批 | `demo_cli.py approve --wf-id <wf_id> [--decision approve\|reject]` |
| 二级审批 | `demo_cli.py second-approve --wf-id <wf_id>` |
| 立即发布 | `demo_cli.py deploy-now --wf-id <wf_id>` |
| 取消公告 | `demo_cli.py cancel --wf-id <wf_id>` |
| 窗口排队补丁 | `demo_cli.py queue-patch --wf-id <wf_id> --new-wf-id <id> --service <svc> --alert-id <aid>` |
| 环境自检 | `demo_cli.py doctor` |
| 突增检测（单轮） | `log_surge_detector.py --once` |
| 组件守护状态 | `python3 scripts/aiops-watchdog.py --status` |
| 需求基线分析 | `analyze_requirements.py --url http://localhost:3000` |
| 灌演示日志 | `demo_log_generator.py --service order --mode surge --count 800` |

演示分支（`start --description` 带关键词）：`low-conf`（置信度不足转人工）、`protected`（受保护目录需二级审批）、`test-fail`（测试首败回炉）、`test-always-fail`（重试耗尽转人工）、`canary-bad`（金丝雀劣化自动回滚）。

---

## 六、配置说明

- **策略文件**：`AIOPS_POLICY_PATH` 指定。演示用 `./demo-policy.yaml`（加速：审批 5 分钟、公告 30 秒、观测 10 秒）；生产用 `./release-gate-policy.yaml`。**worker / BFF / demo_cli 必须用同一份**，否则策略快照错配。
- **环境变量**：全部见 `.env.example`（复制为 `.env` 后按需修改，支持自动加载）。
- **端口约定**：控制台 8600、Temporal 7233、Loki 3101、稳定版服务 18080、告警 Webhook 8099。
- **控制台鉴权（fail-closed + 自动轮换）**：全部 `/api` 接口需 `Authorization: Bearer <token>`。角色 `viewer`（读）/ `operator`（+审批、发布指令、排队）/ `admin`（+被监控应用维护、令牌轮换）。**未配置时所有 `/api` 请求返回 503**。

  令牌**运行时注册表**在 `data/console_tokens.json`（权限 600，含明文令牌，请勿提交）；`AIOPS_CONSOLE_AUTH_TOKENS` 仅作**首次播种**。系统按 `AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS`（默认 30 天）**自动轮换**：为每个身份生成新令牌，旧令牌转 `previous` 并在 `AIOPS_TOKEN_GRACE_SECONDS`（默认 7 天）**宽限期内继续有效**——所以自动轮换不会把在用的运维锁在门外。间隔设 `0` 关闭。

  > **本工程的当前档位：关闭自动轮换。** `.env` 设了 `AIOPS_TOKEN_ROTATE_INTERVAL_SECONDS=0`，令牌固定为 `.env` 播种的三枚（`tok-view`/viewer01、`tok-ops`/zhang、`tok-adm`/li），**不再定期失效**。改这个开关后需重启 BFF，并删除 `data/console_tokens.json` 让它按新档位重新播种（否则注册表里仍留着旧的 `interval_seconds`）。

  管理员可在控制台左下角「访问令牌」处「立即轮换」——**该入口仅当自动轮换开启时才显示**（关闭轮换的部署里点它只会把令牌换成"界面取不回来"的新值）；也可直接调用 `GET /api/auth/status`（自身令牌状态）、`GET /api/auth/tokens`（清单，仅元数据）、`POST /api/auth/rotate`。令牌值默认**不经接口回显**，从注册表文件读取（`AIOPS_TOKEN_ALLOW_REVEAL=true` 可开启回显，属降低安全性）。

  > **两种操作语义不同，别搞混**（踩过坑）：
  > - **轮换**（控制台「立即轮换」或 `POST /api/auth/rotate`）——**平滑替换**：新令牌生效，旧令牌转 `previous` 并在宽限期内继续可用，不会把在用的人锁在门外，也不会让人手上的令牌立刻失效。
  > - **删除注册表**（`rm data/console_tokens.json`）——**吊销全部令牌**：下次 `/api` 请求会用 `AIOPS_CONSOLE_AUTH_TOKENS` 重新播种，**此前发出/复制的所有令牌立即作废**。仅在需要"重置为环境变量里的令牌"时使用。
  >
  > 排查 401 时：先 `GET /api/auth/tokens` 看清单与状态；若刚做过删除重置，请改用 `.env` / `.env.example` 里记载的播种令牌。
  >
  > **注意**：`/api/auth/tokens` 返回的 `id` 是令牌的 **sha256 指纹（前 8 位）**，只用来区分不同令牌，**不是令牌、不能填进「访问令牌」**（填指纹必然 401「凭证无效或已过期」）。要在关闭回显时取真令牌，只能读 `data/console_tokens.json` 里对应记录的 `token` 字段。
- **被监控应用**：`AIOPS_MONITOR_URL`（默认 `http://localhost:3000/`）。控制台「系统状态 → 被监控应用」会探测该地址并展示可达性/状态码/延迟（能收到任何 HTTP 响应即视为在线；条目可选配「页面关键字」，配置后响应内容须包含该关键字才算在线——可发现「端口活着但页面白屏」类故障）。应用部署 AIOps 标准接口（Manifest v1.0）后，新增条目可「从标准接口探测」自动预填接入配置（含健康检查路径 `health_path`，探测/巡检按该路径发起关键字校验）。

### 接入被监控应用（日志 + 代码索引）

把真实应用（如 `智能问数据分析系统` / `nl2sql`）纳入监控与修复链路。**推荐路径：标准接口自动适配** —— 应用部署 [AIOps 标准接口（Manifest v1.0）](../AIOps%20被监控系统标准接口改造方案.md)（`GET /.well-known/aiops.json`）后，控制台「被监控应用 → 新增 → 从标准接口探测」一键预填（名称 / service / 健康关键字 / 健康路径 / 日志路径）；保存后探测、巡检、日志采集、突增检测、修复解析各链路**热生效**，代码索引在首次修复检索时**自动构建**——无需手工建索引、export 环境变量或重启进程。改造方案、字段规范与验收清单一文交底：**《AIOps 被监控系统标准接口改造方案》**（工作区根目录）。

```bash
# 0. 接入前体检：校验应用的 Manifest / 健康页 / 关键字（--extended 再查指标与日志文件）
.venv/bin/python check_aiops_interface.py --url http://localhost:3000 --extended

# 1. 日志接入 AIOps Loki（扮演 Filebeat 角色；增量位点记录在 data/log_ship_positions.json）
#    推荐：清单模式 —— 采集目标来自控制台「被监控应用」清单，每轮重读 → 改动热生效
.venv/bin/python ship_app_logs.py --from-start --once   # 首次全量
.venv/bin/python ship_app_logs.py --follow              # 持续采集

#    兼容：单文件模式（显式指定文件与 service）
.venv/bin/python ship_app_logs.py --file <app>/logs/app_server.log --service nl2sql --once

# 2. 错误日志突增实时检测（ERROR 突增 → Alertmanager → 自动发起修复流程；冷却 300s 防抖）
#    缺省同样按「被监控应用」清单动态取 service，每轮重读 → 热生效
.venv/bin/python log_surge_detector.py --once           # 单轮（调试/验证）
.venv/bin/python log_surge_detector.py                  # 守护运行（默认 15s 间隔）

# 3. 代码索引：无需人工步骤 —— 在控制台为该应用登记「修复仓库路径」（repo）后，
#    首次修复检索自动构建 data/code_index_<service>.json（仓库变更自动重建）；
#    兼容项 AIOPS_CODE_INDEX 仅对「未注册到清单」的 service 生效。
```

采集器会规范化应用日志：JSON 行取 `level`、把 `requestId` 注入为 `trace_id=<id>`、正文带 `module` 前缀；非 JSON 行按正则识别级别。因此既有 `logs.query_lines()`、Drain3 聚类与突增检测**无需改动**即可工作。

一键脚本已常驻拉起上述两件套：**被监控应用日志 → Loki → 突增检测 → 自动发起修复流程** 全链路无需人工干预。演示突增灵敏度为 `--min-lines 5 --factor 2`，生产建议 `--min-lines 100 --factor 3`。

### 主动需求分析（可选）

被监控应用部署「需求收集与反馈」能力后（标准导出接口 `GET /api/requirements/export`，Manifest 以 `requirements_path` 声明，见[改造方案第七章](../AIOps%20被监控系统标准接口改造方案.md)），AIOps 可拉取**已由管理员评估并纳入基线**的需求 / 建议条目做主动分析（优先级研判 / 实现建议 / 排期参考）：

```bash
# 拉取基线需求 → 本地分析（类型 / 优先级分布、P0/P1 高优提示）→ Markdown 报告（data/requirements_report.md）
.venv/bin/python analyze_requirements.py --url http://localhost:3000

# 机器可读：stdout 输出 JSON（不写报告文件）；--since 增量拉取（>= 语义）
.venv/bin/python analyze_requirements.py --url http://localhost:3000 --since 2026-10-01 --json
```

令牌：`--token` 或环境变量 `NL2SQL_OPS_TOKEN`（值 = 被监控系统的 `OPS_API_TOKEN`）。客户端实现 `aiops_agent/requirements_client.py` 标准库零依赖、永不抛异常；接口自描述见 `GET /.well-known/requirements.json`。

控制台侧另有**「需求基线」页**（侧栏入口，只读、`viewer` 起可访问）：数据源与令牌同上（BFF 读 `NL2SQL_OPS_TOKEN`），支持多被监控应用切换、类型 / 优先级 / 提交部门分布与全清单展示，P0 / P1 高优条目置顶；未配置令牌或应用不可达时，页面直接给出中文配置指引。

### Qoder 修复引擎（可选）

修复环节默认用本地 Ollama 生成补丁；也可切换为 **Qoder CLI 无头调用**（Qoder 在隔离工作区内自主读代码并改代码，改动经 `git diff` 采集后接入同一套校验/沙箱/闸门）。完整设计见《AIOps 自动运维智能体系统 Qoder 修复引擎接入设计》。

```bash
# 1. 安装并验证 Qoder CLI（命令名为 qodercli，通常在 ~/.local/bin）
curl -fsSL https://qoder.com/install | bash
qodercli --version

# 2. 鉴权（任选其一）
export QODER_PERSONAL_ACCESS_TOKEN="<PAT>"   # A) 无人值守；PAT 取自 https://qoder.com/account/integrations
# qodercli login                             # B) 设备码流程，浏览器授权一次后复用登录态

# 3. 切换修复提供者（worker 进程需带上该变量）
export AIOPS_FIX_PROVIDER=qoder
export AIOPS_QODER_BIN="$HOME/.local/bin/qodercli"   # 建议显式指定
```

实测（真实 Qoder CLI v1.1.65）：隔离工作区内自主修复 50.5s，产出正确的最小 diff，真实 Docker 沙箱 6/6 通过，真实代码目录零改动。

安全边界：Qoder 只在 `data/qoder/<patch_id>/` 隔离副本内改动（真实代码目录不被触碰），以 `--permission-mode accept_edits` + 工具白名单运行（不使用 `--yolo`）；其产出的补丁仍须通过编译校验、SAST、沙箱测试与三道闸门。Qoder 不可用/超时/无改动时自动回落到确定性兜底补丁，流程不中断。

> 超时关系（重要）：`AIOPS_QODER_TIMEOUT`（子进程超时，默认 180s）必须 **小于** 修复活动的 Temporal 超时 `_FIX_ACTIVITY_TIMEOUT`（`aiops_agent/workflows.py`，300s），否则会被 Temporal 直接取消（硬失败，无法降级）。

> 模型（重要）：默认修复模型为 **`DeepSeek-Flash`**，可用 `AIOPS_QODER_MODEL` 覆盖，可选值见 `qodercli --list-models`。模型 ID **区分大小写**：写错（如小写 `deepseek-flash`）不会报错，Qoder 会**静默回退 `auto`**；接入层已捕获该警告并写入 `meta.model_warning` 与运行留痕 `data/qoder/*.run.json`。

> 沙箱与 Qoder 工作区须位于**容器可挂载路径**：colima 默认仅挂载 `$HOME`，把 `AIOPS_QODER_ROOT` / 沙箱目录放在 `/tmp` 会导致容器内工作区为空（测试报 `Start directory is not importable`）。

---

## 七、目录结构

```
aiops-agent/
├── aiops_agent/        # 核心包：workflows / activities / config / mode / db / models / sandbox / metrics / kill_switch / cleanup / git_integration / release / notify / code_rag / fix_agent / qoder_fix / triage / logs / requirements_client
├── bff/                # 运维控制台 BFF（FastAPI；routes/ 按域拆分 + middleware 鉴权/限速/指标）
├── web/                # 运维控制台前端（React 19 + Vite + TS + Tailwind 深色主题）
├── demo-app/           # 被修复的示例应用（order 服务）
├── tests/              # 单元测试（unittest；663 用例）
├── data/               # 运行产物（索引 / 沙箱 / 留痕 / 审计）；生产数据在 data/prod/ 或 MySQL
├── scripts/            # 演示脚本 + production-smoke.py（生产冒烟）+ security-scan.sh（SAST）
├── deploy/             # 生产交付物：Dockerfile / k8s manifests / prometheus / grafana
├── demo_cli.py         # 演示 CLI（启动流程 / 发信号 / 自检）
├── analyze_requirements.py # 需求基线主动分析 CLI（拉取基线条目 → 报告 / JSON）
├── demo_log_generator.py  # 演示日志生成器
├── demo-policy.yaml    # 演示加速策略
└── release-gate-policy.yaml  # 生产闸门策略
```

---

## 八、运行测试

```bash
cd aiops-agent
.venv/bin/python -m unittest discover -s tests -t .
```

> `-t .`（顶层目录=仓库根）让测试以 `tests.*` 包方式导入，先执行 `tests/__init__.py` 隔离钩子（强制 demo 模式，测试**永不**连生产库）；省略 `-t .` 会按顶层模块导入并跳过该钩子（曾实测把测试数据写进生产 MySQL）。pytest 方式由 `tests/conftest.py` 同款覆盖。

同样的三道门禁已固化到 CI（`.github/workflows/ci.yml`）：单测（demo 档 + 强制 `-t .`）、前端构建（`tsc + vite`）、Bandit SAST（Medium+，HIGH=0）——推送/PR `main` 自动执行。

---

## 九、生产化部署（企业级）

同一套代码支持 **双模运行**：`production`（默认，MySQL/Redis 持久化 + 全量鉴权/限速/指标）与 `demo`（零依赖文件后端，保留既有演示数据），互不影响。

### 9.1 模式差异与切换

| 维度 | production（默认） | demo |
|---|---|---|
| 切换方式 | 不设或 `AIOPS_MODE=production` | `AIOPS_MODE=demo` |
| 持久化 | MySQL 5 表（system_kv / console_tokens / monitored_apps / audit_events / workflow_runs） | 文件后端（`data/` 下 JSON/JSONL，零迁移） |
| 鉴权令牌注册表 | DB 存储 **sha256 指纹** | 文件明文键（既有行为） |
| 限速 | 读 240/min、写 20/min、认证失败 10/min（Redis，IP+令牌维度） | 同左（无 Redis 时自动退化为进程内计数） |
| DB 未配置 | **fail-fast 拒绝启动**（防误以演示档上生产） | 不触达数据库 |

> 演示档起停脚本（`scripts/demo-up.sh`）已自动带 `AIOPS_MODE=demo`，生产模式不受其影响。

### 9.2 本机生产化运行（compose prod profile）

```bash
cd aiops-agent
docker compose --profile prod up -d          # MySQL(3316) + Redis(6380)，端口已避让宿主机常见占用

export AIOPS_MODE=production
export AIOPS_DATABASE_URL='mysql+pymysql://aiops:aiops-demo-pass@127.0.0.1:3316/aiops?charset=utf8mb4'
export AIOPS_REDIS_URL='redis://127.0.0.1:6380/0'
export AIOPS_POLICY_PATH=./release-gate-policy.yaml
export AIOPS_CONSOLE_AUTH_TOKENS='{"<强随机令牌>":{"user":"admin","role":"admin"}}'   # 仅首次播种

# 启动 worker / BFF 后，跑生产冒烟（9 项：建表/审计/令牌指纹鉴权/应用 CRUD/workflow_runs/kill switch/清理预演）
.venv/bin/python scripts/production-smoke.py
```

### 9.3 可观测（Prometheus + Grafana）

```bash
docker compose --profile obs up -d           # Prometheus(9095) + Grafana(3200, admin/aiops-demo)
```

自动加载 `deploy/grafana/aiops-overview.json`（9 面板：成功率 / 各 stage 速率 / 耗时 p95 / 闸门决策 / 修复提供者 / 沙箱通过率 / 金丝雀 / BFF 速率与延迟）与 `deploy/prometheus/aiops-alerts.yml`（6 条告警）。worker 暴露 `:9090/metrics`、BFF 暴露 `:8600/metrics`（无鉴权，供探针与抓取）。

### 9.4 Kubernetes 一键部署

```bash
kubectl apply -f deploy/k8s/                 # 平铺清单（勿放入子目录，apply -f 不递归）
```

- 命名空间 `aiops-system`；镜像统一 `aiops-agent:1.0.0`（构建：`docker build -f deploy/Dockerfile -t aiops-agent:1.0.0 .`，网络受限可加 `--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`）
- 工作负载 2 副本 + PDB（minAvailable 1）+ HPA（CPU 70%，2→6）；MySQL/Redis/Temporal（auto-setup, retention 720h）内置
- Ingress（`aiops.example.com` + cert-manager TLS）、NetworkPolicy default-deny（8 条最小化规则）
- **密钥**：先复制 `deploy/k8s/secret.example.yaml` 为 `secret.yaml` 填入真实值（DB 口令 / Temporal / `AIOPS_CONSOLE_AUTH_TOKENS` / `AIOPS_DATABASE_URL` / `AIOPS_REDIS_URL` / 可选 `AIOPS_GIT_TOKEN`）再 apply；`secret.yaml` 已被 .gitignore 排除
- **探针说明**：BFF 探针走无鉴权的 `/metrics`（`/api` 前缀全量要求 Bearer，httpGet 无法携带）

### 9.5 数据清理（TTL）

生产保留期：审计/执行记录 365 天、通知/ArgoCD 留痕 90 天、沙箱/Qoder 工作区 7 天（演示档更短）。

```bash
.venv/bin/python -m aiops_agent.cleanup --dry-run    # 预演（生产 CronJob 每日 03:00 自动执行真删）
.venv/bin/python demo_cli.py cleanup --dry-run       # 演示/手动
```

K8s 内置 CronJob `aiops-cleanup`（`0 3 * * *`），清理错误以非零退出码暴露在 Job 状态中。

### 9.6 MR 真实创建（可选）

在 `release-gate-policy.yaml` 取消 `git:` 段注释并配置 `repo_url`（GitLab / GitHub / Gitee 三平台自动识别），经环境变量 `AIOPS_GIT_TOKEN` 注入令牌（K8s 对应 Secret `aiops-git-token`）。未配置 → `recorded` 桩；调用失败 → `degraded` 桩 + 原因，**流程永不因 MR 失败中断**（闸门 2 人工审批兜底）。

### 9.7 安全基线

```bash
bash scripts/security-scan.sh                # Bandit（Medium+；发布门禁要求 HIGH=0）
```

- 控制台鉴权 fail-closed；未配置令牌时 `/api` 全部 503
- 告警接入鉴权（P0-1）：IP 白名单 + 共享密钥（`Authorization: Bearer` / `X-AIOps-Token`）+ 可选 HMAC 签名（`X-AIOps-Signature`）+ 限速（默认 120 次/分钟/来源 IP）；production 档未配置密钥时 `/webhook` 全部 503（fail-closed）。两端配置：接入服务读 `AIOPS_WEBHOOK_TOKEN`；Alertmanager 侧执行 `monitoring/apply_alertmanager_route.py --set-webhook-token <同一令牌>` 写入 receiver 的 `http_config.authorization` 并重启容器
- 写操作限速 + 认证失败限速（Redis）；安全响应头（HSTS 等，TLS 终结时按 `X-Forwarded-Proto` 附加）
- 杀开关（kill switch）：`POST /api/system/kill-switch`（admin，body `{"active":true,"reason":"..."}`）激活后 BFF 拒绝一切写操作、webhook 拒绝新流程；控制台「系统状态」页可一键操作
- 生产冒烟 `scripts/production-smoke.py` 9 项全绿方可发布