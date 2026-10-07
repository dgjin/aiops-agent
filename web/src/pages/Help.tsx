/** 帮助中心：快速上手 / 角色权限 / 关键操作 / 令牌与登录 / 常见问题 / 快捷键 / 关于。
 *
 * 内容以当前服务端真实规则为准（权限矩阵与 middleware 的 _WRITE_ROLE_RULES /
 * _READ_ROLE_OVERRIDES 一致；快捷键与 AppShell 实际绑定一致）。
 */

import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLink, Keyboard, ShieldCheck } from 'lucide-react'
import { Logo } from '../components/Logo'
import { roleLabel } from '../components/UserMenu'

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-line bg-panel p-5">
      <h2 className="text-sm font-medium text-ink">{title}</h2>
      <div className="mt-3 space-y-2.5 text-sm leading-relaxed text-muted">{children}</div>
    </section>
  )
}

/** 权限矩阵单元格：允许 / 不允许。 */
function Mark({ ok }: { ok: boolean }) {
  return ok ? (
    <span className="font-medium text-ok">✓</span>
  ) : (
    <span className="text-idle">—</span>
  )
}

const PERMISSION_ROWS: { capability: string; viewer: boolean; operator: boolean; admin: boolean }[] =
  [
    { capability: '查看看板 / 流程 / 审计 / 系统状态', viewer: true, operator: true, admin: true },
    { capability: '审批 / 驳回（含二次审批）', viewer: false, operator: true, admin: true },
    { capability: '发布指令（立即发布 / 取消）与排队补丁', viewer: false, operator: true, admin: true },
    { capability: '被监控应用维护（增删改 / 导入导出）', viewer: false, operator: false, admin: true },
    { capability: '用户管理（创建 / 角色 / 重置密码 / 强制下线）', viewer: false, operator: false, admin: true },
    { capability: '令牌轮换 / kill switch / 令牌清单', viewer: false, operator: false, admin: true },
    { capability: '登出 / 本人修改密码', viewer: true, operator: true, admin: true },
  ]

