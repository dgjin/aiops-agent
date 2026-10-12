# AIOps 自动运维智能体系统 Qoder 修复引擎接入设计

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.0 |
| 日期 | 2026-10-03 |
| 状态 | 待评审 |
| 关联文档 | 《AIOps 自动运维智能体系统设计方案》v1.0、《设计说明书》v1.0、《实施计划》v1.0、《部署手册》v1.0 |
| 适用范围 | 在既有 AIOps 自动运维闭环中，把「补丁生成」环节从本地 Ollama 小模型切换/增强为 Qoder CLI 无头调用 |
| 交付范围 | **仅新增 Qoder 接入层**：新增 1 个模块 + 1 个配置开关 + 1 组单测；不改动既有五层架构、三道闸门、Temporal 工作流与沙箱/发布逻辑 |

---

## 1 背景与目标

### 1.1 背景

既有 AIOps 自动运维智能体（`aiops-agent/`）已实现「告警接入 → 日志聚类 → LLM 根因 → Code RAG 修复 → 沙箱验证 → 三道闸门 → 金丝雀发布」的完整闭环。其中「补丁生成」由 `fix_agent.py` 通过本地 Ollama 小模型（默认 `qwen3:8b`）生成 unified diff。

离线评测（《设计说明书》8.3）显示：小模型在 diff 语义正确率上存在明显瓶颈（`qwen3:8b` 沙箱通过率 0.0，换用 27B 才到 1.0）。根因是**小模型缺少对真实代码仓库的自主探索能力**——它只看到 prompt 里拼接的单个目标文件，无法自行检索调用方、类型定义、既有测试与仓库约定。

Qoder CLI 是面向终端的 AI 编程助手，可自主读取文件、调用工具、执行命令，天然具备「仓库级理解 + 多轮自主修改」的能力，恰好补齐这一环。

### 1.2 目标

1. 在**不破坏既有闭环**的前提下，新增「Qoder 修复提供者」，让修复环节可切换为 Qoder CLI 无头调用；
2. Qoder 在**隔离工作区**内自主阅读代码并产出改动，改动用**确定性方式**（`git diff`）取回，接入既有 diff 应用与编译校验管线；
3. 复用既有全部安全护栏与三道闸门（本接入层不新增、不削弱任何闸门）；
4. 全链路**可降级**：Qoder 不可用/超时/产出非法时，安全侧回落到既有确定性兜底补丁，流程不中断。

### 1.3 非目标

- 不改动告警接入、日志聚类、根因分析、沙箱测试、审批、发布各模块；
- 不改变三道闸门语义与审批/公告/金丝雀策略；
- 不替代既有 Ollama 链路——两者**共存**，由配置开关选择，默认仍为 Ollama（零行为变更）；
- 不引入 Qoder 私有 JSON 输出格式作为契约（该格式未公开）。

---

## 2 接入点定位

现有修复链路（`fix_agent.run_fix`）：

```text
run_fix(alert, root_cause, references, attempt)
  ├─ locate_target_file()            # 嫌疑文件模糊定位（复用，不改）
  ├─ build_fix_prompt()              # 构造 prompt（Qoder 复用同一语义）
  ├─ call_ollama() ──► raw 文本      # ◀── 唯一被替换的环节
  ├─ PatchOutput.model_validate_json # 结构化校验（复用，不改）
  ├─ apply_unified_diff()            # 应用 diff（复用，不改）
  ├─ validate_source()               # py_compile 编译校验（复用，不改）
  └─ 异常 → fallback_patch()         # 安全侧兜底（复用，不改）
```

**接入策略：只替换「生成候选改动」这一个环节，其余全部复用。**

即引入提供者抽象，`ollama` 与 `qoder` 两个实现产出同构的 `PatchOutput{diff, description, risk}`，下游（应用、编译校验、沙箱、审批）完全无感知。

> 关键定位：Qoder 负责「提出改动」，**不负责判定改动是否正确**。正确性仍由既有的沙箱测试 + 三道闸门裁决——这是本接入层不削弱安全性的根本保证。

---

## 3 提供者抽象与接口契约

### 3.1 配置开关

