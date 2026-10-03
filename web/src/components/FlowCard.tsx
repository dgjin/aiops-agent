import { Link } from 'react-router-dom'
import type { FlowItem } from '../lib/types'
import { cn, fmtDuration, fmtTime } from '../lib/format'
import { CountdownText } from './CountdownText'
import { StageBadge } from './StageBadge'

/** 流程卡片（看板卡片墙 / 详情跳转入口）。 */
export function FlowCard({ item }: { item: FlowItem }) {
  const alert = item.alert
  const running = item.exec_status === 'RUNNING'
  return (
    <Link
      to={`/flows/${encodeURIComponent(item.wf_id)}`}
      className="block rounded-xl border border-line bg-panel p-4 transition-colors hover:border-accent/40 hover:bg-elevated"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{alert?.service ?? '未知服务'}</div>
          <div className="mt-0.5 truncate font-mono text-xs text-muted">
            {alert?.alert_id ?? item.wf_id}
          </div>
        </div>
        <StageBadge stage={item.stage} />
      </div>
      <div className="mt-3 flex items-center justify-between gap-3 text-xs text-muted">
        <span className="truncate">
          {running ? `开始于 ${fmtTime(item.start_time)}` : `耗时 ${fmtDuration(item.duration_seconds)}`}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {item.needs_second && (
            <span className="rounded border border-warn/40 px-1.5 py-0.5 text-[10px] text-warn">
              二级审批
            </span>
          )}
          {item.deadline && <CountdownText at={item.deadline.at} className="text-sm" />}
        </span>
      </div>
      {item.queued_patches.length > 0 && (
        <div className={cn('mt-2.5 text-xs text-muted')}>
          队列中 {item.queued_patches.length} 个补丁：
          <span className="font-mono">{item.queued_patches.map((p) => p.alert_id).join('、')}</span>
        </div>
      )}
      {item.stage_error && <div className="mt-2 text-xs text-danger">{item.stage_error}</div>}
    </Link>
  )
}
