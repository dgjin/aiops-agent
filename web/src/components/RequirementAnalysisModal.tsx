/** 需求智能分析弹窗：查看分析结果 → 管理员反馈再分析 → 批准进入修复工作流。
 *
 * 闭环（契约见 BFF /api/requirements/analyses）：
 *   发起分析（后台 LLM + 代码引用检索，3s 轮询）→ 查看结构化结果 →
 *   管理员提交优化建议 / 具体要求 → 系统带反馈再次分析（版本递增）→
 *   「同意」→ 启动需求驱动修复工作流（沙箱验证 / 审批 / 发布全链路）。
 * 权限与后端同源（middleware）：发起 / 反馈 / 批准需 admin；查看全部角色可读。
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { ConfirmDialog } from './ConfirmDialog'
import { Modal, fieldClass, primaryButtonClass, secondaryButtonClass } from './Modal'
import { PermissionGate } from './PermissionGate'
import type {
  RequirementAnalysisMeta,
  RequirementAnalysisResult,
  RequirementAnalysisVersion,
  RequirementsEntry,
} from '../lib/types'

/** 会话状态徽标（页面行内与弹窗共用；与后端 STATUSES 一致）。 */
export const ANALYSIS_STATUS_META: Record<string, { label: string; cls: string }> = {
  analyzing: { label: '分析中', cls: 'border-accent/40 bg-accent/10 text-accent' },
  analyzed: { label: '待批准', cls: 'border-line bg-elevated text-muted' },
  failed: { label: '分析失败', cls: 'border-danger/40 bg-danger/10 text-danger' },
  approved: { label: '已批准', cls: 'border-ok/40 bg-ok/10 text-ok' },
}

const TRIGGER_LABELS: Record<string, string> = {
  initial: '初始分析',
  feedback: '反馈再分析',
  retry: '失败重试',
}

const VERSION_STATUS_META: Record<string, { label: string; cls: string }> = {
  running: { label: '执行中', cls: 'text-accent' },
  done: { label: '完成', cls: 'text-ok' },
  failed: { label: '失败', cls: 'text-danger' },
}

/** 置信度配色：≥0.8 绿 / ≥0.5 黄 / 其余红。 */
function confidenceCls(value: number): string {
  if (value >= 0.8) return 'text-ok'
  if (value >= 0.5) return 'text-warn'
  return 'text-danger'
}

