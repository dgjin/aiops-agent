/** 转人工待办（P1-3）：ESCALATED 处置闭环——指派 / 处置关闭 / 重试修复。
 *
 * - 列表 3 秒轮询；读侧自动幂等登记（ESCALATED 流程 → open 待办），已关闭不重开；
 * - 超 SLA（默认 30 分钟）未处置的条目由 BFF 后台巡检经通知渠道再升级一次，
 *   前端以「已超时未处置 / 已再升级」标记提示值班同学；
 * - 处置路径：指派责任人 → 手动关闭（备注留档）或「重试修复」（新幂等键重启完整修复链路）；
 *   需求来源的待办（entry.requirement）重试时重启需求修复流（批准版方案 → 沙箱 → 审批 → 发布）。
 * - 内容管理：关键词搜索（服务 / 升级原因 / 告警 / 工作流 / 责任人 / 备注）+ 来源筛选
 *   （告警 / 需求）+ 仅看超时 + 排序（待处置优先 / 最近登记 / 最早登记）；状态页签带全量
 *   计数；卡片双列布局（超长原因截断，悬停看全文）——均前端内存计算，状态页签仍走服务端。
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Search, X } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CopyableId } from '../components/CopyableId'
import { EmptyState } from '../components/EmptyState'
import {
  Modal,
  fieldClass,
  primaryButtonClass,
  secondaryButtonClass,
} from '../components/Modal'
import { Pagination, usePagination } from '../components/Pagination'
import { PermissionGate } from '../components/PermissionGate'
import { Toast } from '../components/Toast'
import type { EscalationEntry } from '../lib/types'

const STATUS_TABS = [
  { value: 'all', label: '全部' },
  { value: 'open', label: '待指派' },
  { value: 'assigned', label: '已指派' },
  { value: 'closed', label: '已关闭' },
] as const

type StatusFilter = (typeof STATUS_TABS)[number]['value']

const STATUS_META: Record<EscalationEntry['status'], { label: string; cls: string }> = {
  open: { label: '待指派', cls: 'border-danger/40 text-danger' },
  assigned: { label: '已指派', cls: 'border-warn/40 text-warn' },
  closed: { label: '已关闭', cls: 'border-ok/40 text-ok' },
}

/** 筛选/排序下拉统一输入样式（同 Requirements / Audit 页 INPUT_CLS 族）。 */
const SELECT_CLS =
  'rounded-lg border border-line bg-panel px-2 py-1.5 text-xs text-ink focus:border-accent/50 focus:outline-none'

/** 排序方式：待处置优先（默认）/ 最近登记 / 最早登记。 */
const SORT_OPTIONS = [
  { key: 'default', label: '待处置优先' },
  { key: 'escalated_desc', label: '最近登记' },
  { key: 'escalated_asc', label: '最早登记' },
] as const
type SortKey = (typeof SORT_OPTIONS)[number]['key']

/** 状态权重（默认排序组内：open → assigned → closed）。 */
const STATUS_RANK: Record<EscalationEntry['status'], number> = { open: 0, assigned: 1, closed: 2 }

const tsOf = (value: string | null | undefined): number => (value ? Date.parse(value) : 0)

/** 默认排序：未关闭在前 → 超时在前 → 状态权重 → 早登记先处置。 */
function compareEscalations(a: EscalationEntry, b: EscalationEntry, sort: SortKey): number {
  if (sort === 'escalated_desc') return tsOf(b.escalated_at) - tsOf(a.escalated_at)
  if (sort === 'escalated_asc') return tsOf(a.escalated_at) - tsOf(b.escalated_at)
  const closed = Number(a.status === 'closed') - Number(b.status === 'closed')
  if (closed) return closed
  const overdue = Number(b.overdue) - Number(a.overdue)
  if (overdue) return overdue
  const rank = STATUS_RANK[a.status] - STATUS_RANK[b.status]
  if (rank) return rank
  return tsOf(a.escalated_at) - tsOf(b.escalated_at)
}