| 环境变量 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `AIOPS_FIX_PROVIDER` | `ollama` \| `qoder` | `ollama` | 修复提供者；默认 `ollama`，行为与现状完全一致 |

### 3.2 统一契约

两个提供者均实现同一语义：给定 `(alert, root_cause, references, attempt, repo_dir, target_rel)`，返回 `(PatchOutput, meta)`；失败一律抛异常，由 `run_fix` 统一走兜底。

`meta` 在既有字段基础上新增 `provider`，并保留 `degraded/validated/reason/generations/elapsed_seconds` 语义。

### 3.3 新增模块

```text
aiops_agent/qoder_fix.py      # 新增：Qoder CLI 无头调用适配器（唯一新模块）
tests/test_qoder_fix.py       # 新增：Qoder 接入层单测
tests/fakes/fake_qodercli.sh  # 新增：测试用 CLI 桩（不依赖真实 Qoder）
```

对既有文件的改动仅限 `fix_agent.py` 内约 40 行的提供者分发逻辑，以及 `activities.py` 日志行新增一个 `provider=` 字段。

---

## 4 Qoder CLI 无头调用契约

以下为 Qoder CLI 官方文档确认的**真实接口**（`docs.qoder.com/zh/cli/*`），非推测。

### 4.1 安装与命令名

```bash
# 推荐：安装脚本（原生二进制，性能更好）
curl -fsSL https://qoder.com/install | bash

# 备选：npm（需 Node >= 20）
npm install -g @qoder-ai/qodercli

# 验证
qoder --version        # 打印版本号即成功
```

安装后的可执行命令名为 **`qoder`**。

### 4.2 无头（Headless）调用

```bash
qoder -p "<prompt>"                    # -p/--print：非交互，输出一次后退出
echo "<prompt>" | qoder -p             # 亦支持 stdin 传入
```

关键参数（本接入层使用）：

| 参数 | 取值 | 本设计用途 |
| --- | --- | --- |
| `-p, --print` | bool | 强制无头模式 |
| `-o, --output-format` | `text`/`json`/`stream-json` | 用 `json` 便于留痕；**不解析其内部 schema** |
| `--permission-mode` | `default`/`plan`/`auto`/`accept_edits`/`dont_ask`/`bypass_permissions` | 用 **`accept_edits`**：自动接受工作区内安全编辑，但不放行 Bash 等高风险操作 |
| `-w, --cwd` | 目录 | 指定隔离工作区为工作目录 |
| `--max-turns` | 数字 | 限制自主轮数，防跑飞，控制成本 |
| `--no-session-persistence` | bool | 仅 `--print` 有效；不落盘会话，保证批量任务无状态、可复现 |
| `--allowed-tools` | 工具名列表 | 白名单（只读 + 编辑类），最小权限 |
| `--disallowed-tools` | 工具名列表 | 黑名单（如 Bash），双保险 |
| `-m, --model` | 模型 ID | 指定修复模型（`auto`/`lite`/`performance`/BYOK） |

> **不使用 `--yolo` / `--dangerously-skip-permissions`**：等价于 `bypass_permissions`，会让 Qoder 跳过一切确认（含执行任意命令），与「最小权限」原则冲突。

### 4.3 鉴权（CI / 无人值守）

```bash
export QODER_PERSONAL_ACCESS_TOKEN="<PAT>"   # 设置后自动使用 PAT 认证
```

- PAT 获取地址：`https://qoder.com/account/integrations`；
- 环境存在 PAT 时优先于交互式 `/login`；
- 本接入层支持两种注入方式：进程环境已有 `QODER_PERSONAL_ACCESS_TOKEN`（推荐，由 CI Secret 注入），或通过 `AIOPS_QODER_TOKEN` 显式传入（适配器内部转写，便于统一配置管理）。

### 4.4 两个「不依赖」原则（重要，已实测验证）

官方文档**未公开** `--output-format json` 的字段结构，也**未定义**退出码语义。因此本设计：

1. **不解析 Qoder 的 JSON 输出结构**——只用它做留痕；变更来源一律以 `git diff` 为准；
2. **不依赖退出码判定成败**——成败一律以「工作区是否产生了合法 diff」为准，退出码仅作留痕。

