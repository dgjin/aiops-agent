# AIOps 控制台鉴权与体验升级实施方案

> 版本：v1.0（2026-10-05 定稿）
> 决策记录：用户名+密码登录 / 双主题+强调色可选 / 后端先行（前端批量交付）/ 方案落盘
> 关联文档：《AIOps 自动运维智能体系统用户操作手册》《AIOps 自动运维智能体系统设计方案》《控制台易用性评估报告-2026-10-03》

---

## 1. 背景与目标

控制台此前已完成 Token 级鉴权（三角色 RBAC + 自动轮换），但缺少"人"的维度与体验层：
无登录页、无用户管理、前端不感知角色；UI 层面侧边栏不可收缩、无品牌 Logo、单主题、无帮助体系。

本方案目标（两条主线）：

1. **身份治理主线**：升级为完整的用户/会话/密码体系——登录、登出、用户管理、按钮级权限可见性；
   现有静态令牌保留为自动化/应急通道（渐进增强，不推翻已运行的轮换机制）。
2. **体验升级主线**：可收缩侧边栏 + 品牌 Logo + 深/浅双主题（含强调色切换）+ 帮助中心 + 细节美化。

---

## 2. 现状盘点

### 2.1 已具备（本方案的地基，避免重复建设）

| 能力 | 现状 | 位置 |
|---|---|---|
| 三角色模型 | viewer(1) / operator(2) / admin(3) 等级递进 | `bff/auth.py` |
| 路由级角色守卫 | 集中式中间件：读=viewer 起步；写按规则表；未知写接口安全默认 admin | `bff/middleware.py` |
| 令牌体系 | Bearer Token + 30 天自动轮换 + 7 天宽限 + 双后端（文件 600 / MySQL 指纹） | `bff/auth.py` |
| 安全配套 | 读/写/认证失败分限速、kill switch 拦截、安全头、fail-closed | `bff/middleware.py` |
| 审计身份 | 写操作 actor 为真实用户 | `bff/routes/*.py` |

### 2.2 缺口（本方案的落点）

1. 无登录页/密码/登出；用户=令牌，发令牌靠人工传递注册表；前端左下角手工粘贴 Bearer token 是唯一入口
2. 无用户管理 UI/接口（增删用户、改角色、禁用、重置密码、强制下线）
3. 前端不感知角色：除"立即轮换"外无按钮级权限控制（viewer 看到审批按钮，点击才 403）
4. 侧边栏固定 224px 不可收缩；无 logo（临时圆形 favicon）；单主题无切换；无帮助体系

---

## 3. 评估方案更新

结合《控制台易用性评估报告》结论，本批是两条评估短板的"正面回应"：

- **易用性 B → A 路径**：登录体系、权限可视、帮助中心、主题切换是体验线主体工程
- **安全身份治理**：将"令牌收敛到 secret 层"建议升级为完整用户/会话/密码体系

**整合路线图**（本批与既有 P0 建议互补、不重叠）：

```
本批（先行，用户已确认）              上轮 P0 建议（紧随）
┌───────────────────────────┐      ┌───────────────────────────┐
│ 控制台身份治理：登录/用户/角色/会话 │      │ 告警入口鉴权（webhook 防伪造）  │
│ UI 体验包：主题/收缩/Logo/帮助    │      │ Meta-Ops watchdog          │
└───────────────────────────┘      │ CI 流水线                    │
   ※ 管"谁在操作控制台"               └───────────────────────────┘
                                       ※ 管"谁在触发修复链路"
```

---

## 4. 需求一：用户鉴权与角色权限

### 4.1 总体设计（三层，渐进增强）

```
┌─ 体验层（新增）：登录页 / 登出 / 用户菜单 / 用户管理页
├─ 会话层（新增）：用户名+密码登录 → 服务端会话令牌（可强制下线、可审计在线）
└─ 凭证层（保留）：静态令牌继续有效（自动化/应急），校验链为：
     会话令牌 → 静态令牌 两级匹配（production-smoke 与场景脚本零改动）
```

