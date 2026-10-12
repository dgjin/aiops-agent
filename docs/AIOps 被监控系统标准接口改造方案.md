# AIOps 被监控系统标准接口改造方案

> 目标：被监控系统**一次改造**（部署标准接口），此后在 AIOps 控制台「被监控应用」新增/配置即完成接入——探测巡检、日志采集、突增检测、修复仓库解析、代码索引、修复后验证**全链路自动适配**，无需再人工改 AIOps 代码、手工建索引或导出环境变量。
>
> 适用版本：AIOps 自动运维智能体（本仓库 `aiops-agent/`），标准接口规范 **AIOps Manifest v1.0**。
>
> 可选扩展：**需求基线条目接口**（Manifest 字段 `requirements_path`）——被监控系统的「需求收集与反馈」经管理员评估纳入基线后，供 AIOps 做主动需求分析（见第七章）。
>
> 修订（2026-10-12）：页面判据升级为**通用白屏判据兜底**——`probe_keyword` 改为可选（留空自动采用通用白屏检测，配置则精确匹配并兼作修复后金丝雀验证契约）。

---

## 一、标准接口定义（AIOps Manifest v1.0）

### 1.1 端点约定

| 项 | 约定 |
| --- | --- |
| 路径 | `GET /.well-known/aiops.json`（应用根地址下，如 `http://app.internal:8000/.well-known/aiops.json`） |
| 内容类型 | `application/json; charset=utf-8`（UTF-8 JSON 对象） |
| 认证 | 匿名可读（探测方不携带凭证；如需内网限制请用网络策略，不要用登录墙） |
| 超时/大小 | 探测方默认 3s 超时、上限 256KB；响应应轻量（建议 < 2KB） |
| 失败语义 | 响应缺失/非 JSON/必填缺失 = 「未接入标准接口」——控制台按手工填写处理，不影响应用运行 |

### 1.2 字段规范

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `spec_version` | **是** | string | 接口规范版本，当前 `"1.0"`；主版本不符仅告警、尽力兼容 |
| `service` | **是** | string | 服务标识，**必须与告警/日志中的 service 一致**（如 `nl2sql`、`order`）。仅允许字母/数字/`.`/`_`/`-`/`:`，建议全小写 |
| `name` | 否 | string | 人类可读名称（控制台默认展示名） |
| `probe_keyword` | 否 | string | 健康判据关键字（**可选，推荐留空**）：配置时健康页响应体须包含该子串才算在线，并兼作修复后金丝雀验证契约；**未配置时自动采用通用白屏判据**（响应非空 + HTML 完整闭合 + body 含可渲染容器，不依赖具体挂载点名称）——两种模式均覆盖「端口活着但页面白屏」 |
| `health_path` | 否 | string | 健康检查路径（如 `/health`）。**控制台探测与可用性巡检按该路径发起页面判据校验**（`url + health_path`）；空 = 根路径 `/`（旧行为） |
| `metrics_path` | 否 | string | Prometheus 指标路径（观测增强，可空） |
| `log_path` | 否 | string | 应用日志文件路径，支持 `*` 通配（如 `/var/log/app/app_server*.log`）；采集器据此持续采集 |
| `protected_paths` | 否 | string[] | 受保护路径模式（如 `["auth/**", "payment/**"]`）：修复补丁命中 → 强制二级审批 |
| `test_command` | 否 | string | 沙箱回归建议命令（信息性；发布策略文件优先） |
| `requirements_path` | 否 | string | 需求基线条目导出路径（如 `/api/requirements/export`）：AIOps 据此拉取已纳入基线的需求做主动分析（见第七章） |

### 1.3 完整示例

```json
{
  "spec_version": "1.0",
  "service": "nl2sql",
  "name": "智能问数据分析系统",
  "probe_keyword": "<div id=\"root\"",
  "health_path": "/",
  "log_path": "/Users/dgjin/dgjinapp/智能问数据分析系统/logs/app_server*.log",
  "protected_paths": ["auth/**", "payment/**"],
  "requirements_path": "/api/requirements/export"
}
```

最小可用示例（只有必填字段）：

```json
{ "spec_version": "1.0", "service": "order" }
```

### 1.4 校验规则（探测方执行）