**实测（v1.1.65）验证了这两条判断**：未登录时 CLI 输出

```json
{"type":"result","subtype":"success","is_error":true,"num_turns":1,
 "result":"Not logged in · Please run /login","session_id":"…","total_cost_usd":0,
 "total_credits":0,"terminal_reason":"completed", …}
```

**退出码为 0**。即「失败」在退出码上完全不可见——若按退出码判定，会把未登录误判为成功。
接入层因此完全依据 `git diff` 是否有内容来判定，退出码仅写入留痕。

> 附带收益：真实 `--output-format json` 字段已探明（`type/subtype/is_error/result/duration_ms/num_turns/session_id/total_cost_usd/total_credits/usage/modelUsage/permission_denials/terminal_reason/uuid`）。但 `result` 是**自然语言摘要而非补丁**，故仍不作为变更来源；仅可用于后续提取 token/费用元数据。

### 4.5 子进程环境清洗（实测必需）

**实测故障**：若 AIOps worker 运行在 Qoder 自身进程树下，会继承 Qoder 的内部变量，`qodercli` 误判为「Agent SDK 入口」并进入 SDK 模式，直接失败：

```text
Error: sdk_invalid_args: Agent SDK entrypoint env is set but required flags are missing.
Expected --print --input-format stream-json --output-format stream-json, got print=true, inputFormat=undefined, outputFormat=json
```

**已定位触发变量**：`QODER_AGENT_SDK_ENTRYPOINT`（实测值为 `sdk-ts`）。

**处理**：适配器在起子进程前调用 `sanitize_env()`，剔除前缀为 `QODER_AGENT_SDK` / `QODER_WORKER` / `QODER_SDK` / `QODERCLI_RUNTIME` / `QODER_SESSION_` / `QODER_MCP_` / `QODER_CLI_` 及 `FEATURE_FLAGS` 的变量；保留 `QODER_PERSONAL_ACCESS_TOKEN`、`QODER_CONFIG_DIR` 等鉴权与配置变量（不匹配上述前缀）。此逻辑有专门单测回归防护。

### 4.6 活动超时约束（实测必需）

**实测故障**：`generate_patch` 沿用默认活动超时 **60s**（原为本地 LLM 单次生成设计），真实 Qoder 修复（约 50s 起，演示栈内实测单次 208s）超时后被 Temporal **直接取消**：

```text
asyncio.exceptions.CancelledError: Activity cancelled
{'activity_type': 'generate_patch', 'attempt': 3, …}   # 3 次重试全部被取消 → 活动失败
```

**关键区别**：被 Temporal **取消是硬失败**——异常一路冒泡为活动失败并触发重试，**永远无法降级**；只有**子进程自身超时**才会抛 `QoderFixError` → `run_fix` 捕获 → 回落确定性兜底补丁 → 活动正常成功。

**约束**：修复活动的 Temporal 超时必须 **大于** 提供者子进程超时。已加单测守护该不变量。

| 参数 | 位置 | 值 | 说明 |
| --- | --- | --- | --- |
| `_FIX_ACTIVITY_TIMEOUT` | `workflows.py` | **300s** | 修复活动 Temporal 超时（原默认 60s） |
| `AIOPS_QODER_TIMEOUT` | `qoder_fix.py` | 默认 **180s** | Qoder 子进程超时，须 **<** 活动超时 |

### 4.7 模型选择与「静默回退」风险（实测必需）

默认修复模型为 **`DeepSeek-Flash`**（`AIOPS_QODER_MODEL` 可覆盖；函数显式传参优先级最高）。

**实测风险**：Qoder 模型 ID **区分大小写**，且无效 ID **不报错**：

```bash
$ qodercli -p ok -o json -m deepseek-flash        # 小写：无效
{"type":"result","is_error":false,"result":"Hi! What would you like to work on? ..."}   # 退出码 0，结果看似成功
# stderr: Model "deepseek-flash" is not available right now; using "auto" instead.
```

即写错模型名会**静默回退 `auto`**，流程照常跑完，但用的并不是指定的模型；只看退出码或结果文本**完全无法察觉**。

