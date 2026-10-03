/** 系统状态：Temporal / 策略 / 索引 / 稳定版探测 / 最近发布（设计方案 7.6）。 */

import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { fmtDateTime } from '../lib/format'
import { EmptyState } from '../components/EmptyState'
import { JsonBlock } from '../components/JsonBlock'

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

  if (isError) {
    return <EmptyState title="无法加载系统状态" hint={String(error)} />
  }
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  const { temporal, policy, policy_error, index: idx, stable, recent_releases } = data

  return (
    <div>
      <h1 className="text-lg font-medium">系统状态</h1>
      <p className="mt-1 text-xs text-muted">运行依赖与策略快照（15 秒自动刷新）</p>

      <div className="mt-5 grid gap-4 md:grid-cols-2">
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
    </div>
  )
}