- `spec_version` 缺失/非字符串 → 无效；主版本与 `1.0` 不同 → 警告但按尽力兼容处理；
- `service` 缺失/空白 → 无效；
- `name`/`probe_keyword`/`health_path`/`metrics_path`/`log_path`/`test_command`/`requirements_path` 若出现必须为字符串；
- `protected_paths` 若出现必须为字符串数组；
- 校验实现在 [`aiops_agent/app_manifest.py`](../aiops_agent/app_manifest.py)，控制台探测与 CLI 校验共用同一实现。

---

## 二、被监控系统改造方案

### 2.1 改造原则

1. **只加不改**：仅在应用 HTTP 层新增一个只读端点（或部署一个静态文件），不侵入业务代码；
2. **两份数据、一个来源**：Manifest 里声明的 `service`/`log_path`/`probe_keyword`/`health_path` 应与你实际的日志落盘、健康页保持事实一致（建议从同一份配置生成，避免漂移）；
3. **失败无害**：端点异常/超时不影响应用自身运行（探测方所有失败均被捕获）。

### 2.2 各技术栈接入示例

**Python（标准库，完整参考实现见本仓库 [`demo-app/http_server.py`](../demo-app/http_server.py)）**

```python
MANIFEST = {
    "spec_version": "1.0",
    "service": "order",
    "name": "订单服务",
    "probe_keyword": '"status": "ok"',
    "health_path": "/health",
    "log_path": "/var/log/order/app_server*.log",
    "protected_paths": ["auth/**"],
}

def do_GET(self):
    if self.path == "/health":
        self._send(200, {"status": "ok"})
    elif self.path == "/.well-known/aiops.json":
        self._send(200, MANIFEST)
```

**Flask / FastAPI（路由挂载）**

```python
# Flask
@app.get("/.well-known/aiops.json")
def aiops_manifest():
    return MANIFEST            # dict 自动转 JSON

# FastAPI
@app.get("/.well-known/aiops.json")
def aiops_manifest():
    return MANIFEST
```

**Node / Express**

```javascript
app.get('/.well-known/aiops.json', (req, res) => res.json(manifest));
```

**Java / Spring Boot**

```java
@GetMapping("/.well-known/aiops.json")
public Map<String, Object> aiopsManifest() {
    return manifest; // 或 classpath 静态文件映射
}
```

**静态站点 / Nginx 等无后端场景（构建期生成静态文件）**

```bash
# 前端构建产物：<dist>/.well-known/aiops.json（由构建脚本生成，值来自 CI 变量/配置）
mkdir -p dist/.well-known
cat > dist/.well-known/aiops.json <<'EOF'
{ "spec_version": "1.0", "service": "web-portal", "name": "门户前端",
  "probe_keyword": "<div id=\"root\"", "health_path": "/" }
EOF
```

```nginx
# 或直接由 Nginx 提供（应用不改任何代码）
location = /.well-known/aiops.json {
    default_type application/json;
    alias /etc/aiops/web-portal-manifest.json;
}
```

### 2.3 本仓库 demo-app 参考实现（已改造）

`aiops-agent/demo-app/http_server.py` 已按 v1.0 部署端点，作为端到端验证的样板：

```python
MANIFEST = {
    "spec_version": "1.0",
    "service": "order",
    "name": "demo-app 订单服务",
    "probe_keyword": '"status": "ok"',
    "health_path": "/health",
    "protected_paths": ["auth/**"],
}
```

### 2.4 nl2sql（智能问数据分析系统）改造指引

1. 在其 HTTP 服务（如 `app_server`）新增 `GET /.well-known/aiops.json`，`service` 填 `nl2sql`（与 Loki 标签、告警 service 一致）；
2. `probe_keyword` 为**可选**：不填时探测/巡检/修复验证自动采用通用白屏判据（不依赖挂载点名称，当前清单中该项已留空）；如需锁定具体挂载点（如 `<div id="root"`）或健康页关键字再显式填写；有独立健康页则填 `health_path`（+ 可选该页关键字）；
3. `log_path` 填实际日志通配路径（当前为 `/Users/dgjin/dgjinapp/智能问数据分析系统/logs/app_server*.log`）；
4. 用校验 CLI 验证通过后，在控制台编辑该条目点「从标准接口探测」即可回填/对齐全部字段（此后路径变更只需改 Manifest 或控制台，无需改 AIOps 代码）；
5. （可选扩展）需求基线条目接口：本系统已部署 `GET /api/requirements/export` 并在 Manifest 以 `requirements_path` 声明；AIOps 用 `analyze_requirements.py` 拉取「已纳入基线」的需求做主动分析（见第七章）。