**处理**：适配器用 `_detect_model_warning(stderr)` 捕获该警告，写入：

- `meta["model_warning"]`，并追加到 `meta["reason"]`（出现在 worker 日志与 `gate_events` 留痕）；
- 运行留痕 `data/qoder/<patch_id>.run.json` 的 `model` / `model_warning` 字段。

**取合法模型值**：`qodercli --list-models`。实测可选：`Auto` / `Ultimate` / `Performance` / `Efficient` / `Qwen3.8-Max` / `Qwen3.8-Flash` / `Kimi-K3` / `GLM-5.3` / **`DeepSeek-Flash`** / `DeepSeek-V4-Pro` / `MiniMax-M3` 等。

---

## 5 隔离工作区与变更获取

### 5.1 工作区准备

```text
data/qoder/<patch_id>/
```

1. 复制 `repo_dir`（demo-app）到该目录（复用 `shutil.ignore_patterns`：跳过 `__pycache__`/`.venv`/`.git`/`data`）；
2. 在工作区内 `git init` → `git add -A` → `git commit`（基线提交）；
3. 基线提交保证 `git diff` 只反映 Qoder 的改动。

> 选用「复制 + 自建基线提交」而非 Qoder 的 `--worktree`：工作区自带 git 历史，不依赖外层仓库状态（本仓库当前无任何提交），行为确定、可测试。

### 5.2 变更获取

Qoder 退出后，在隔离工作区内执行：

```bash
git --no-pager diff --no-color HEAD -- <target_rel>
```

产出标准 unified diff（含 `a/`、`b/` 前缀），可直接喂给既有 `apply_unified_diff` 解析器。

- 空 diff → 视为**未产生有效改动**，抛异常走安全侧兜底；
- 只取**目标文件**的改动：约束 Qoder 单点修复，避免顺手重构（与既有 prompt 约束一致）。

### 5.3 隔离与安全

| 维度 | 措施 |
| --- | --- |
| 目录隔离 | 只在 `data/qoder/<patch_id>/` 内改动，**绝不触碰真实代码目录** |
| 工具白名单 | `--allowed-tools` 仅允许读/编辑类工具；`--disallowed-tools` 追加屏蔽 Bash |
| 权限模式 | `accept_edits`（非 `bypass_permissions`） |
| 轮数/时长 | `--max-turns` + 子进程超时双限 |
| 无状态 | `--no-session-persistence` |
| 环境清洗 | 剔除继承自 Qoder 进程的 Agent-SDK 变量（见 4.5），否则 CLI 误入 SDK 模式 |
| 事后校验 | `git diff` 必须能被既有管线应用**且**通过 `py_compile`，否则丢弃 |
| 下游复核 | SAST（禁 `eval/exec/subprocess/…`）+ 沙箱单测 + 三道闸门（全部复用，未削弱） |

---

## 6 无头调用时序

```text
run_fix(provider=qoder)
 │
 ├─1 locate_target_file()                          # 复用既有
 ├─2 prepare_qoder_workspace(repo_dir, patch_id)   # 复制 + git 基线提交
 ├─3 build_qoder_prompt(alert, root_cause, refs, target_rel, retry_hint)
 ├─4 qoder_available()                             # 定位 qoder 可执行文件 + 取版本
 │      └─ 不可用 ─────────────────────────────► 抛异常 → 兜底补丁（degraded）
 ├─5 subprocess.run([
 │       qoder, "-p", <prompt>,
 │       "-o", "json", "--permission-mode", "accept_edits",
 │       "-w", <workspace>, "--max-turns", N,
 │       "--no-session-persistence",
 │       "--allowed-tools", <白名单>, "--disallowed-tools", "Bash",
 │       "-m", <model>],
 │       cwd=workspace, timeout=<T>, env=<含 PAT>)
 │      └─ 超时/非零退出/无改动 ───────────────► 抛异常 → 兜底补丁（degraded）
 ├─6 collect_diff(workspace, target_rel)           # git diff HEAD -- <target>
 │      └─ 空 diff ────────────────────────────► 抛异常 → 兜底补丁（degraded）
 ├─7 组装 PatchOutput{diff, description, risk}     # 描述/风险由 Qoder 运行元数据与根因生成
 ├─8 apply_unified_diff() + validate_source()      # 复用既有双重校验
 │      └─ 失败 ───────────────────────────────► 兜底补丁（degraded）
 └─9 生成 Patch(model_version="qoder-cli:<version>") + meta{provider:"qoder",...}
```