/** 分析结果结构化展示（理解 / 方案 / 涉及文件 / 验收要点 / 风险 / 复杂度）。 */
function ResultSection({
  result,
  meta,
}: {
  result: RequirementAnalysisResult
  meta: RequirementAnalysisMeta | null
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-idle">
        <span>
          置信度{' '}
          <span className={cn('font-mono', confidenceCls(result.confidence))}>
            {result.confidence.toFixed(2)}
          </span>
        </span>
        {meta?.model && <span>模型 {meta.model}</span>}
        {typeof meta?.elapsed_seconds === 'number' && <span>耗时 {meta.elapsed_seconds}s</span>}
      </div>

      <div className="rounded-lg border border-line bg-elevated/40 p-3">
        <div className="text-[11px] text-idle">需求理解</div>
        <p className="mt-1 text-xs text-ink">{result.understanding}</p>
      </div>

      {result.plan.length > 0 && (
        <div>
          <h3 className="text-xs font-medium text-ink">实现方案</h3>
          <ol className="mt-1.5 list-decimal space-y-1 pl-5 text-xs text-muted">
            {result.plan.map((step, index) => (
              <li key={index}>{step}</li>
            ))}
          </ol>
        </div>
      )}

      {result.suspect_files.length > 0 && (
        <div>
          <h3 className="text-xs font-medium text-ink">预计涉及文件</h3>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {result.suspect_files.map((file) => (
              <span
                key={file}
                className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted"
              >
                {file}
              </span>
            ))}
          </div>
        </div>
      )}

      {result.acceptance.length > 0 && (
        <div>
          <h3 className="text-xs font-medium text-ink">验收要点</h3>
          <ul className="mt-1.5 list-disc space-y-1 pl-5 text-xs text-muted">
            {result.acceptance.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {(result.risk || result.complexity) && (
        <div className="grid gap-2 text-xs sm:grid-cols-2">
          {result.risk && (
            <div className="rounded-lg border border-warn/30 bg-warn/5 p-2.5">
              <span className="text-[11px] text-warn">风险与注意事项</span>
              <p className="mt-1 text-muted">{result.risk}</p>
            </div>
          )}
          {result.complexity && (
            <div className="rounded-lg border border-line p-2.5">
              <span className="text-[11px] text-idle">复杂度</span>
              <p className="mt-1 text-muted">{result.complexity}</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** 迭代时间线（每版触发方式 / 状态 / 反馈原文；失败版本显示原因）。 */
function VersionTimeline({
  versions,
  currentVersion,
}: {
  versions: RequirementAnalysisVersion[]
  currentVersion: number
}) {
  return (
    <section>
      <h3 className="text-xs font-medium text-ink">迭代历史（当前 v{currentVersion}）</h3>
      <ol className="mt-2 space-y-2">
        {versions.map((item) => {
          const meta = VERSION_STATUS_META[item.status] ?? { label: item.status, cls: 'text-idle' }
          return (
            <li key={item.version} className="rounded-lg border border-line/70 px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="font-mono text-muted">v{item.version}</span>
                <span className="text-muted">{TRIGGER_LABELS[item.trigger] ?? item.trigger}</span>
                <span className={meta.cls}>{meta.label}</span>
                <span className="ml-auto text-idle">
                  {fmtDateTime(item.ts)} · {item.actor}
                </span>
              </div>
              {item.feedback && (
                <p className="mt-1.5 rounded bg-elevated/60 px-2 py-1.5 text-[11px] text-muted">
                  反馈：{item.feedback}
                </p>
              )}
              {item.status === 'failed' && item.error && (
                <p className="mt-1.5 text-[11px] text-danger">{item.error}</p>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}

export function RequirementAnalysisModal({
  open,
  appId,
  entry,
  existingId,
  onClose,
}: {
  open: boolean
  /** 发起分析用的应用 ID（需求列表当前数据源） */
  appId: string
  /** 需求条目（发起 / 重试时携带快照） */
  entry: RequirementsEntry | null
  /** 已有分析会话 ID（列表摘要带入）；null=打开时现场发起 */
  existingId: string | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [analysisId, setAnalysisId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [feedbackText, setFeedbackText] = useState('')
  const [feedbackBusy, setFeedbackBusy] = useState(false)
  const [retryBusy, setRetryBusy] = useState(false)
  const [approveOpen, setApproveOpen] = useState(false)
  const [approveBusy, setApproveBusy] = useState(false)
  const [approveError, setApproveError] = useState<string | null>(null)
  const startedRef = useRef(false)

  // 打开时重置状态（existingId 有值直接进入查看；无值则自动发起）
  useEffect(() => {
    if (!open) return
    setAnalysisId(existingId)
    setActionError(null)
    setFeedbackText('')
    setApproveOpen(false)
    setApproveError(null)
    startedRef.current = false
  }, [open, existingId])

  // 无既有会话：打开即发起分析（admin 已由入口按钮门控；失败在弹窗内提示）
  useEffect(() => {
    if (!open || existingId || analysisId || startedRef.current || !entry || !appId) return
    startedRef.current = true
    setStarting(true)
    setActionError(null)
    api
      .startRequirementAnalysis(appId, entry)
      .then(async (resp) => {
        setAnalysisId(resp.analysis.id)
        await queryClient.invalidateQueries({ queryKey: ['requirement-analyses'] })
      })
      .catch((err) => setActionError(describeError(err)))
      .finally(() => setStarting(false))
  }, [open, existingId, analysisId, entry, appId, queryClient])

  const detail = useQuery({
    queryKey: ['requirement-analysis', analysisId],
    queryFn: () => api.requirementAnalysis(analysisId as string),
    enabled: open && !!analysisId,
    // 分析执行中 3 秒轮询；完成 / 失败后停止
    refetchInterval: (query) =>
      query.state.data?.analysis.status === 'analyzing' ? 3000 : false,
  })

  const session = detail.data?.analysis ?? null
  const versions = session?.versions ?? []
  const latest = [...versions].reverse().find((item) => item.analysis) ?? null
  const result = latest?.analysis ?? null
  const analyzing = session?.status === 'analyzing'
  const failed = session?.status === 'failed'
  const approved = session?.status === 'approved'
  const canApprove = !!session && !!result && !result.degraded && !analyzing && !approved
  const statusMeta = session ? ANALYSIS_STATUS_META[session.status] : undefined

  const submitFeedback = async () => {
    const text = feedbackText.trim()
    if (!analysisId || !text) return
    setFeedbackBusy(true)
    setActionError(null)
    try {
      await api.requirementAnalysisFeedback(analysisId, text)
      setFeedbackText('')
      await queryClient.invalidateQueries({ queryKey: ['requirement-analyses'] })
      await detail.refetch()
    } catch (err) {
      setActionError(describeError(err))
    } finally {
      setFeedbackBusy(false)
    }
  }

  const retryAnalysis = async () => {
    if (!entry || !appId) return
    setRetryBusy(true)
    setActionError(null)
    try {
      // 失败会话走 retry：同输入追加新版本再分析
      await api.startRequirementAnalysis(appId, entry)
      await queryClient.invalidateQueries({ queryKey: ['requirement-analyses'] })
      await detail.refetch()
    } catch (err) {
      setActionError(describeError(err))
    } finally {
      setRetryBusy(false)
    }
  }

  const submitApprove = async () => {
    if (!analysisId) return
    setApproveBusy(true)
    setApproveError(null)
    try {
      await api.approveRequirementAnalysis(analysisId)
      setApproveOpen(false)
      await queryClient.invalidateQueries({ queryKey: ['requirement-analyses'] })
      await detail.refetch()
    } catch (err) {
      setApproveError(describeError(err))
    } finally {
      setApproveBusy(false)
    }
  }

  return (
    <>
      <Modal
        open={open}
        title={`需求智能分析 · #${entry?.id ?? session?.entry_id ?? ''}`}
        size="lg"
        onClose={onClose}
        footer={
          <>
            <button type="button" className={secondaryButtonClass} onClick={onClose}>
              关闭
            </button>
            {session && !approved && (
              <PermissionGate require="admin" fallback={null}>
                <button
                  type="button"
                  className={primaryButtonClass}
                  disabled={!canApprove}
                  title={canApprove ? undefined : '需分析成功完成（未降级、非执行中）后可批准'}
                  onClick={() => {
                    setApproveError(null)
                    setApproveOpen(true)
                  }}
                >
                  同意并进入修复工作流
                </button>
              </PermissionGate>
            )}
          </>
        }
      >
        <div className="space-y-4">
          {entry && (
            <div className="rounded-lg border border-line bg-elevated/40 px-3 py-2.5 text-xs">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="font-medium text-ink">{entry.title}</span>
                {entry.priority && <span className="text-warn">{entry.priority}</span>}
              </div>
              {entry.content && <p className="mt-1 text-muted">{entry.content}</p>}
              {entry.assessment && (
                <p className="mt-1 text-idle">需求方评估意见：{entry.assessment}</p>
              )}
            </div>
          )}

          {starting && (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted">
              <Loader2 size={15} className="animate-spin text-accent" />
              正在提交分析任务…
            </div>
          )}

          {actionError && (
            <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
              {actionError}
            </div>
          )}

          {detail.isError && (
            <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
              {describeError(detail.error)}
            </div>
          )}

          {session && statusMeta && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
              <span className={cn('rounded border px-1.5 py-0.5 text-[10px]', statusMeta.cls)}>
                {statusMeta.label}
              </span>
              <span>当前版本 v{session.current_version}</span>
              <span>服务 {session.service || '—'}</span>
              <span>更新 {fmtDateTime(session.updated_at)}</span>
            </div>
          )}

          {analyzing && (
            <div className="flex items-center gap-2 rounded-lg border border-accent/30 bg-accent/5 px-3 py-2.5 text-xs text-muted">
              <Loader2 size={14} className="shrink-0 animate-spin text-accent" />
              系统正在执行 v{session?.current_version} 分析（LLM 结构化输出 + 代码引用检索），
              完成后自动展示；反馈与批准在此期间不可用。
            </div>
          )}

          {approved && session?.approved && (
            <div className="rounded-xl border border-ok/40 bg-ok/10 p-3.5 text-xs">
              <div className="font-medium text-ok">已批准 · 修复流程已启动</div>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-muted">
                <span>批准版本 v{session.approved.version}</span>
                <span>批准人 {session.approved.actor}</span>
                <span>{fmtDateTime(session.approved.at)}</span>
              </div>
              <Link
                to={`/flows/${encodeURIComponent(session.approved.wf_id)}`}
                className="mt-1.5 inline-flex items-center gap-1 font-mono text-[11px] text-accent hover:underline"
              >
                {session.approved.wf_id} · 查看流程详情 →
              </Link>
            </div>
          )}

          {result && (
            <div>
              <h3 className="mb-2 text-xs font-medium text-ink">
                分析结果（v{latest?.version}
                {latest?.version !== session?.current_version ? '，当前版本分析中，展示上一版' : ''}）
              </h3>
              {result.degraded ? (
                <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2.5 text-xs">
                  <div className="font-medium text-warn">分析未成功完成（已降级留痕，不可批准）</div>
                  <p className="mt-1 text-muted">
                    {latest?.meta?.reason || latest?.error || '未知原因'}
                  </p>
                  <p className="mt-1 text-idle">
                    可提交反馈引导再分析，或点击「重试分析」以相同输入重新执行。
                  </p>
                </div>
              ) : (
                <ResultSection result={result} meta={latest?.meta ?? null} />
              )}
            </div>
          )}

          {failed && !result && (
            <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
              分析执行中断（{latest?.error || '无结果'}），可点击「重试分析」重新执行。
            </div>
          )}

          {versions.length > 0 && (
            <VersionTimeline versions={versions} currentVersion={session?.current_version ?? 1} />
          )}

          {session && !approved && (
            <PermissionGate
              require="admin"
              fallback={
                <p className="text-xs text-idle">
                  需要管理员（admin）权限：可提交优化建议 / 具体要求并批准进入修复流程
                </p>
              }
            >
              <section className="rounded-xl border border-line p-3.5">
                <h3 className="text-xs font-medium text-ink">优化建议 / 具体要求</h3>
                <p className="mt-1 text-[11px] text-idle">
                  提交后系统将带着反馈再次分析并给出新结果（版本递增）；例如「必须保持现有接口兼容」
                  「实现方案改为在 xxx 模块新增函数」
                </p>
                <textarea
                  rows={3}
                  value={feedbackText}
                  onChange={(event) => setFeedbackText(event.target.value)}
                  placeholder="输入优化建议或具体要求…"
                  className={cn(fieldClass, 'mt-2')}
                  disabled={analyzing}
                />
                <div className="mt-2 flex flex-wrap justify-end gap-2">
                  {failed && (
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={retryBusy}
                      onClick={retryAnalysis}
                    >
                      {retryBusy ? '提交中…' : '重试分析'}
                    </button>
                  )}
                  <button
                    type="button"
                    className={primaryButtonClass}
                    disabled={feedbackBusy || analyzing || !feedbackText.trim()}
                    onClick={submitFeedback}
                  >
                    {feedbackBusy ? '提交中…' : '提交反馈并再分析'}
                  </button>
                </div>
              </section>
            </PermissionGate>
          )}
        </div>
      </Modal>

      <ConfirmDialog
        open={approveOpen}
        title="同意分析结果并进入修复工作流？"
        confirmLabel="同意并启动修复"
        busy={approveBusy}
        error={approveError}
        onConfirm={submitApprove}
        onClose={() => {
          if (!approveBusy) setApproveOpen(false)
        }}
      >
        <div className="space-y-2 text-xs">
          <p>
            将按已批准的实现方案（v{latest?.version ?? session?.current_version}
            ）启动修复流程，自动完成：修复生成 → 沙箱测试 → 人工审批（闸门 2）→ 公告倒计时 →
            金丝雀发布。
          </p>
          <p className="text-idle">
            流程与告警修复同构：审批 / 发布指令在「审批中心」「流程详情」操作；发布窗口内可回滚。
          </p>
        </div>
      </ConfirmDialog>
    </>
  )
}