---

## 三、自动适配机制总览（前台配置 → 全链路热生效）

### 3.1 字段 → 消费链路对照

清单存储：demo 档 = `data/monitored_apps.json`；生产档 = MySQL `monitored_apps` 表（同库同表，BFF/worker 双端读取）。**所有读取均为运行时读取，无进程缓存。**

| 清单字段 | 消费链路（每轮/每次执行均重新读取 → 热生效） |
| --- | --- |
| `url` | ① BFF 实时探测（管理页状态列）② `app_prober.py` 可用性巡检 → 连续失败注入 Alertmanager → 自动修复 |
| `health_path` | ① 探测/巡检的请求路径（非空且非 `/` 时请求 `url + health_path`）② 页面判据（关键字/通用白屏）作用于该路径 |
| `probe_keyword` | ① 探测/巡检页面级校验：配置则精确匹配，**留空则通用白屏判据**（`page_health.py`；体检 CLI 与直连发布探针同一实现）② 已配置时作为修复契约 `app_registry.resolve().contract.keyword` → 修复后金丝雀/直连验证（未配置时探针走通用判据、不做关键字锁定） |
| `service` | ① Loki 标签（`ship_app_logs` 采集）② 突增检测（`log_surge_detector`）③ 告警 → 修复注册表匹配（`app_registry.resolve`）④ 专属索引命名 `code_index_<service>.json` |
| `log_path` | `ship_app_logs.py --follow` 每轮按 `log_targets()` 采集；突增检测同源 |
| `repo` | ① 代码索引**自动构建**（修复检索首次执行时，缺失/仓库变更即自动建，见 `_ensure_app_index`）② 修复工作区定位（补丁生成/沙箱验证的源码根） |
| `enabled` | 全部链路的总开关（停用即不再探测/采集/巡检） |

### 3.2 关键自动适配点（相较旧版人工步骤的变化）

| 旧人工步骤 | 现自动行为 |
| --- | --- |
| 手工 `index_codebase.py --repo ... --index ...` 建索引 | 控制台登记 `repo` 后，修复检索首次执行时**自动构建**专属索引；仓库变更自动重建（索引头部记录 repo，比对不一致即重建） |
| `export AIOPS_CODE_INDEX=...` 并重启 worker | 仅对**未注册** service 生效的兼容项；已注册应用一律用专属索引，环境变量不再劫持他仓检索 |
| 探测只探根地址、关键字对不上健康页 | Manifest 的 `health_path` 自动预填并被探测/巡检消费（关键字在健康页校验；未配置关键字时通用白屏判据自动兜底） |
| 翻代码找日志路径/关键字，人工填表 | 控制台「从标准接口探测」一键预填（名称/service/关键字/健康路径/日志路径） |
| 手工核对「仓库/索引/日志」是否就绪 | 管理页就绪度三态（绿/红/灰）+ 悬停说明，一眼定位缺哪项 |

### 3.3 索引解析优先级（重要）

```
修复检索目标 service
├─ 已注册（清单含该 service 且配置 repo、目录存在）
│   └─ 专属索引 data/code_index_<service>.json
│       ├─ 存在且头部 repo 匹配 → 直接复用（fast-path）
│       └─ 缺失 / repo 变更 → 单飞锁内自动构建（进程内并发只建一次）
│           └─ 构建失败 → 本次降级为空引用（绝不回退他仓索引，避免张冠李戴）
└─ 未注册 → 兼容回落：AIOPS_CODE_INDEX 环境变量 > 默认 data/code_index.json
```

---

## 四、接入流程（三步）

### 第 1 步：应用侧改造并自检

按第二章部署 `/.well-known/aiops.json` 后，用仓库自带校验 CLI 做「接入前体检」：

```bash
cd aiops-agent
.venv/bin/python check_aiops_interface.py --url http://localhost:8000            # core 三项
.venv/bin/python check_aiops_interface.py --url order.internal:8000 --extended   # +指标/日志检查
```

输出示例（demo-app 实测）：

```
== AIOps 标准接口校验 ==
目标：http://localhost:8123

[1/3] Manifest /.well-known/aiops.json  ✓ 通过（spec_version=1.0, service=order, name=demo-app 订单服务）
[2/3] 健康检查 /health  ✓ HTTP 200（58 字节）
[3/3] 关键字 '"status": "ok"'  ✓ 命中健康页响应
[ext] 指标路径  · 未配置，跳过
[ext] 日志路径  · 未配置（无 log_path 则日志采集 / 突增检测不可用）

结果：core 检查全部通过（3/3，扩展告警 1 项）。
```