留痕产物（对齐既有 `data/` 审计约定）：

| 产物 | 路径 | 说明 |
| --- | --- | --- |
| Qoder 工作区 | `data/qoder/<patch_id>/` | 改动后源码，供追溯 |
| Qoder 运行留痕 | `data/qoder/<patch_id>.run.json` | 命令、退出码、耗时、stdout/stderr 摘要、diff 摘要 |
| 既有产物 | `data/sandbox/<patch_id>/` 等 | 后续环节不变 |

---

## 7 Prompt 设计

复用 `fix_agent.build_fix_prompt` 的语义（根因 + RAG 引用 + 目标文件 + 最小化约束 + 重试强化提示），针对 Qoder 的**自主能力**做两点调整：

1. **弱化「内联目标文件全文」**：Qoder 能自己读文件，prompt 改为给出**目标文件路径**与定位线索，避免污染上下文；
2. **强化「只改这一处、只动这个文件」约束**：明确禁止新增文件、禁止改动测试、禁止重构无关代码；
3. **显式要求自检**：要求 Qoder 修改后自行运行仓库既有单测（若工作区允许），确认目标用例转绿——但**最终判定仍以沙箱为准**。

输出约束（与既有管线对齐）：只需产出**代码改动**，无需输出 diff 文本——diff 由 `git diff` 确定性生成，从根上规避小模型「手写 diff 行号错乱 / 上下文改写」的经典失败模式。

---

## 8 降级链与错误处理

沿用既有「观测/推理面故障一律降级并放大不确定性」的原则：

| 故障点 | 降级行为 | 最终效果 |
| --- | --- | --- |
| `qoder` 未安装 / 不在 PATH | 抛 `QoderUnavailable` | 兜底补丁（degraded）→ 沙箱大概率拦截 → 回炉/转人工 |
| 鉴权失败（无 PAT / PAT 失效） | 非零退出或空 diff | 同上 |
| 子进程超时 | kill + 抛错 | 同上 |
| Qoder 未产生改动（空 diff） | 抛错 | 同上 |
| diff 无法应用 / 编译失败 | 抛错 | 同上 |
| 演示分支关键词命中 | 保持既有短路（**先于**提供者分发） | 演示矩阵不受 provider 影响 |

> 安全原则：Qoder 任何异常都**不会**导致「带病放行」。最坏情况是回落为确定性兜底补丁，由真实沙箱测试拦截。

---

## 9 与 Ollama 链路的共存

| 维度 | `ollama`（默认） | `qoder`（新增） |
| --- | --- | --- |
| 生成方式 | 单次 LLM 调用，prompt 内联目标文件 | 多轮自主探索，工作区内真实编辑 |
| 变更来源 | LLM 直接输出 diff 文本 | `git diff`（确定性） |
| 仓库理解 | 仅目标文件 + RAG top-5 片段 | 全仓库自主检索 |
| 依赖 | 本地 Ollama 服务 | Qoder CLI + PAT（需联网） |
| 适合场景 | 离线/内网/演示、单点小修复 | 复杂缺陷、需跨文件理解、追求正确率 |
| 切换 | `AIOPS_FIX_PROVIDER=ollama` | `AIOPS_FIX_PROVIDER=qoder` |

两者产出的 `Patch` 结构完全一致，可**按告警/环境灰度**：生产用 `qoder`，演示与离线回归用 `ollama`。

---

