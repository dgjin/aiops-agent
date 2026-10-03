/** 系统状态：Temporal / 策略 / 索引 / 稳定版探测 / 最近发布（设计方案 7.6）。 */

import type { ReactNode } from 'react'
import { useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, describeError } from '../lib/api'
import { fmtDateTime } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { EmptyState } from '../components/EmptyState'
import { JsonBlock } from '../components/JsonBlock'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Toast } from '../components/Toast'

function Card({
  title,
  extra,
  children,
  className,
}: {
  title: string
  extra?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={className ? `rounded-xl border border-line bg-panel p-5 ${className}` : 'rounded-xl border border-line bg-panel p-5'}>
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium">{title}</h2>
        {extra}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  )
}

function Metric({ label, value, mono = true }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className={mono ? 'mt-1 font-mono text-sm' : 'mt-1 text-sm'}>{value ?? '—'}</div>
    </div>
  )
}

export function SystemPage() {
  const { data, isError, error } = useQuery({
    queryKey: ['system'],
    queryFn: api.system,
    refetchInterval: 15000,
  })

  // kill switch 操作入口（仅 admin 可见；角色获取失败时降级为只读展示）
  const { data: authStatus } = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    refetchInterval: 30000,
    retry: false,
  })
  const killReasonRef = useRef('')
  const write = useWriteAction()

  if (isError) {
    return <EmptyState title="无法加载系统状态" hint={describeError(error)} />
  }
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  const {
    temporal,
    kill_switch: kill,
    policy,
    policy_error,
    index: idx,
    stable,
    monitored_apps,
    config_items,
    recent_releases,
  } = data

  const killActive = Boolean(kill.state?.active)
  const isAdmin = authStatus?.self_token?.role === 'admin'

  const EFFECT_META: Record<string, { label: string; cls: string }> = {
    hot: { label: '热生效', cls: 'border-ok/40 text-ok' },
    restart: { label: '需重启', cls: 'border-warn/40 text-warn' },
    snapshot: { label: '启动快照', cls: 'border-danger/40 text-danger' },
  }

  return (
    <div>
      <h1 className="text-lg font-medium">系统状态</h1>
      <p className="mt-1 text-xs text-muted">运行依赖与策略快照（15 秒自动刷新）</p>

      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <Card
          title="Kill Switch（全局熔断）"
          className="md:col-span-2"
          extra={
            <span className="inline-flex items-center gap-1.5 text-xs">
              <span
                className={`h-1.5 w-1.5 rounded-full ${killActive ? 'bg-danger animate-pulse' : 'bg-ok'}`}
              />
              <span className={killActive ? 'text-danger' : 'text-muted'}>
                {killActive ? '已激活 · 写操作被拒绝' : '未激活'}
              </span>
            </span>
          }
        >
          {kill.error ? (
            <div className="text-xs text-danger">状态不可读：{kill.error}</div>
          ) : killActive && kill.state ? (
            <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2.5">
              <div className="text-xs text-danger">
                审批、发布指令、配置变更等一切写操作与 webhook 新流程均已拒绝；关闭后恢复。
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-3">
                <Metric label="操作者" value={kill.state.actor || '—'} />
                <Metric label="激活时间" value={fmtDateTime(kill.state.since)} />
                <Metric label="最近更新" value={fmtDateTime(kill.state.updated_at)} />
              </div>
              {kill.state.reason && (
                <div className="mt-2 text-xs text-muted">原因：{kill.state.reason}</div>
              )}
            </div>
          ) : (
            <div className="text-xs text-muted">
              未激活。故障时的紧急停止：激活后将拒绝一切写操作（审批、发布指令、配置维护）与
              webhook 新流程启动；正在运行的流程不受影响。
              {kill.state?.updated_at && (
                <span className="ml-1">最近更新：{fmtDateTime(kill.state.updated_at)}</span>
              )}
            </div>
          )}
          {isAdmin && !kill.error && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {killActive ? (
                <button
                  type="button"
                  onClick={() =>
                    write.open({
                      title: '关闭 kill switch？',
                      confirmLabel: '关闭并恢复写操作',
                      detail: (
                        <div className="text-xs text-muted">
                          关闭后，审批、发布指令与配置维护将恢复可用；本操作会记录审计。
                        </div>
                      ),
                      run: () => api.killSwitch(false, '手动关闭'),
                      success: '已关闭 kill switch，写操作恢复',
                    })
                  }
                  className="rounded-md border border-line px-2.5 py-1 text-xs text-muted hover:bg-elevated hover:text-ink"
                >
                  关闭 kill switch
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    killReasonRef.current = ''
                    write.open({
                      title: '激活 kill switch（紧急停止）？',
                      tone: 'danger',
                      confirmLabel: '立即激活',
                      detail: (
                        <div className="space-y-2.5">
                          <div className="text-xs">
                            激活后将<b className="text-danger">立即拒绝</b>所有写操作（审批、发布指令、
                            配置维护）并拒绝 webhook 启动新流程；正在运行的流程不受影响。仅本管理端点保持
                            可用，以便随时关闭。
                          </div>
                          <input
                            defaultValue=""
                            onChange={(event) => {
                              killReasonRef.current = event.target.value
                            }}
                            placeholder="激活原因（建议填写，将记录到审计）"
                            className="w-full rounded-md border border-line bg-canvas px-2.5 py-1.5 text-xs text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none"
                          />
                        </div>
                      ),
                      run: () => api.killSwitch(true, killReasonRef.current.trim() || '未注明'),
                      success: '已激活 kill switch：所有写操作已被拒绝',
                    })
                  }}
                  className="rounded-md border border-danger/40 px-2.5 py-1 text-xs text-danger hover:bg-danger/10"
                >
                  激活（紧急停止）
                </button>
              )}
            </div>
          )}
        </Card>

        <Card
          title="Temporal"
          extra={
            <span className="inline-flex items-center gap-1.5 text-xs text-muted">
              <span className={`h-1.5 w-1.5 rounded-full ${temporal.connected ? 'bg-ok' : 'bg-danger animate-pulse'}`} />
              {temporal.connected ? '已连接' : '断开'}
            </span>
          }
        >
          <div className="grid grid-cols-2 gap-3">
            <Metric label="地址" value={temporal.address} />
            <Metric label="延迟" value={temporal.connected ? `${temporal.latency_ms} ms` : '—'} />
          </div>
          {temporal.error && <div className="mt-3 text-xs text-danger">{temporal.error}</div>}
        </Card>

        <Card
          title="稳定版服务探测"
          extra={
            <span className="inline-flex items-center gap-1.5 text-xs text-muted">
              <span className={`h-1.5 w-1.5 rounded-full ${stable.running ? 'bg-ok' : 'bg-idle'}`} />
              {stable.running ? '在线' : '未运行'}
            </span>
          }
        >
          <div className="grid grid-cols-2 gap-3">
            <Metric label="端口" value={stable.port} />
            <Metric label="探测目标" value={stable.target} />
          </div>
          {stable.running && stable.body ? (
            <div className="mt-3">
              <JsonBlock data={stable.body} />
            </div>
          ) : (
            stable.error && <div className="mt-3 text-xs text-muted">{stable.error}</div>
          )}
        </Card>

        <Card
          title="被监控应用"
          extra={<span className="text-xs text-muted">{monitored_apps.length} 个 · 在「被监控应用」页维护</span>}
        >
          {monitored_apps.length === 0 ? (
            <EmptyState title="暂无被监控应用" hint="在「被监控应用」页新增" />
          ) : (
            <ul className="space-y-3">
              {monitored_apps.map((app) => {
                const running = app.enabled && app.probe.running
                const dot = !app.enabled ? 'bg-idle' : running ? 'bg-ok' : 'bg-danger animate-pulse'
                const text = !app.enabled
                  ? '已停用'
                  : app.probe.running
                    ? `在线 ${app.probe.status_code ?? ''}${app.probe.latency_ms != null ? ` · ${app.probe.latency_ms}ms` : ''}`
                    : '不可达'
                return (
                  <li key={app.id} className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-sm">{app.name}</div>
                      <div className="mt-0.5 truncate font-mono text-[10px] text-idle">{app.url}</div>
                    </div>
                    <span
                      className={`inline-flex items-center gap-1.5 text-xs ${running || !app.enabled ? 'text-muted' : 'text-danger'}`}
                    >
                      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
                      {text}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
        </Card>

        <Card
          title="配置项（来源与生效方式）"
          className="md:col-span-2"
          extra={<span className="text-xs text-muted">改前先看「生效方式」，避免以为改了其实没生效</span>}
        >
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-line text-left text-muted">
                  <th className="py-2 pr-3 font-medium">配置</th>
                  <th className="py-2 pr-3 font-medium">当前值</th>
                  <th className="py-2 pr-3 font-medium">来源</th>
                  <th className="py-2 pr-3 font-medium">生效方式</th>
                  <th className="py-2 pr-3 font-medium">运行中流程</th>
                </tr>
              </thead>
              <tbody>
                {config_items.map((item) => {
                  const meta = EFFECT_META[item.effect] ?? EFFECT_META.restart
                  return (
                    <tr key={item.key} className="border-b border-line/60 last:border-0">
                      <td className="py-2 pr-3 align-top">
                        <div className="text-ink">{item.label}</div>
                        <div className="mt-0.5 font-mono text-[10px] text-idle">{item.key}</div>
                      </td>
                      <td className="max-w-[16rem] py-2 pr-3 align-top">
                        <span className="block break-words font-mono text-[11px] text-muted">{item.value}</span>
                        {item.note && <div className="mt-0.5 text-[10px] text-idle">{item.note}</div>}
                      </td>
                      <td className="py-2 pr-3 align-top text-muted">{item.source}</td>
                      <td className="py-2 pr-3 align-top">
                        <span className={`whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px] ${meta.cls}`}>
                          {meta.label}
                        </span>
                        <div className="mt-0.5 text-[10px] text-idle">{item.owner}</div>
                      </td>
                      <td className="py-2 pr-3 align-top">
                        {item.applies_to_running ? (
                          <span className="text-ok">生效</span>
                        ) : (
                          <span className="text-muted">不生效</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="策略快照" className="md:col-span-2">
          {policy ? (
            <>
              <div className="text-xs text-muted">来源：{policy.source}</div>
              <div className="mt-1 text-sm">{policy.summary}</div>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <div>
                  <div className="mb-1.5 text-xs text-muted">triage</div>
                  <JsonBlock data={policy.triage} />
                </div>
                <div>
                  <div className="mb-1.5 text-xs text-muted">approval</div>
                  <JsonBlock data={policy.approval} />
                </div>
                <div>
                  <div className="mb-1.5 text-xs text-muted">notify_window</div>
                  <JsonBlock data={policy.notify_window} />
                </div>
                <div>
                  <div className="mb-1.5 text-xs text-muted">canary</div>
                  <JsonBlock data={policy.canary} />
                </div>
              </div>
              <div className="mt-4 border-t border-line pt-4">
                <div className="mb-2 text-xs text-muted">策略锁定项（不可通过控制台修改）</div>
                <ul className="space-y-1.5">
                  {policy.locks.map((lock) => (
                    <li key={lock.key} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                      <span className="font-mono text-muted">{lock.key}</span>
                      <span className="font-mono text-ink">{JSON.stringify(lock.value)}</span>
                      <span className="text-idle">{lock.reason}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </>
          ) : (
            <div className="text-xs text-danger">{policy_error ?? '策略不可用'}</div>
          )}
        </Card>

        <Card title="知识索引">
          {idx ? (
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
              <Metric label="模型" value={idx.model} />
              <Metric label="向量维度" value={idx.dim} />
              <Metric label="后端" value={idx.backend} />
              <Metric label="分块数" value={idx.chunks} />
              <Metric label="文件数" value={idx.files} />
              <Metric label="工单数" value={idx.tickets} />
              <Metric label="FAISS 索引" value={idx.faiss_file ? '存在' : '缺失'} />
              <Metric label="创建时间" value={fmtDateTime(idx.created_at)} />
              <Metric label="仓库" value={idx.repo} />
            </div>
          ) : (
            <EmptyState title="索引不可用" hint="请先执行索引构建" />
          )}
        </Card>

        <Card title="最近发布">
          {recent_releases.length ? (
            <ul className="space-y-2.5">
              {recent_releases.map((release) => (
                <li key={release.file} className="text-xs">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className="text-sm">{release.name ?? release.file}</span>
                    {release.version && <span className="font-mono text-accent">{release.version}</span>}
                    {release.service && <span className="text-muted">{release.service}</span>}
                  </div>
                  <div className="mt-1 font-mono text-[10px] text-idle">
                    {release.file} · {fmtDateTime(release.mtime)}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="暂无发布记录" hint="完成发布后这里会显示最近版本" />
          )}
        </Card>
      </div>

      <ConfirmDialog
        open={write.spec !== null}
        title={write.spec?.title ?? ''}
        tone={write.spec?.tone}
        confirmLabel={write.spec?.confirmLabel}
        busy={write.busy}
        error={write.error}
        onConfirm={write.confirm}
        onClose={write.close}
      >
        {write.spec?.detail}
      </ConfirmDialog>
      <Toast message={write.toast} />
    </div>
  )
}