export function Help() {
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-medium">帮助中心</h1>
          <p className="mt-1 text-xs text-muted">
            从处置流程到权限规则的使用说明；任意页面按 <Kbd>?</Kbd> 可快速回到本页
          </p>
        </div>
        <Logo size={34} className="shrink-0 text-ink" />
      </div>

      <Section title="快速上手：一次告警处置的标准路径">
        <ol className="list-decimal space-y-1.5 pl-5">
          <li>
            <Link to="/" className="text-accent underline-offset-2 hover:underline">
              流程看板
            </Link>
            ：俯视全局——进行中的修复、待审批数量与发布窗口状态一目了然。
          </li>
          <li>
            <Link to="/flows" className="text-accent underline-offset-2 hover:underline">
              流程列表
            </Link>
            → 进入流程详情：时间线呈现每一阶段（日志证据 → 根因分析 → 补丁生成 → 沙箱验证 → 灰度发布）。
          </li>
          <li>
            <Link to="/approvals" className="text-accent underline-offset-2 hover:underline">
              审批中心
            </Link>
            ：需要人工确认的流程在此批准 / 驳回；批准后进入发布窗口排队。
          </li>
          <li>
            <Link to="/window" className="text-accent underline-offset-2 hover:underline">
              发布窗口
            </Link>
            ：观察延迟发布倒计时；倒计时内可「立即发布」或「取消」，异常时系统自动回滚。
          </li>
          <li>
            <Link to="/audit" className="text-accent underline-offset-2 hover:underline">
              审计回看
            </Link>
            ：所有写操作（审批 / 发布 / 配置 / 用户管理）均可按操作者与时间回溯。
          </li>
        </ol>
      </Section>

      <Section title="角色与权限矩阵">
        <p>
          当前登录身份与角色见右上角用户菜单。角色由管理员分配；调整角色或禁用账号对现有会话
          <span className="text-ink"> 即时生效</span>（鉴权以用户表为准，不等会话过期）。
        </p>
        <div className="overflow-x-auto">
          <table className="mt-1 w-full text-xs">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="py-2 pr-4 font-medium">能力</th>
                <th className="w-16 py-2 pr-4 text-center font-medium">
                  {roleLabel('viewer')}
                  <div className="font-mono text-[10px] text-idle">viewer</div>
                </th>
                <th className="w-16 py-2 pr-4 text-center font-medium">
                  {roleLabel('operator')}
                  <div className="font-mono text-[10px] text-idle">operator</div>
                </th>
                <th className="w-16 py-2 text-center font-medium">
                  {roleLabel('admin')}
                  <div className="font-mono text-[10px] text-idle">admin</div>
                </th>
              </tr>
            </thead>
            <tbody>
              {PERMISSION_ROWS.map((row) => (
                <tr key={row.capability} className="border-b border-line/60 last:border-0">
                  <td className="py-2 pr-4 text-muted">{row.capability}</td>
                  <td className="py-2 pr-4 text-center">
                    <Mark ok={row.viewer} />
                  </td>
                  <td className="py-2 pr-4 text-center">
                    <Mark ok={row.operator} />
                  </td>
                  <td className="py-2 text-center">
                    <Mark ok={row.admin} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-[11px] text-idle">
          注：安全默认——未列出的写接口一律要求管理员；「用户管理」与「令牌清单」即使只读也仅管理员可访问。
        </p>
      </Section>

      <Section title="关键操作说明">
        <ul className="list-disc space-y-1.5 pl-5">
          <li>
            <span className="text-ink">审批</span>：在审批中心或流程详情中批准 / 驳回；部分高风险变更需要
            <span className="text-ink">二次审批</span>（第二位操作员确认）后才会进入发布窗口。
          </li>
          <li>
            <span className="text-ink">发布指令</span>：流程进入延迟发布窗口后，可在倒计时内「立即发布」或
            「取消」；超时未操作按策略自动发布，发布后异常由系统自动回滚。
          </li>
          <li>
            <span className="text-ink">排队补丁</span>：修复补丁排队期间可调整关联告警 / 服务 / 描述（操作记入审计）。
          </li>
          <li>
            <span className="text-ink">被监控应用</span>：改动写盘即热生效（无需重启）；15 秒内探测状态刷新，
            就绪度提示会标出缺失的仓库 / 索引 / 日志配置。
          </li>
          <li>
            <span className="text-ink">全局熔断（kill switch）</span>：管理员可在「系统状态」页激活，
            激活期间一切写操作被拒绝（横幅常驻提示操作者与原因）；登出、本人改密与关闭熔断本身仍有豁免。
          </li>
        </ul>
      </Section>

      <Section title="登录、会话与访问令牌">
        <ul className="list-disc space-y-1.5 pl-5">
          <li>
            <span className="text-ink">会话登录（推荐）</span>：用户名 + 密码登录；会话 12 小时内活跃自动续期，
            连续闲置超过 12 小时需重新登录。登出即吊销当前会话。
          </li>
          <li>
            <span className="text-ink">静态访问令牌（自动化 / 应急）</span>：长期有效，入口在
            <span className="text-ink">右上角用户菜单 →「访问令牌」</span>（或登录页折叠入口）；
            适合脚本 / CI 调用。它不会自动过期，安全依赖轮换。
          </li>
          <li>
            <span className="text-ink">令牌轮换</span>：管理员可在「访问令牌」弹窗中「立即轮换」；
            轮换后旧令牌在宽限期内仍有效，请尽快更新调用方与本地配置。
          </li>
          <li>
            <span className="text-ink">修改密码</span>：用户菜单 →「修改密码」（需原密码）；成功后
            <span className="text-danger">全部会话（含其他设备）被吊销</span>，需重新登录。
          </li>
          <li>
            <span className="text-ink">忘记密码</span>：请联系管理员在「用户管理」中重置（重置后同样强制下线）。
          </li>
        </ul>
      </Section>

      <Section title="常见问题（横幅与错误提示）">
        <ul className="space-y-2 pl-0">
          <li>
            <span className="font-mono text-xs text-danger">401</span>
            <span className="ml-2">
              登录已过期或凭证失效：点击横幅「重新登录」；自动化场景更新静态令牌。
            </span>
          </li>
          <li>
            <span className="font-mono text-xs text-danger">403</span>
            <span className="ml-2">当前角色权限不足：请管理员在「用户管理」中提升角色。</span>
          </li>
          <li>
            <span className="font-mono text-xs text-danger">429</span>
            <span className="ml-2">
              登录失败次数过多触发限速：等待响应头 Retry-After 指示的秒数后重试。
            </span>
          </li>
          <li>
            <span className="font-mono text-xs text-danger">503</span>
            <span className="ml-2">
              服务端未配置令牌注册表（fail-closed）：检查 AIOPS_CONSOLE_AUTH_TOKENS 或
              data/console_tokens.json 后重启 BFF。
            </span>
          </li>
          <li>
            <span className="text-warn">降级模式横幅</span>
            <span className="ml-2">
              修复链路依赖（日志 / 索引 / 沙箱）不可用：相关流程可能转人工或回落兜底补丁，按提示修复依赖后自动恢复。
            </span>
          </li>
        </ul>
      </Section>

      <Section title="界面与偏好">
        <ul className="list-disc space-y-1.5 pl-5">
          <li>
            <span className="text-ink">侧栏收起</span>：点击侧栏 logo 行右侧的「收起侧栏」按钮折叠为图标栏；
            收起后按钮移至 logo 下方，点击「展开侧栏」即可恢复。收起态悬停图标可查看页面名称。
          </li>
          <li>
            <span className="text-ink">主题 / 强调色</span>：在右上角「外观」菜单中切换（深色 / 浅色与强调色），
            两套主题均通过对比度检查（正文 ≥ AA）。
          </li>
          <li>以上偏好与侧栏状态保存在本机浏览器，刷新后保持，不随账号同步。</li>
        </ul>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="快捷键">
          <ul className="space-y-2">
            <li className="flex items-center gap-2">
              <Kbd>?</Kbd>
              <span>打开帮助中心（在输入框内输入时不触发）</span>
            </li>
            <li className="flex items-center gap-2">
              <Kbd>Esc</Kbd>
              <span>关闭下拉菜单与浮层（用户菜单 / 外观菜单 / 确认框）</span>
            </li>
          </ul>
          <p className="flex items-center gap-1.5 text-[11px] text-idle">
            <Keyboard size={12} /> 更多快捷键将随使用反馈逐步加入
          </p>
        </Section>

        <Section title="关于">
          <div className="flex items-start gap-3">
            <Logo size={30} className="mt-0.5 shrink-0 text-ink" />
            <div className="space-y-1.5">
              <div className="text-ink">AIOps 运维控制台 v1.0.0</div>
              <div className="text-xs">
                自动运维智能体：告警接入 → 异常研判 → 自动修复 → 人机审批 → 灰度发布与回滚，全链路留痕。
              </div>
              <div className="flex items-center gap-1.5 text-xs">
                <ShieldCheck size={12} className="text-ok" />
                全部写操作记入审计（操作者 / 动作 / 对象 / 结果）
              </div>
            </div>
          </div>
        </Section>
      </div>

      <div className="pb-2 text-center text-[11px] text-idle">
        仍有疑问？查看项目根目录《用户操作手册》与《部署手册》；
        <ExternalLink size={10} className="mx-0.5 inline" />
        运行指标见「系统状态」页。
      </div>
    </div>
  )
}

/** 键帽样式。 */
function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-md border border-line bg-canvas px-1.5 py-0.5 font-mono text-[11px] text-ink">
      {children}
    </kbd>
  )
}