## 10 配置项清单

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `AIOPS_FIX_PROVIDER` | `ollama` | 修复提供者：`ollama` / `qoder` |
| `AIOPS_QODER_BIN` | `qoder` | Qoder CLI 可执行文件路径（便于多版本/自定义安装） |
| `AIOPS_QODER_MODEL` | **`DeepSeek-Flash`** | 传给 `-m` 的修复模型。**ID 区分大小写**，写错会静默回退 `auto`（见 4.7） |
| `AIOPS_QODER_TIMEOUT` | `180` | 子进程超时（秒）。Qoder 多轮探索，需大于纯 LLM 的 45s |
| `AIOPS_QODER_MAX_TURNS` | `20` | 传给 `--max-turns` |
| `AIOPS_QODER_PERMISSION_MODE` | `accept_edits` | 权限模式（安全起见默认不接受 bypass） |
| `AIOPS_QODER_ALLOWED_TOOLS` | `Read,Edit,Write,Grep,Glob` | 工具白名单（可覆盖以适配不同 CLI 版本） |
| `AIOPS_QODER_ROOT` | `data/qoder` | 隔离工作区根目录 |
| `AIOPS_QODER_TOKEN` | 空 | 显式注入的 PAT；为空时沿用进程环境 `QODER_PERSONAL_ACCESS_TOKEN` |

---

## 11 测试策略

**核心原则：单测不依赖真实 Qoder CLI**（未安装也能全绿）。

- `tests/fakes/fake_qodercli.sh`：可编程桩脚本，由环境变量控制行为（正常产出改动 / 无改动 / 非零退出 / 超时 / 输出噪声），模拟 `qoder` 的参数位；
- 通过 `AIOPS_QODER_BIN` 指向桩脚本，覆盖：

| 用例 | 断言 |
| --- | --- |
| CLI 正常产出改动 | `validated=True`、`provider=qoder`、diff 可应用且编译通过、model_version 前缀 `qoder-cli:` |
| CLI 缺失 | degraded 兜底、流程不抛异常 |
| 非零退出 | degraded 兜底 |
| 空 diff（未改动） | degraded 兜底 |
| 超时 | 子进程被终止、degraded 兜底 |
| 非法改动（语法错误） | 编译校验拦截、degraded 兜底 |
| 环境变量清洗 | 剔除 `QODER_AGENT_SDK_ENTRYPOINT` 等继承变量；保留鉴权/配置变量 |
| 活动超时不变量 | `_FIX_ACTIVITY_TIMEOUT`(300s) > Qoder 子进程超时(180s) |
| 默认模型 | 未设 `AIOPS_QODER_MODEL` 时 `-m` 传 `DeepSeek-Flash`；环境变量可覆盖，显式参数优先 |
| 静默回退告警 | 无效模型名 → 捕获 stderr 警告写入 `meta.model_warning` / `reason` / 运行留痕 |
| 提供者默认值 | 未设 `AIOPS_FIX_PROVIDER` 时走 Ollama，行为零变更 |
| 演示分支 + qoder | 演示短路优先，仍走确定性补丁 |

- 回归：既有 **135 个用例必须全绿**（`AIOPS_FIX_PROVIDER` 默认值保证零回归）。

---

## 12 落地步骤与验收

### 12.1 上线前置

1. 安装 Qoder CLI（已实测 v1.1.65，安装脚本官方、SHA256 校验、走阿里云 OSS）：

```bash
curl -fsSL https://qoder.com/install | bash
qodercli --version            # 安装到 ~/.local/bin/qodercli（命令名为 qodercli）
```

2. 鉴权（两种方式，任选其一）：

```bash
# 方式 A（推荐，无人值守）：PAT 环境变量
export QODER_PERSONAL_ACCESS_TOKEN="<PAT>"   # https://qoder.com/account/integrations

# 方式 B（本机交互一次，之后复用登录态）：设备码流程，无头环境亦可
qodercli login      # 打印一个 qoder.com/device/... URL，浏览器授权后 CLI 即已登录
```

3. 配置：`AIOPS_FIX_PROVIDER=qoder`（并确保 `AIOPS_FIX_PROVIDER` 在 worker 进程生效）。

4. 指定 CLI 路径（可选）：`AIOPS_QODER_BIN=/Users/<you>/.local/bin/qodercli`。

### 12.2 验收标准