退出码 `0` = core 全部通过（可接入）；`1` = 需先补齐（Manifest / 健康页 / 页面判据——关键字或通用白屏检测）。

### 第 2 步：控制台添加（自动预填）

「被监控应用 → 新增」→ 填写地址 → 点「**从标准接口探测**」→ 名称 / service / 健康关键字 / 健康路径 / 日志路径自动预填 → 按需补「修复仓库路径」（本地仓库目录，供代码检索与自动修复）→ 保存。

### 第 3 步：验证全链路生效

一键验证脚本（本仓库提供，真实 HTTP + 隔离清单，不触碰生产数据）：

```bash
cd demo-app && PORT=8123 ../.venv/bin/python http_server.py &   # 起一个样例被监控应用
cd .. && .venv/bin/python scripts/e2e_auto_adapt.py --port 8123
```

实测输出（全部通过）：

```
[1/6] 标准接口探测  ✓ spec_version=1.0 service=order（预填 ... health='/health'）
[2/6] 清单保存  ✓ id=demo-e2e（隔离文件，真实清单不受影响）
[3/6] 修复注册表热生效  ✓ repo=.../repo
[4/6] 可用性探测  ✓ HTTP 200 延迟 0.9ms
[5/6] 日志采集目标  ✓ demo-e2e 订单服务 → .../app.log
[6/6] 代码索引自动适配  ✓ 真实构建 code_index_demo-e2e.json（repo 匹配），检索返回 5 条
```

生产环境等价操作：保存清单后——
- 探测/巡检：下轮即用新地址/健康路径（管理页 15s 刷新、巡检默认 15s 间隔）；
- 日志采集：`ship_app_logs.py --follow` 下一轮即纳入新应用；突增检测同源；
- 修复与索引：下一次告警触发修复流程时，注册表即解析到新 repo，首次检索自动构建索引。

---

## 五、兼容与回退

| 场景 | 行为 |
| --- | --- |
| 应用未部署 Manifest | 控制台手动填写全部字段，行为与旧版一致（「未检测到标准接口，可手动填写」提示） |
| 清单无 `health_path`（旧数据） | 探测回落根地址 `/`（旧语义零变化） |
| 清单无 `probe_keyword`（含新数据留空） | 探测/巡检/直连发布验证自动采用通用白屏判据（响应非空 + HTML 完整闭合 + body 含可渲染容器），无需强制配置关键字 |
| 生产库未迁移 `health_path` 列 | 读侧容错为空串；写入侧使用新列。升级请执行：`ALTER TABLE monitored_apps ADD COLUMN health_path VARCHAR(512) NOT NULL DEFAULT '';` |
| 未注册 service 的历史部署 | `AIOPS_CODE_INDEX` 环境变量与默认索引的回落链保留，存量单应用部署不受影响 |
| 修复检索索引构建失败 | 本次修复降级为「无代码引用」（不阻断流程、不误用他仓索引），日志留痕 `[rag] 应用 xxx 专属索引构建失败` |

---

## 六、验收清单

- [ ] `GET <应用>/.well-known/aiops.json` 返回 200 且为合法 JSON，含 `spec_version` 与 `service`；
- [ ] `check_aiops_interface.py --url <应用>` 退出码 0（`--extended` 无关键告警）；
- [ ] 控制台「从标准接口探测」预填成功（提示「已从标准接口预填：…」）；
- [ ] 保存后管理页就绪度三态：仓库（已配 repo）/索引（首次检索后自动转绿）/日志 符合预期；
- [ ] `ship_app_logs.py --once` 日志采集包含新应用；`log_surge_detector.py --once` 运行正常；
- [ ] 触发一次告警（或执行 `scripts/e2e_auto_adapt.py`）：修复链路解析到对应 repo、自动构建/复用专属索引并检索到代码块。

---

## 七、需求基线条目接口（可选扩展，主动需求分析）

被监控系统内置「需求收集与反馈」闭环（用户提交 → 管理员评估分析 → 纳入基线管理）时，可再部署**需求基线条目导出接口**，让 AIOps 拉取已纳入基线的需求 / 建议做主动分析（优先级研判 / 实现建议 / 排期参考）。规范 v1.0：