/** 指派/改派责任人（提交即热生效，落操作审计）。 */
function AssignDialog({ entry, onClose }: { entry: EscalationEntry | null; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [assignee, setAssignee] = useState(entry?.assignee ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const close = () => {
    if (busy) return
    onClose()
  }

  const submit = async () => {
    if (!entry) return
    if (!assignee.trim()) {
      setError('责任人不能为空')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.assignEscalation(entry.id, assignee.trim())
      await queryClient.invalidateQueries()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={entry !== null}
      title={entry?.assignee ? '改派责任人' : '指派责任人'}
      onClose={close}
      footer={
        <>
          <button type="button" className={secondaryButtonClass} onClick={close}>
            取消
          </button>
          <button type="button" className={primaryButtonClass} disabled={busy} onClick={submit}>
            {busy ? '提交中…' : entry?.assignee ? '改派' : '指派'}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
          <div>服务：{entry?.service || '—'}</div>
          <div className="truncate" title={entry?.auto_reason}>
            原因：{entry?.auto_reason || '—'}
          </div>
          <div className="col-span-2">
            工作流：<span className="font-mono text-xs">{entry?.wf_id ?? '—'}</span>
          </div>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">责任人（值班同学 / 小组）*</label>
          <input
            autoFocus
            value={assignee}
            onChange={(event) => setAssignee(event.target.value)}
            placeholder="例如 zhangsan 或 支付值班组"
            className={fieldClass}
          />
        </div>
        {entry?.assignee && (
          <p className="text-xs text-idle">当前责任人：{entry.assignee}（提交即改派）</p>
        )}
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
    </Modal>
  )
}

/** 处置关闭：备注写入处置轨迹（history）与操作审计。 */
function CloseDialog({ entry, onClose }: { entry: EscalationEntry; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const close = () => {
    if (busy) return
    onClose()
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.closeEscalation(entry.id, note.trim())
      await queryClient.invalidateQueries()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      title="处置关闭"
      onClose={close}
      footer={
        <>
          <button type="button" className={secondaryButtonClass} onClick={close}>
            取消
          </button>
          <button type="button" className={primaryButtonClass} disabled={busy} onClick={submit}>
            {busy ? '提交中…' : '确认关闭'}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted">
          <div>服务：{entry.service || '—'}</div>
          <div className="truncate" title={entry.auto_reason}>
            原因：{entry.auto_reason || '—'}
          </div>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">
            处置备注（写入处置轨迹与操作审计）
          </label>
          <textarea
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="例如：已人工回滚发布，错误率恢复；或已知问题，无需自动修复"
            className={fieldClass}
          />
        </div>
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
    </Modal>
  )
}

export function Escalations() {
  const write = useWriteAction()
  const [status, setStatus] = useState<StatusFilter>('all')
  const [query, setQuery] = useState('')
  const [filterSource, setFilterSource] = useState('')
  const [onlyOverdue, setOnlyOverdue] = useState(false)
  const [sort, setSort] = useState<SortKey>('default')
  const [assignTarget, setAssignTarget] = useState<EscalationEntry | null>(null)
  const [closeTarget, setCloseTarget] = useState<EscalationEntry | null>(null)

  const { data, isError, error } = useQuery({
    queryKey: ['escalations', status],
    queryFn: () => api.escalations(status),
    refetchInterval: 3000,
  })

  /** 重置全部内容管理条件。 */
  const resetConditions = () => {
    setQuery('')
    setFilterSource('')
    setOnlyOverdue(false)
    setSort('default')
  }

  // ---- 内容管理：搜索 + 筛选 + 排序（前端内存计算；状态页签仍走服务端过滤）----
  const items = data?.escalations ?? []
  const q = query.trim().toLowerCase()
  const matched = items.filter((entry) => {
    if (filterSource === 'requirement' && !entry.requirement) return false
    if (filterSource === 'alert' && entry.requirement) return false
    if (onlyOverdue && !entry.overdue) return false
    if (!q) return true
    // 命中范围：服务 / 升级原因 / 告警 / 工作流 / 责任人 / 备注 / 来源标签
    const haystack = [
      entry.service,
      entry.auto_reason,
      entry.alert_id,
      entry.wf_id,
      entry.assignee,
      entry.note,
      entry.requirement ? '需求' : '告警',
    ]
      .map((value) => String(value ?? ''))
      .join('\n')
      .toLowerCase()
    return haystack.includes(q)
  })
  const sorted = [...matched].sort((a, b) => compareEscalations(a, b, sort))
  const overdueCount = items.filter((entry) => entry.overdue).length
  const hasConditions = Boolean(q || filterSource || onlyOverdue || sort !== 'default')

  // ---- 分页：状态页签 / 条件变化回到第 1 页（越界页码自动夹取）----
  const pg = usePagination(sorted.length, 10)
  const { setPage } = pg
  useEffect(() => {
    setPage(1)
  }, [status, query, filterSource, onlyOverdue, sort, setPage])
  const paged = sorted.slice(pg.start, pg.start + pg.pageSize)

  /** 页签计数（stats 为全量口径，不随 status 变化）。 */
  const tabCount = (tab: StatusFilter): number => {
    const stats = data?.stats
    if (!stats) return 0
    if (tab === 'open') return stats.open
    if (tab === 'assigned') return stats.assigned
    if (tab === 'closed') return stats.closed
    return stats.total
  }

  const openRetry = (entry: EscalationEntry) => {
    write.open({
      title: '确认重试修复？',
      confirmLabel: '启动新流程',
      detail: (
        <div className="space-y-2 text-sm">
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <div>服务：{entry.service || '—'}</div>
            <div>原告警：{entry.alert_id || '—'}</div>
            <div className="col-span-2">
              工作流：<span className="font-mono text-xs">{entry.wf_id}</span>
            </div>
          </div>
          <p className="text-xs text-idle">
            {entry.requirement
              ? '需求来源待办：以新幂等键重启需求修复链路（批准版方案 → 补丁 → 沙箱 → 审批 → 公告 → 发布），不重走日志诊断；启动成功后本待办自动关闭。'
              : '以新幂等键（自动追加 retry 序号）重启完整修复链路：诊断 → 补丁 → 沙箱 → 审批 → 发布；启动成功后本待办自动关闭。'}
          </p>
        </div>
      ),
      run: () => api.retryEscalation(entry.id),
      success: '已启动重试修复流程',
    })
  }

  return (
    <div>
      <h1 className="text-lg font-medium">转人工待办</h1>
      <p className="mt-1 text-xs text-muted">
        升级人工后的处置闭环（3 秒自动刷新）：指派责任人 → 处置关闭或重试修复；超时未处置将自动再升级
      </p>

      {/* 状态页签（计数为全量口径）+ SLA 说明 */}
      <div className="mt-4 flex flex-wrap items-center gap-1.5">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.value}
            type="button"
            onClick={() => setStatus(tab.value)}
            className={cn(
              'rounded-lg px-3 py-1 text-xs transition-colors',
              status === tab.value
                ? 'bg-elevated text-ink'
                : 'text-muted hover:bg-elevated hover:text-ink',
            )}
          >
            {tab.label}
            <span className={cn('ml-1 font-mono', status === tab.value ? 'text-accent' : 'text-idle')}>
              {tabCount(tab.value)}
            </span>
          </button>
        ))}
        {data && (
          <span className="ml-auto text-xs text-idle">
            SLA {data.stats.sla_minutes} 分钟：超时未处置自动再升级一次（每单一次，防轰炸）
          </span>
        )}
      </div>

      {data && (
        <>
          {/* 工具条：搜索 + 来源筛选 + 仅看超时 + 排序 + 计数 */}
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search
                size={13}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-idle"
              />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') setQuery('')
                }}
                placeholder="搜索服务 / 原因 / 告警 / 工作流 / 责任人"
                className="w-72 rounded-lg border border-line bg-panel py-1.5 pl-8 pr-7 text-xs text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery('')}
                  title="清除搜索"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-idle transition-colors hover:text-ink"
                >
                  <X size={13} />
                </button>
              )}
            </div>
            <select
              value={filterSource}
              onChange={(event) => setFilterSource(event.target.value)}
              className={SELECT_CLS}
            >
              <option value="">全部来源</option>
              <option value="alert">告警来源</option>
              <option value="requirement">需求来源</option>
            </select>
            {overdueCount > 0 && (
              <button
                type="button"
                onClick={() => setOnlyOverdue((value) => !value)}
                title="只看已超 SLA 未处置的待办"
                className={cn(
                  'rounded-lg border px-2 py-1.5 text-xs transition-colors',
                  onlyOverdue
                    ? 'border-danger/40 bg-danger/10 text-danger'
                    : 'border-line text-muted hover:bg-elevated hover:text-ink',
                )}
              >
                仅看超时（{overdueCount}）
              </button>
            )}
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as SortKey)}
              className={SELECT_CLS}
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.key} value={option.key}>
                  排序：{option.label}
                </option>
              ))}
            </select>
            {hasConditions && (
              <button
                type="button"
                onClick={resetConditions}
                className="rounded-lg border border-line px-2 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
              >
                重置条件
              </button>
            )}
            <span className="ml-auto text-xs text-muted">
              显示 <span className="text-ink">{sorted.length}</span> / 共 {items.length} 条
            </span>
          </div>
        </>
      )}

      {isError && (
        <div className="mt-5">
          <EmptyState title="无法加载待办列表" hint={describeError(error)} />
        </div>
      )}

      {data &&
        (items.length ? (
          sorted.length === 0 ? (
            <div className="mt-5">
              <EmptyState
                title="没有匹配的待办"
                hint="试试调整关键词，或重置筛选条件查看全部待办"
              />
              <div className="mt-3 flex justify-center">
                <button
                  type="button"
                  onClick={resetConditions}
                  className="rounded-lg border border-accent/40 px-3.5 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/10"
                >
                  重置条件
                </button>
              </div>
            </div>
          ) : (
          <>
          <div className="mt-5 grid gap-3 lg:grid-cols-2">
            {paged.map((entry) => {
              const meta = STATUS_META[entry.status]
              const open = entry.status !== 'closed'
              return (
                <div key={entry.id} className="rounded-xl border border-line bg-panel p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-3">
                        <span className="text-sm font-medium">{entry.service || '未知服务'}</span>
                        <span className={cn('rounded border px-1.5 py-0.5 text-[10px]', meta.cls)}>
                          {meta.label}
                        </span>
                        {entry.requirement && (
                          <span className="rounded border border-accent/40 px-1.5 py-0.5 text-[10px] text-accent">
                            需求
                          </span>
                        )}
                        {open && entry.overdue && (
                          <span className="rounded border border-danger/40 bg-danger/10 px-1.5 py-0.5 text-[10px] text-danger">
                            已超时未处置
                          </span>
                        )}
                        {entry.re_escalated && (
                          <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-idle">
                            已再升级
                          </span>
                        )}
                      </div>
                      <div className="mt-2 flex gap-1 text-xs text-muted">
                        <span className="shrink-0">升级原因：</span>
                        <span className="line-clamp-2 text-ink" title={entry.auto_reason}>
                          {entry.auto_reason}
                        </span>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
                        <span>
                          告警 <CopyableId value={entry.alert_id || '—'} />
                        </span>
                        <span>
                          工作流 <CopyableId value={entry.wf_id} />
                        </span>
                        <span>登记 {fmtDateTime(entry.escalated_at)}</span>
                        {entry.assignee && (
                          <span>
                            责任人 <span className="text-ink">{entry.assignee}</span>
                          </span>
                        )}
                        {entry.status === 'closed' && entry.note && (
                          <span>备注：{entry.note}</span>
                        )}
                      </div>
                    </div>
                    <Link
                      to={`/flows/${encodeURIComponent(entry.wf_id)}`}
                      className="shrink-0 text-xs text-accent hover:underline"
                    >
                      流程详情 →
                    </Link>
                  </div>
                  {open && (
                    <PermissionGate require="operator" fallback={null}>
                      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line/60 pt-3">
                        <ActionButton onClick={() => setAssignTarget(entry)}>
                          {entry.assignee ? '改派' : '指派'}
                        </ActionButton>
                        <ActionButton onClick={() => setCloseTarget(entry)}>处置关闭</ActionButton>
                        <ActionButton tone="accent" onClick={() => openRetry(entry)}>
                          重试修复
                        </ActionButton>
                      </div>
                    </PermissionGate>
                  )}
                </div>
              )
            })}
          </div>
          <Pagination pg={pg} />
          </>
          )
        ) : (
          <div className="mt-5">
            <EmptyState
              title="当前没有转人工待办"
              hint="根因不确定 / 审批驳回 / 测试回炉耗尽 / 金丝雀劣化回滚的流程会自动登记到这里"
            />
          </div>
        ))}

      {assignTarget && (
        <AssignDialog
          key={assignTarget.id}
          entry={assignTarget}
          onClose={() => setAssignTarget(null)}
        />
      )}
      {closeTarget && (
        <CloseDialog key={closeTarget.id} entry={closeTarget} onClose={() => setCloseTarget(null)} />
      )}

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