| # | 验收项 | 判定 |
| --- | --- | --- |
| 1 | 既有 135 用例零回归 | 全绿（合计 155 用例） |
| 2 | 新增 Qoder 接入层 28 用例全绿 | 全绿（不依赖真实 CLI） |
| 2b | 发布测试容器隔离 | 真实发布容器在连续 3 次 + 全量单测后 ID/健康态不变 |
| 3 | 默认 provider 不改变现网行为 | `AIOPS_FIX_PROVIDER` 未设时链路与现状一致 |
| 4 | Qoder 正常路径端到端 | Patch 进沙箱 → 审批 → 公告 → 金丝雀 → 全量 |
| 5 | 降级路径端到端 | 关闭/卸载 Qoder 时，流程仍能走到沙箱并被正确拦截 |
| 6 | 隔离性 | Qoder 运行期间真实 `demo-app/` 未被改动（工作区在 `data/qoder/`） |
| 7 | 可审计 | `data/qoder/<patch_id>.run.json` 含命令/耗时/diff 摘要 |

### 12.3 回滚

一行配置即可切回：`AIOPS_FIX_PROVIDER=ollama`（或删除该变量）。

### 12.4 实测证据（2026-10-03）

环境：macOS(aarch64) + **真实 Qoder CLI v1.1.65** + colima(Docker 29.2.1) + Python 3.12。

**A. 真实 Qoder CLI 端到端（非桩）**

| 项 | 实测值 |
| --- | --- |
| 链路 | 隔离工作区 → Qoder 自主读代码改代码 → `git diff` → 应用/编译校验 → **真实 Docker 沙箱** |
| `provider` / `model_version` | `qoder` / `qoder-cli:1.1.65` |
| `validated` / `degraded` | `True` / `False` |
| CLI 耗时 | 50.5s（`exit=0`，未超时） |
| 产出 diff | `-        discount = coupon["discount"]` → `+        discount = coupon["discount"] if coupon else 0`（正确的最小单点修复） |
| 沙箱 | **6/6 用例通过**（`python:3.12-slim` 隔离容器，SAST 无命中） |
| 隔离性 | 真实 `demo-app/order_service.py` 逐字节未变 |
| Qoder 自述 | 「仅改动 `order_service.py` 一处，未新增文件、未改测试」——与 prompt 约束一致 |
| 真实成本 | 0.752 credits |

**B. 全量单测与故障注入（桩驱动，可离线复现）**

| # | 验证项 | 结果 |
| --- | --- | --- |
| 1 | 全量单测 | **166 passed**（既有 135 + 新增 31），零回归 |
| 2 | 降级路径 | CLI 缺失 / 非零退出 / 空 diff / 超时 / 语法错误，5 类故障均安全回落兜底补丁 |
| 3 | 环境清洗 | `QODER_AGENT_SDK_ENTRYPOINT` 等继承变量被剔除（回归防护单测） |
| 4 | 活动超时不变量 | `_FIX_ACTIVITY_TIMEOUT` > `AIOPS_QODER_TIMEOUT`（回归防护单测） |
| 5 | 默认零变更 | 未设 `AIOPS_FIX_PROVIDER` 时走 Ollama，既有行为不变 |

**C. 完整发布链路（全套演示栈，真实 Qoder 提供者）**

用真实 Qoder 提供者跑通「告警 → 根因 → 修复 → 沙箱 → 审批 → 公告 → 金丝雀 → 全量发布」全链路：

| 环节 | 实测结果 |
| --- | --- |
| 日志取证 / 聚类 | Loki 1720 行 → Drain3 7 个模板（压缩比 0.0041） |
| 根因（真实 Ollama qwen3:8b） | `NullPointerException`、`confidence=0.95`、2.0s、未降级（过闸门 1） |
| 目标定位 | 真实 triage 给出 Java 路径 `order-service/src/main/java/…/OrderService.java`，模糊定位正确映射到 `order_service.py` |
| 修复（**真实 Qoder**） | `provider=qoder`、`model=qoder-cli:1.1.65`、`validated=True`、`degraded=False` |
| 产出 diff | `discount = coupon["discount"]` → `discount = coupon["discount"] if coupon is not None else 0` |
| 沙箱 | PASS，6/6 用例（`python:3.12-slim` 隔离容器） |
| MR | `!2478`，标签 `ai-fix:qoder-full-2` / `model:qoder-cli:1.1.65` / `confidence:0.95` |
| 公告 | `v1.0.678`，推送 1327 会话 |
| 金丝雀 | 20 次真实请求：错误率 0.00%、P99 6ms → 健康 |
| 全量发布 | 稳定容器 `aiops-stable-order` 实际运行于 `:18080`，镜像 `aiops-demo-app:p-qoder-full-2-r0`；`/health` 返回 `version=v1.0.678`（与公告一致） |
| 终态审计 | `stage=DONE`、`duration=275.5s`、`rolled_back=false`；gate_events 全齐 |
| 隔离性 | 全流程后真实 `demo-app/order_service.py:34` 仍是**原始缺陷行**，零改动 |

