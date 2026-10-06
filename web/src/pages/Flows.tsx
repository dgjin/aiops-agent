/** 流程列表：状态 / 阶段 / 服务过滤 + 明细表（设计方案 7.3）。 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api, describeError } from '../lib/api'
import { fmtDateTime, fmtDuration } from '../lib/format'
import { EmptyState } from '../components/EmptyState'
import { STAGE_META, StageBadge } from '../components/StageBadge'

const STAGE_OPTIONS = [
  'DETECTED',
  'TRIAGING',
  'FIXING',
  'TESTING',
  'WAIT_APPROVAL',
  'NOTIFYING',
  'CANARY',
  'ROLLING_OUT',
  'DONE',
  'SHADOWED',
  'ESCALATED',
  'FAILED',
  'CANCELLED',
]

const INPUT_CLS =
  'rounded-lg border border-line bg-panel px-3 py-1.5 text-sm text-ink focus:border-accent/50 focus:outline-none'

export function Flows() {
  // 支持从看板环图跳转（/flows?stage=DONE）：以 URL 参数初始化筛选
  const [params] = useSearchParams()
  const [status, setStatus] = useState('all')
  const [stage, setStage] = useState(params.get('stage') ?? '')
  const [service, setService] = useState(params.get('service') ?? '')

  const { data, isError, error } = useQuery({
    queryKey: ['flows', status, stage, service],
    queryFn: () => api.flows({ status, stage: stage || undefined, service: service || undefined }),
    refetchInterval: 5000,
  })

  return (
    <div>
      <h1 className="text-lg font-medium">流程列表</h1>
      <p className="mt-1 text-xs text-muted">全部工作流（含终态），支持状态 / 阶段 / 服务过滤</p>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <select value={status} onChange={(e) => setStatus(e.target.value)} className={INPUT_CLS}>
          <option value="all">全部状态</option>
          <option value="open">运行中</option>
          <option value="closed">已结束</option>
        </select>
        <select value={stage} onChange={(e) => setStage(e.target.value)} className={INPUT_CLS}>
          <option value="">全部阶段</option>
          {STAGE_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {STAGE_META[s]?.label ?? s}
            </option>
          ))}
        </select>
        <input
          value={service}
          onChange={(e) => setService(e.target.value)}
          placeholder="按服务名过滤"
          className={INPUT_CLS}
        />
        {data && <span className="ml-auto text-xs text-idle">共 {data.items.length} 条</span>}
      </div>

      {isError && (
        <div className="mt-4">
          <EmptyState title="无法加载流程列表" hint={describeError(error)} />
        </div>
      )}

      {data &&
        (data.items.length ? (
          <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-panel">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2.5 font-medium">工作流</th>
                  <th className="px-4 py-2.5 font-medium">服务</th>
                  <th className="px-4 py-2.5 font-medium">告警</th>
                  <th className="px-4 py-2.5 font-medium">阶段</th>
                  <th className="px-4 py-2.5 font-medium">开始时间</th>
                  <th className="px-4 py-2.5 font-medium">耗时</th>
                  <th className="px-4 py-2.5 font-medium" />
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.wf_id} className="border-b border-line/60 last:border-0 hover:bg-elevated/60">
                    <td className="px-4 py-2.5 font-mono text-xs">{item.wf_id}</td>
                    <td className="px-4 py-2.5">{item.alert?.service ?? '—'}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {item.alert?.alert_id ?? '—'}
                    </td>
                    <td className="px-4 py-2.5">
                      <StageBadge stage={item.stage} />
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted">{fmtDateTime(item.start_time)}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted">
                      {item.exec_status === 'RUNNING' ? '运行中' : fmtDuration(item.duration_seconds)}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Link
                        to={`/flows/${encodeURIComponent(item.wf_id)}`}
                        className="text-xs text-accent hover:underline"
                      >
                        详情
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mt-4">
            <EmptyState title="没有匹配的流程" hint="调整过滤条件，或等待新告警接入" />
          </div>
        ))}
    </div>
  )
}