### 4.2 存储设计（双后端，对齐现有模式）

| 存储 | demo 模式 | production 模式 |
|---|---|---|
| 用户 | `data/console_users.json`（600 权限，原子写） | MySQL 新表 `console_users` |
| 密码 | `pbkdf2_sha256$210000$salt$hash`（hashlib stdlib，零新依赖） | 同左 |
| 会话 | `data/console_sessions.json`（仅存 sha256 指纹） | MySQL 新表 `console_sessions` |

- 用户字段：`username / password_hash / role / state(active|disabled) / created_at / updated_at`
- 会话字段：`token_hash / username / role / created_at / expires_at / last_seen_at / revoked`
- 首启播种管理员：`admin / admin123`（`AIOPS_BOOTSTRAP_ADMIN_PASSWORD` 可配），并强制首登修改提示
- 会话 TTL：默认 12 小时（`AIOPS_SESSION_TTL_SECONDS` 可配），滑动续期

### 4.3 接口设计（新增 `bff/routes/users.py` + `bff/auth.py` 扩展）

| 接口 | 方法 | 权限 | 说明 |
|---|---|---|---|
| /api/auth/login | POST | 公开（限速） | 用户名+密码 → 会话令牌+身份；失败计认证失败限速 |
| /api/auth/logout | POST | 登录用户 | 吊销当前会话 |
| /api/auth/status | GET | 已认证 | 扩展返回 `self`（user/role/source/expires_at） |
| /api/users | GET/POST | admin | 用户列表 / 新建（初始密码+角色） |
| /api/users/{u} | PUT/DELETE | admin | 改角色 / 删除 |
| /api/users/{u}/password | POST | admin 重置；本人改密 | 密码管理 |
| /api/users/{u}/sessions | GET/DELETE | admin | 在线会话查看 / 强制下线 |

所有登录/登出/用户管理操作写审计（复用现有 audit 通道）。

### 4.4 权限矩阵（角色 × 功能 × 前端表现）