> gate_events 全链：`tests:passed:attempt=0` → `mr:created:!2478` → `gate2:approved` → `gate3:announced:v1.0.678` → `gate3:command:deploy_now` → `canary:healthy->full-rollout`

**D. 环境注意**

1. 沙箱工作区须位于**家目录**下（colima 默认仅挂载 `$HOME`；`/tmp` 对容器不可见，会导致工作区为空、报「Start directory is not importable」）。
2. 安装后命令名为 **`qodercli`**（`~/.local/bin/qodercli`），旧文档中的 `qoder` 是 IDE/CLI 分发器，未必在 PATH 上——生产建议显式设置 `AIOPS_QODER_BIN`。
3. 若 worker 与 Qoder 同进程树，务必保留 4.5 的环境清洗逻辑。
4. `tests/test_release.py` 会操作**真实 Docker**（真实发布集成），但已完成**容器隔离**：测试使用 `aiops-test-stable:18099`，不再触碰真实发布的 `aiops-stable-order:18080`（`DockerRolloutRunner` 支持注入稳定容器名/端口，生产默认值不变；测试另有 `addCleanup` 兜底回收）。实测：连续 3 次 + 全量单测后，真实发布容器 ID 与 `/health` 均不变。

---

## 13 已知边界与演进

| 项 | 现状 | 演进方向 |
| --- | --- | --- |
| Qoder 联网依赖 | 需 PAT 且联网调用 | 支持内网/私有化部署的 Qoder 端点（若提供） |
| JSON 输出结构 | 未公开，故不使用 | 若官方公开 schema，可解析以获取 token/费用元数据 |
| 单文件约束 | 仅取目标文件 diff | 支持多文件最小修复（需放宽 `git diff` 路径过滤 + 审批面展示） |
| Qoder 自检测试 | prompt 建议、非强制 | 在隔离工作区用 `--allowed-tools` 放行受限测试命令，做「提交前自检」 |
| 成本 | `--max-turns` 限流 | 增加按服务/按告警的预算与配额控制 |

---

## 附录 A Qoder CLI 关键命令速查

```bash
# 无头单次执行
qoder -p "修复 order_service.py 中优惠券为空导致的 TypeError"

# 完整参数（本接入层使用形态）
qoder -p "<prompt>" \
      -o json \
      --permission-mode accept_edits \
      -w <workspace> \
      --max-turns 20 \
      --no-session-persistence \
      --allowed-tools "Read,Edit,Write,Grep,Glob" \
      --disallowed-tools "Bash" \
      -m auto

# 鉴权
export QODER_PERSONAL_ACCESS_TOKEN="<PAT>"
qoder --version
```

## 附录 B 与既有文档的对应关系

| 既有概念 | 本设计中的位置 |
| --- | --- |
| 决策层「修复 Agent」（设计说明书 4.3.2） | 新增 `qoder` 提供者，与 `ollama` 提供者并列 |
| 安全侧降级链（6.1） | 新增 `qoder` 不可用/超时/空 diff 三条降级分支，终点一致 |
| LLM diff 应用管线容错（6.2） | 完全复用；且 Qoder 路径以 `git diff` 取变更，天然规避该失败模式 |
| 沙箱验证（4.4）/ 三道闸门（2.4） | **不改动**；Qoder 产出的补丁同样经受这些关卡 |
