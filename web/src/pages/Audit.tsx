/** 审计回看：终态流程审计 + 控制台操作审计（设计方案 7.5）。 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { cn, downloadJson, fmtDateTime } from '../lib/format'
import { EmptyState } from '../components/EmptyState'
import { StageBadge } from '../components/StageBadge'

const INPUT_CLS =
  'rounded-lg border border-line bg-panel px-3 py-1.5 text-sm text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none'

const ACTION_TONE: Array<{ match: string; cls: string }> = [
  { match: 'reject', cls: 'text-danger' },
  { match: 'cancel', cls: 'text-danger' },
  { match: 'approval:approve', cls: 'text-ok' },
  { match: 'second-approval:approve', cls: 'text-ok' },
  { match: 'deploy_now', cls: 'text-warn' },
  { match: 'queue-patch', cls: 'text-info' },
]

function actionCls(action: string): string {
  for (const item of ACTION_TONE) {
    if (action.includes(item.match)) return item.cls
  }
  return 'text-muted'
}

export function Audit() {
  const [q, setQ] = useState('')
  const [days, setDays] = useState(7)
  const [tab, setTab] = useState<'flows' | 'ops'>('flows')

  const { data, isError, error } = useQuery({
    queryKey: ['audit', q, days],
    queryFn: () => api.audit({ q: q || undefined, days }),
    refetchInterval: 15000,
  })

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">审计回看</h1>
          <p className="mt-1 text-xs text-muted">终态流程审计记录与控制台操作留痕</p>
        </div>
        {data && (
          <button
            type="button"
            onClick={() => downloadJson('aiops-audit.json', data)}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
          >
            导出 JSON
          </button>
        )}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索工作流 / 告警 / 服务 / 补丁"
          className={cn(INPUT_CLS, 'w-72')}
        />
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} className={INPUT_CLS}>
          <option value={1}>近 1 天</option>
          <option value={3}>近 3 天</option>
          <option value={7}>近 7 天</option>
          <option value={30}>近 30 天</option>
        </select>
        <div className="ml-auto flex gap-1 rounded-lg border border-line bg-panel p-0.5">
          {(
            [
              { key: 'flows', label: '流程审计' },
              { key: 'ops', label: '操作审计' },
            ] as const
          ).map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setTab(item.key)}
              className={cn(
                'rounded-md px-3 py-1 text-xs transition-colors',
                tab === item.key ? 'bg-accent/15 text-accent' : 'text-muted hover:text-ink',
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {isError && (
        <div className="mt-4">
          <EmptyState title="无法加载审计数据" hint={String(error)} />
        </div>
      )}

      {data && tab === 'flows' && (
        data.items.length ? (
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2.5 font-medium">结束时间</th>
                  <th className="px-4 py-2.5 font-medium">服务</th>
                  <th className="px-4 py-2.5 font-medium">告警</th>
                  <th className="px-4 py-2.5 font-medium">阶段</th>
                  <th className="px-4 py-2.5 font-medium">补丁</th>
                  <th className="px-4 py-2.5 font-medium">置信度</th>
                  <th className="px-4 py-2.5 font-medium" />
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.wf_id} className="border-b border-line/60 last:border-0 hover:bg-elevated/60">
                    <td className="px-4 py-2.5 text-xs text-muted">{fmtDateTime(item.close_time)}</td>
                    <td className="px-4 py-2.5">{item.alert?.service ?? '—'}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {item.alert?.alert_id ?? '—'}
                    </td>
                    <td className="px-4 py-2.5">
                      <StageBadge stage={item.stage} />
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">{item.patch_id ?? '—'}</td>
                    <td className="px-4 py-2.5 font-mono text-xs">
                      {item.confidence != null ? item.confidence.toFixed(2) : '—'}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Link
                        to={`/flows/${encodeURIComponent(item.wf_id)}`}
                        className="text-xs text-accent hover:underline"
                      >
                        回看
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mt-4">
            <EmptyState title="没有匹配的审计记录" hint="调整搜索条件或时间范围" />
          </div>
        )
      )}

      {data && tab === 'ops' && (
        data.ops.length ? (
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2.5 font-medium">时间</th>
                  <th className="px-4 py-2.5 font-medium">操作者</th>
                  <th className="px-4 py-2.5 font-medium">动作</th>
                  <th className="px-4 py-2.5 font-medium">工作流</th>
                  <th className="px-4 py-2.5 font-medium">参数</th>
                  <th className="px-4 py-2.5 font-medium">结果</th>
                </tr>
              </thead>
              <tbody>
                {data.ops.map((row, index) => (
                  <tr key={`${row.ts}-${index}`} className="border-b border-line/60 last:border-0 hover:bg-elevated/60">
                    <td className="px-4 py-2.5 text-xs text-muted">{fmtDateTime(row.ts)}</td>
                    <td className="px-4 py-2.5 text-xs">{row.actor}</td>
                    <td className={cn('px-4 py-2.5 font-mono text-xs', actionCls(row.action))}>
                      {row.action}
                    </td>
                    <td className="px-4 py-2.5">
                      <Link
                        to={`/flows/${encodeURIComponent(row.wf_id)}`}
                        className="font-mono text-xs text-accent hover:underline"
                      >
                        {row.wf_id}
                      </Link>
                    </td>
                    <td className="max-w-60 truncate px-4 py-2.5 font-mono text-xs text-muted">
                      {Object.keys(row.params).length ? JSON.stringify(row.params) : '—'}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted">{row.result}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mt-4">
            <EmptyState title="暂无控制台操作记录" hint="在控制台执行的审批 / 窗口操作会追加到这里" />
          </div>
        )
      )}
    </div>
  )
}