| 功能点 | 接口 | viewer | operator | admin | 前端表现（PermissionGate） |
|---|---|---|---|---|---|
| 全部读页面 | GET /api/* | ✓ | ✓ | ✓ | 可见 |
| 一级/二级审批 | POST flows/*/approval | - | ✓ | ✓ | 隐藏按钮+悬浮"需要 operator" |
| 发布指令/补丁排队 | deploy-command / queue-patch | - | ✓ | ✓ | 同上 |
| 被监控应用 CRUD | monitored-apps 写侧 | - | - | ✓ | 隐藏入口 |
| kill switch | POST system/kill-switch | - | - | ✓ | 仅 admin |
| 令牌轮换 | POST auth/rotate | - | - | ✓ | 仅 admin |
| 用户管理页 | /api/users/* | - | - | ✓ | 侧栏入口仅 admin；URL 越权拦截 |

后端以现有 `_WRITE_ROLE_RULES` 逐条复核补齐；前端权限规则与后端保持同源单一常量表（防漂移）。

### 4.5 前端鉴权改造

| 文件 | 改动 |
|---|---|
| `lib/auth.tsx`（新） | AuthContext：identity 拉取、login/logout、全局 401 → 清 token → 跳登录页 |
| `pages/Login.tsx`（新） | 居中卡片：Logo + 用户名/密码 + 错误提示 + "令牌登录"折叠入口（兼容模式） |
| `App.tsx` | 路由守卫（未认证 → /login）+ 顶栏用户菜单（用户名/角色徽章/登出） |
| `lib/api.ts` | 登录成功复用 `setToken()`（localStorage aiops.token），零破坏 |

---

## 5. 需求二：UI 体验包

### 5.1 主题系统（深/浅双主题 + 强调色三选）

Tailwind v4 两层 token 改造，实现零闪烁切换：

```css
/* 原始层（可切换） */
:root { --c-canvas:#0b0f14; ... }
[data-theme="light"] { --c-canvas:#f6f8fa; ... }
/* 语义层（@theme inline 直连变量） */
@theme inline { --color-canvas: var(--c-canvas); ... }
```

浅色主题色板（与深色同气质的高级浅色）：

| Token | 深色（现有） | 浅色（新增） |
|---|---|---|
| canvas / panel | #0b0f14 / #10161d | #f6f8fa / #ffffff |
| elevated / line | #172029 / #1f2933 | #eef2f6 / #d8e0e8 |
| ink / muted | #e6edf3 / #8899a6 | #111827 / #5b6b7c |
| ok / warn / danger / info / idle | #34d399 / #fbbf24 / #f87171 / #60a5fa / #64748b | #059669 / #d97706 / #dc2626 / #2563eb / #94a3b8 |

**强调色三选**（`data-accent` 属性切换，深浅主题各一套值）：

| 强调色 | 深色 | 浅色 |
|---|---|---|
| teal（默认） | #2dd4bf | #0d9488 |
| blue | #38bdf8 | #0284c7 |
| violet | #a78bfa | #7c3aed |

- 切换机制：`lib/theme.ts`（主题：深色/浅色/跟随系统；强调色：teal/blue/violet）+ localStorage 持久化
- `index.html` 首屏防闪烁脚本；图表（状态机/管道/环形图）全部走 token 类，自动适配
- 切换器放置：顶栏 + 用户菜单双入口

### 5.2 侧边栏收缩 + 顶栏重组

- 侧栏两态：展开 224px / 折叠 64px（icon-only + hover tooltip + 徽章收缩为圆点），localStorage 记忆 + 200ms 过渡
- 新增顶栏：当前页面名 / 帮助(?) / 主题切换 / 用户菜单（含登出与"访问令牌"兼容入口）
- 底部 ServerClock/ConnectionDot 保留；TokenControl 迁入用户菜单（兼容通道）

### 5.3 Logo 设计（三概念，实施时评审定稿）

- **概念 A（推荐）· 盾 + 脉冲**：圆角盾形轮廓（teal→emerald 渐变描边），中央白色心电脉冲折线，盾底呼吸点——"安全闸门 + 监控守望"
- 概念 B · 六边形节点网 + 中心脉冲（智能体取向）
- 概念 C · 字母 A 变形为上升脉冲（字标取向）
- 交付：`Logo.tsx`（SVG 三尺寸）+ 登录页大尺寸 + 侧栏 24px + favicon 替换

### 5.4 帮助功能

- **帮助中心页**：快速上手三步、流程状态机图例、三道闸门术语、角色权限表、FAQ（含"转人工后怎么办"等高频问题）、场景剧本索引、文档链接
- **页面级帮助抽屉**：顶栏 ? → 右滑抽屉，内容随当前页（功能说明 + 术语 + 常见操作）
- 内容与《用户操作手册》对齐；不做全站 tour（避免过度工程）

### 5.5 美化细节清单

过渡动画统一（页面/hover/展开）、focus-visible 规范、滚动条美化、空态插画化、横幅（降级/kill switch/令牌）双主题适配、列表行 hover 微交互。

---

## 6. 实施计划

### 6.1 交付批次（已确认：后端先行，前端批量交付）

| 批次 | 内容 | 交付物 | 验收 |
|---|---|---|---|
| **批次 1（后端）** | 用户/会话/密码体系 + 用户管理接口 + 中间件接入 + 单测 | 接口可用、全量测试无回归、curl 实测链路 | 登录→调用→登出→401；权限矩阵 403 实测 |
| **批次 2（前端）** | 登录闭环、主题系统、布局+Logo、权限 UI+用户管理页、帮助中心、美化 | 全量前端交付 | 三角色按钮矩阵实测；双主题截图；收缩态截图；构建通过 |
| **收尾** | 回归 + 冒烟 + 提交推送 | 提交记录 | 全量 unittest + e2e 冒烟 |

### 6.2 阶段明细

| 阶段 | 归属批次 | 内容 | 工期 |
|---|---|---|---|
| 1 | 批次 1 | 后端用户与会话（auth.py 扩展、users.py、db.py 建表、单测、curl 实测） | 1.5d |
| 2 | 批次 2 | 前端登录闭环（auth.tsx、Login.tsx、路由守卫、用户菜单） | 1d |
| 3 | 批次 2 | 主题系统（styles.css 重构、theme.ts、防闪脚本、强调色、切换器） | 1d |
| 4 | 批次 2 | 布局+Logo（Logo.tsx、可收缩侧栏、顶栏、favicon） | 1d |
| 5 | 批次 2 | 权限 UI+用户管理（permission.ts、PermissionGate 全站接入、Users.tsx） | 1d |
| 6 | 批次 2 | 帮助中心（Help.tsx、HelpDrawer、内容编写） | 1d |
| 7 | 批次 2 | 美化+回归（细节打磨、双主题截图、全量回归、提交推送） | 1d |

合计约 7.5 人日。阶段 3/4 可并行压缩。

### 6.3 文件级改动清单

**批次 1（后端）**：
- `bff/auth.py`：用户存储（users 读写双后端）、pbkdf2 密码 hash、会话签发/校验/吊销、登录逻辑
- `bff/routes/users.py`（新）：login/logout/users CRUD/password/sessions
- `bff/middleware.py`：校验链加会话层（会话→静态令牌）；`_WRITE_ROLE_RULES` 补 `/api/users` → admin
- `aiops_agent/db.py`：console_users / console_sessions 表定义
- `bff/routes/tokens.py`：/api/auth/status 扩展 self 身份字段
- `tests/test_auth_users.py`（新）：登录/会话/权限/吊销/审计单测

**批次 2（前端）**：
- 新增：`lib/auth.tsx`、`lib/theme.ts`、`lib/permission.ts`、`components/Logo.tsx`、`components/ThemeToggle.tsx`、`components/PermissionGate.tsx`、`components/HelpDrawer.tsx`、`pages/Login.tsx`、`pages/Users.tsx`、`pages/Help.tsx`
- 修改：`App.tsx`（守卫+布局重构）、`styles.css`（主题重构）、`index.html`（防闪+favicon）、`lib/api.ts`（登录/用户 API + 401 处理）、各页面按钮权限接入

---

## 7. 风险与兼容性

| 风险 | 对策 |
|---|---|
| 旧令牌被破坏（自动化依赖） | 会话与静态令牌并存，校验链兼容；production-smoke/场景脚本零改动 |
| 首启无管理员可用 | env 播种 admin + 强制首登改密；保留 fail-closed 语义 |
| 浅色主题对比度不足 | 全组件走 token（现状已达标）+ 阶段 3 专项对比度检查 |
| Tailwind v4 @theme inline 行为差异 | 阶段 3 先验证构建产物（两主题各构建一次实测） |
| 密码/会话安全 | pbkdf2 210k+盐；会话仅存指纹；限速复用；审计全留痕 |
| dist 入库策略 | 沿用现状：构建产物随源码提交 |

---

## 8. 验收标准

1. 未登录：所有 /api 返回 401，前端展示登录页
2. viewer：零写操作入口（按钮隐藏），直接调用写 API 得 403
3. operator：无用户管理/应用管理/kill switch 入口
4. admin：用户管理全流程可用（创建→分配角色→重置密码→强制下线）
5. 兼容通道：`tok-ops` 等静态令牌行为与升级前完全一致
6. 全量 unittest 通过；前端构建通过；深浅双主题与收缩态截图核验

---

## 9. 决策记录

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 登录方式 | 用户名+密码（会话 12h），静态令牌保留为自动化/应急通道 |
| 2 | 主题范围 | 深/浅双主题 + 强调色三选（teal/blue/violet） |
| 3 | 实施节奏 | 后端先行交付（批次 1），前端批量交付（批次 2） |
| 4 | 方案落盘 | 本文件 |
