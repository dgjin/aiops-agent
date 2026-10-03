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
| Ollama | 本地 LLM | `localhost:11434` | `ollama pull qwen3:8b && ollama serve` |
| Docker | 沙箱测试 / 金丝雀发布 | — | 本机已装（colima 或 Docker Desktop） |

> 依赖是否就绪，可随时运行 `python demo_cli.py doctor` 一键自检（见下文）。

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
bash scripts/demo-up.sh        # 探活依赖 → 启动 worker+BFF → 灌日志 → 起流程
bash scripts/demo-up.sh down   # 停止 worker 与 BFF
```

脚本会自动完成依赖探活、后台启动 worker 与 BFF、灌入演示日志、启动一个修复流程，并打印控制台地址与后续操作提示。

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
| 灌演示日志 | `demo_log_generator.py --service order --mode surge --count 800` |

演示分支（`start --description` 带关键词）：`low-conf`（置信度不足转人工）、`protected`（受保护目录需二级审批）、`test-fail`（测试首败回炉）、`test-always-fail`（重试耗尽转人工）、`canary-bad`（金丝雀劣化自动回滚）。

---

## 六、配置说明

- **策略文件**：`AIOPS_POLICY_PATH` 指定。演示用 `./demo-policy.yaml`（加速：审批 5 分钟、公告 30 秒、观测 10 秒）；生产用 `./release-gate-policy.yaml`。**worker / BFF / demo_cli 必须用同一份**，否则策略快照错配。
- **环境变量**：全部见 `.env.example`（复制为 `.env` 后按需修改，支持自动加载）。
- **端口约定**：控制台 8600、Temporal 7233、Loki 3101、稳定版服务 18080、告警 Webhook 8099。

---

## 七、目录结构

```
aiops-agent/
├── aiops_agent/        # 核心包：workflows / activities / config / models / sandbox / release / notify / code_rag / fix_agent / triage / logs
├── bff/                # 运维控制台 BFF（FastAPI，聚合 Temporal + data 产物）
├── web/                # 运维控制台前端（React 19 + Vite + TS + Tailwind 深色主题）
├── demo-app/           # 被修复的示例应用（order 服务）
├── tests/              # 单元测试（unittest）
├── data/               # 运行产物（索引 / 沙箱 / 留痕 / 审计）
├── scripts/            # 一键演示与剧本脚本
├── demo_cli.py         # 演示 CLI（启动流程 / 发信号 / 自检）
├── demo_log_generator.py  # 演示日志生成器
├── demo-policy.yaml    # 演示加速策略
└── release-gate-policy.yaml  # 生产闸门策略
```

---

## 八、运行测试

```bash
cd aiops-agent
.venv/bin/python -m unittest discover -s tests
```