### 7.1 端点与认证

| 项 | 约定 |
| --- | --- |
| 导出端点 | `GET /api/requirements/export`（Manifest 字段 `requirements_path` 声明） |
| 能力自描述 | `GET /.well-known/requirements.json`（匿名只读：`export_path` / `auth` / `query` / `status_flow` / `entry_fields`） |
| 认证 | `Authorization: Bearer <OPS_API_TOKEN>`（令牌由被监控系统侧配置；AIOps 侧经 `--token` 或环境变量 `NL2SQL_OPS_TOKEN` 注入） |
| 查询参数 | `status`（默认 `BASELINED`，可选 `PENDING` / `REJECTED` / `ALL`）、`kind`（`REQUIREMENT` / `SUGGESTION` / `BUG` / `OTHER`）、`since`（按 `updated_at >=` 增量，格式 `YYYY-MM-DD[ HH:MM[:SS]]`）、`limit`（1-500，默认 200） |
| 响应 | `{spec_version, service, system, exportedAt, filter, returned, entries[]}`；**无 `success` 字段**，以 HTTP 状态码为准；401=凭据问题，400=参数非法 |
| 响应上限 | 单条正文 ≤ 5000 字符；消费端读取上限 16MB |

### 7.2 AIOps 侧消费（本仓库已内置）

```bash
cd aiops-agent
# 拉取基线条目 → 本地分析（类型 / 优先级 / 版本分布、P0/P1 高优提示）→ Markdown 报告
.venv/bin/python analyze_requirements.py --url http://localhost:3000
# 机器可读 JSON（stdout）与增量拉取
.venv/bin/python analyze_requirements.py --url http://localhost:3000 --since 2026-10-01 --json
```

实现：`aiops_agent/requirements_client.py`（标准库零依赖，`fetch_requirements()` 永不抛异常）；报告给出下次增量游标（本次 `exportedAt`，`>=` 语义）。

### 7.3 验收清单

- [ ] `GET <应用>/.well-known/aiops.json` 含 `requirements_path` 且取值为导出端点路径；
- [ ] `GET <应用>/.well-known/requirements.json` 返回 200 且含 `export_path` / `auth`；
- [ ] `analyze_requirements.py --url <应用> --json` 退出码 0，`entries` 为已纳入基线条目；
- [ ] 无令牌 / 错误令牌时导出返回 401（`analyze_requirements.py` 退出码 1 且不产出报告）。

---

## 附录 A：升级 SQL（生产库）

```sql
-- 历史升级项（若已执行可忽略）
ALTER TABLE monitored_apps ADD COLUMN probe_keyword VARCHAR(256) NOT NULL DEFAULT '';
ALTER TABLE monitored_apps ADD COLUMN repo VARCHAR(1024) NOT NULL DEFAULT '';
-- 本次新增：健康检查路径（Manifest 自动预填）
ALTER TABLE monitored_apps ADD COLUMN health_path VARCHAR(512) NOT NULL DEFAULT '';
```

## 附录 B：文件索引（实现位置）

| 能力 | 文件 |
| --- | --- |
| Manifest 规范与探测 | `aiops-agent/aiops_agent/app_manifest.py` |
| 接入前体检 CLI | `aiops-agent/check_aiops_interface.py` |
| 清单存储（双后端、热生效） | `aiops-agent/bff/monitored_apps.py` |
| 控制台 API（探测/就绪度） | `aiops-agent/bff/routes/monitored_apps.py` |
| 可用性探测（含 health_path） | `aiops-agent/bff/aggregator.py` |
| 页面健康判据（通用白屏/关键字） | `aiops-agent/aiops_agent/page_health.py` |
| 可用性巡检 → 告警 | `aiops-agent/app_prober.py` |
| 修复注册表（service→repo/契约） | `aiops-agent/aiops_agent/app_registry.py` |
| 索引自动构建与检索路由 | `aiops-agent/aiops_agent/activities.py`、`aiops-agent/aiops_agent/code_rag.py` |
| 需求基线条目客户端 | `aiops-agent/aiops_agent/requirements_client.py` |
| 需求基线主动分析 CLI | `aiops-agent/analyze_requirements.py` |
| 参考实现（demo-app） | `aiops-agent/demo-app/http_server.py` |
| 端到端验证脚本 | `aiops-agent/scripts/e2e_auto_adapt.py` |
