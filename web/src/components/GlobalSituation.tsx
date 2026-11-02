/** 全局态势（原流程看板内容）：计数卡 + 运行中流程墙 + 近 7 日终态分布 + 运营度量。
 *
 * 2026-10 UI 重构：从独立首页（Dashboard）下沉为「系统总览」页的折叠区——
 * 流程视角的既有能力完整保留，但不再占据首页第一屏（首页主语切换为被监控系统）。
 *
 * 2026-11 M5：运营度量扩展为值班看板——分维度完成率、证据与采集、沙箱质量、
 * 待办 SLA、值班告警（Runbook 抽屉）。空样本一律显示「暂无数据」，不渲染 0%。
 */

import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { EmptyState } from './EmptyState'
import { FlowCard } from './FlowCard'
import { Donut } from './charts/Donut'
import type { OpsMetricsResp, OpsRateBlock, OverviewCounts } from '../lib/types'

const STAT_CARDS: Array<{ key: keyof OverviewCounts; label: string; tone: string }> = [
  { key: 'running', label: '运行中', tone: 'text-info' },
  { key: 'wait_approval', label: '待审批', tone: 'text-warn' },
  { key: 'notifying', label: '公告倒计时', tone: 'text-warn' },
  { key: 'today_done', label: '今日完成', tone: 'text-ok' },
  { key: 'today_escalated', label: '今日转人工', tone: 'text-danger' },
  { key: 'today_failed', label: '今日失败', tone: 'text-danger' },
]

const KIND_LABELS: Array<[string, string]> = [
  ['alert', '告警修复'],
  ['requirement', '需求修复'],
]

/** 秒数说成人话（MTTR / MTTA / SLA 展示）。 */
function fmtDuration(seconds: number | null): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = seconds / 60
  if (minutes < 60) return `${minutes.toFixed(1)}m`
  return `${(minutes / 60).toFixed(1)}h`
}

function fmtRate(rate: number | null): string {
  return rate == null ? '暂无数据' : `${(rate * 100).toFixed(1)}%`
}

/** 比率：空样本用弱化文案，避免被读成 0%。 */
function Rate({ rate }: { rate: number | null }) {
  if (rate == null) return <span className="text-sm font-normal text-idle">暂无数据</span>
  return <>{fmtRate(rate)}</>
}

function MetricCard({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: ReactNode
  hint: string
  tone: string
}) {
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className={cn('mt-2 font-mono text-2xl font-semibold tabular-nums', tone)}>{value}</div>
      <div className="mt-1 text-[11px] text-idle">{hint}</div>
    </div>
  )
}

function Panel({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <div className="text-xs text-muted">{title}</div>
        {hint && <div className="text-[11px] text-idle">{hint}</div>}
      </div>
      {children}
    </div>
  )
}

function Row({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 text-xs">
      <span className="text-muted">
        {label}
        {hint && <span className="ml-1 text-[11px] text-idle">· {hint}</span>}
      </span>
      <span className="font-mono tabular-nums text-ink">{value}</span>
    </div>
  )
}

const LEVEL_STYLE: Record<string, string> = {
  danger: 'border-danger text-danger',
  warn: 'border-warn text-warn',
  unknown: 'border-line text-idle',
}
const LEVEL_LABEL: Record<string, string> = { danger: '严重', warn: '关注', unknown: '数据不足' }

/** Runbook 抽屉：按告警 code 取 docs 小节（Bearer 鉴权，不能直接用 <a> 打开仓库文件）。 */
function RunbookDrawer({ code, onClose }: { code: string; onClose: () => void }) {
  const { data, isError, error } = useQuery({
    queryKey: ['ops-runbook', code],
    queryFn: () => api.opsRunbook(code),
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <aside
        className="h-full w-full max-w-2xl overflow-y-auto border-l border-line bg-panel p-5"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <div className="text-sm font-medium text-ink">{data?.title || `Runbook · ${code}`}</div>
            <div className="mt-1 font-mono text-[11px] text-idle">
              {data?.path || 'docs/AIOps 值班 Runbook.md'}#{code}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-line px-2 py-1 text-xs text-muted hover:text-ink"
          >
            关闭
          </button>
        </div>
        {isError ? (
          <div className="text-xs text-danger">{describeError(error)}</div>
        ) : !data ? (
          <div className="text-xs text-muted">加载中…</div>
        ) : data.known ? (
          <pre className="whitespace-pre-wrap font-sans text-xs leading-6 text-ink">{data.body}</pre>
        ) : (
          <div className="text-xs text-warn">
            未找到该告警的 Runbook 小节（或文档暂不可读）。请检查 `docs/AIOps 值班 Runbook.md`
            是否包含 `## {code}` 标题。
          </div>
        )}
      </aside>
    </div>
  )
}

function KindBlock({ label, block }: { label: string; block?: OpsRateBlock }) {
  if (!block || !block.closed) {
    return (
      <div className="rounded-lg border border-line p-3">
        <div className="text-xs text-muted">{label}</div>
        <div className="mt-1 text-sm text-idle">暂无数据</div>
      </div>
    )
  }
  return (
    <div className="rounded-lg border border-line p-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-xs text-muted">{label}</div>
        <div className="text-[11px] text-idle">
          终态 {block.closed} · 完成 {block.done}
        </div>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <div>
          <div className="text-[11px] text-idle">自动完成率</div>
          <div className="font-mono text-sm tabular-nums text-ok">
            <Rate rate={block.auto_fix_rate} />
          </div>
        </div>
        <div>
          <div className="text-[11px] text-idle">人工干预率</div>
          <div className="font-mono text-sm tabular-nums text-warn">
            <Rate rate={block.human_intervention_rate} />
          </div>
        </div>
      </div>
      <div className="mt-1 text-[11px] text-idle">
        干预样本 {block.human_intervention_sample} · MTTR {fmtDuration(block.mttr.avg_seconds)}
      </div>
    </div>
  )
}

/** 运营度量区块（M5）：值班告警置顶，其后为分维度 / 证据 / 沙箱 / 待办。 */
function OpsMetricsSection({ metrics }: { metrics: OpsMetricsResp }) {
  const [runbook, setRunbook] = useState<string | null>(null)
  const { mttr, mtta, evidence, logship, sandbox, escalation_detail: detail, rollback } = metrics
  const activeGates = metrics.gate_breakdown.filter((row) => row.count > 0)
  const sourceLabel = metrics.source === 'warehouse' ? 'SQL 镜像' : 'Temporal 读侧'

  return (
    <section>
      <h2 className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm font-medium text-muted">
        <span>
          运营度量（近 {metrics.period_days} 日 · 共 {metrics.closed_total} 个终态流程）
        </span>
        <span className="text-[11px] font-normal text-idle">
          数据源：{sourceLabel}
          {metrics.mirror_updated_at ? ` · 镜像同步 ${fmtDateTime(metrics.mirror_updated_at)}` : ''}
        </span>
      </h2>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <MetricCard
          label="MTTR（DONE 均值）"
          value={fmtDuration(mttr.avg_seconds)}
          hint={`P95 ${fmtDuration(mttr.p95_seconds)} · 样本 ${mttr.sample}`}
          tone="text-info"
        />
        <MetricCard
          label="MTTA（首次人工动作）"
          value={fmtDuration(mtta.avg_seconds)}
          hint={`P95 ${fmtDuration(mtta.p95_seconds)} · 样本 ${mtta.sample}`}
          tone="text-info"
        />
        <MetricCard
          label="自动完成率"
          value={<Rate rate={metrics.auto_fix_rate} />}
          hint={`DONE ${metrics.auto_fix_sample}/${metrics.closed_total}`}
          tone="text-ok"
        />
        <MetricCard
          label="人工干预率"
          value={<Rate rate={metrics.human_intervention_rate} />}
          hint={`${metrics.human_intervention_sample}/${metrics.closed_total} 被人工写操作触及`}
          tone="text-warn"
        />
        <MetricCard
          label="闸门拦截率"
          value={<Rate rate={metrics.gate_block_rate} />}
          hint={`${metrics.gate_block_sample}/${metrics.closed_total} 被闸门拦下`}
          tone="text-danger"
        />
        <MetricCard
          label="回滚率"
          value={<Rate rate={rollback.rate} />}
          hint={`劣化 ${rollback.sample} / 进入金丝雀 ${rollback.released}`}
          tone={rollback.sample ? 'text-danger' : 'text-muted'}
        />
      </div>

      <div className="mt-3 space-y-3">
        <Panel
          title="值班告警"
          hint={metrics.duty_alerts.length ? `${metrics.duty_alerts.length} 条触发中` : '窗口内无触发'}
        >
          {metrics.duty_alerts.length ? (
            <ul className="space-y-2">
              {metrics.duty_alerts.map((alert) => (
                <li
                  key={alert.code}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
                >
                  <span
                    className={cn(
                      'rounded border px-1.5 py-0.5 text-[11px]',
                      LEVEL_STYLE[alert.level] || LEVEL_STYLE.unknown,
                    )}
                  >
                    {LEVEL_LABEL[alert.level] || alert.level}
                  </span>
                  <span className="text-ink">{alert.label}</span>
                  <span className="font-mono text-muted">
                    {alert.value == null ? '—' : alert.value} / {alert.threshold}
                  </span>
                  <span className="text-idle">{alert.detail}</span>
                  <button
                    type="button"
                    onClick={() => setRunbook(alert.code)}
                    className="ml-auto text-info hover:underline"
                  >
                    Runbook
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-xs text-idle">窗口内没有触发中的值班告警</div>
          )}
          {metrics.degraded.length > 0 && (
            <div className="mt-2 border-t border-line pt-2 text-[11px] text-warn">
              数据源降级：{metrics.degraded.join('、')}（对应分量按空窗口展示，告警可能不全）
            </div>
          )}
        </Panel>

        <div className="grid gap-3 lg:grid-cols-2">
          <Panel title="分来源与分风险" hint={`分母各自独立 · 共 ${metrics.closed_total} 条终态`}>
            <div className="grid grid-cols-2 gap-2">
              {KIND_LABELS.map(([key, label]) => (
                <KindBlock key={key} label={label} block={metrics.by_kind[key]} />
              ))}
            </div>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-[11px] text-idle">
                    <th className="py-1 pr-2 font-normal">风险</th>
                    <th className="py-1 pr-2 text-right font-normal">终态</th>
                    <th className="py-1 pr-2 text-right font-normal">完成</th>
                    <th className="py-1 text-right font-normal">自动完成率</th>
                  </tr>
                </thead>
                <tbody>
                  {metrics.by_risk.map((row) => (
                    <tr key={row.key} className="border-t border-line/60">
                      <td className="py-1 pr-2 text-muted">{row.label}</td>
                      <td className="py-1 pr-2 text-right font-mono tabular-nums">{row.closed}</td>
                      <td className="py-1 pr-2 text-right font-mono tabular-nums">{row.done}</td>
                      <td className="py-1 text-right font-mono tabular-nums">
                        {fmtRate(row.auto_fix_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel
            title="证据与采集"
            hint={`证据样本 ${evidence.sample} · 心跳服务 ${logship.services}`}
          >
            {evidence.available ? (
              <>
                <Row
                  label="证据充分率"
                  value={fmtRate(evidence.ok_rate)}
                  hint={`样本 ${evidence.sample}`}
                />
                <Row label="观测面异常占比" value={fmtRate(evidence.observability_share)} />
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {evidence.by_status
                    .filter((row) => row.count > 0)
                    .map((row) => (
                      <span
                        key={row.key}
                        className={cn(
                          'rounded border px-1.5 py-0.5 text-[11px]',
                          row.observability ? 'border-line text-warn' : 'border-line text-muted',
                        )}
                        title={row.observability ? '观测面异常，不计入模型低置信' : undefined}
                      >
                        {row.label} {row.count}
                      </span>
                    ))}
                </div>
              </>
            ) : (
              <div className="text-xs text-idle">暂无证据归档数据</div>
            )}

            <div className="mt-3 border-t border-line pt-2">
              {logship.available ? (
                <>
                  <Row
                    label="采集覆盖率"
                    value={logship.coverage ? fmtRate(logship.coverage.rate) : '暂无数据'}
                    hint={
                      logship.coverage
                        ? `${logship.coverage.covered}/${logship.coverage.total} 服务`
                        : undefined
                    }
                  />
                  <Row
                    label="心跳新鲜度 P95"
                    value={fmtDuration(logship.freshness_p95_seconds)}
                    hint={`不健康 ${logship.unhealthy}/${logship.services}`}
                  />
                  {logship.unhealthy_services.length > 0 && (
                    <div className="mt-1 text-[11px] text-warn">
                      不健康：{logship.unhealthy_services.join('、')}
                    </div>
                  )}
                </>
              ) : (
                <div className="text-xs text-idle">暂无采集心跳数据（不能判定为正常）</div>
              )}
            </div>
          </Panel>
        </div>

        <div className="grid gap-3 lg:grid-cols-2">
          <Panel title="沙箱与补丁质量" hint={`已归档测试报告 ${sandbox.sample} 份`}>
            {sandbox.available ? (
              <>
                <Row
                  label="真实沙箱通过率"
                  value={fmtRate(sandbox.real_pass_rate)}
                  hint={`仅 UNIT_TEST · 样本 ${sandbox.real_sample}`}
                />
                <Row
                  label="静态 / 契约（单列，不计入真实通过率）"
                  value={`${sandbox.static_count} / ${sandbox.contract_count}`}
                />
                <Row
                  label="补丁结构可应用率"
                  value={fmtRate(sandbox.patch_structural_rate)}
                  hint={`样本 ${sandbox.patch_structural_sample}（真实 git apply 由发布阶段判定）`}
                />
                <Row
                  label="人工改判率"
                  value={fmtRate(sandbox.override_rate)}
                  hint={`样本 ${sandbox.override_sample}`}
                />
                <Row
                  label="高置信误放行"
                  value={
                    <span className={sandbox.high_confidence_false_pass ? 'text-danger' : ''}>
                      {sandbox.high_confidence_false_pass}
                    </span>
                  }
                  hint="组合判据放行 + 置信度 ≥0.8 + 最终转人工"
                />
              </>
            ) : (
              <div className="text-xs text-idle">暂无沙箱测试报告</div>
            )}
          </Panel>

          <Panel
            title="待办与知识"
            hint={`SLA ${detail.sla_minutes} 分钟 · 开放样本 ${detail.open_sample}`}
          >
            {detail.available ? (
              <>
                <Row
                  label="开放时长均值 / P95"
                  value={`${fmtDuration(detail.open_avg_seconds)} / ${fmtDuration(detail.open_p95_seconds)}`}
                />
                <Row
                  label="处置时长均值（已关闭）"
                  value={fmtDuration(detail.handle_avg_seconds)}
                />
                <Row
                  label="SLA 超时"
                  value={
                    <span className={detail.sla_breach ? 'text-danger' : ''}>
                      {detail.sla_breach == null ? '暂无数据' : detail.sla_breach}
                    </span>
                  }
                />
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {detail.close_reasons.length ? (
                    detail.close_reasons.map((row) => (
                      <span
                        key={row.key}
                        className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted"
                      >
                        {row.label} {row.count}
                      </span>
                    ))
                  ) : (
                    <span className="text-[11px] text-idle">窗口内无关闭记录</span>
                  )}
                </div>
                <div className="mt-2 border-t border-line pt-2 text-[11px] text-idle">
                  知识候选：待审核 {detail.knowledge.draft} · 已入库 {detail.knowledge.approved} ·
                  已拒绝 {detail.knowledge.rejected} · 采用情况{' '}
                  {detail.knowledge.adopted == null ? '暂无数据' : detail.knowledge.adopted}
                </div>
              </>
            ) : (
              <div className="text-xs text-idle">暂无待办数据</div>
            )}
            {metrics.escalations && (
              <div className="mt-2 text-[11px] text-idle">
                待办现状：待指派 {metrics.escalations.open} · 已指派 {metrics.escalations.assigned} ·
                已关闭 {metrics.escalations.closed}
              </div>
            )}
            <div className="mt-2 text-[11px] text-idle">
              模型成本：{metrics.cost.model_cost_usd == null ? '暂无数据' : `$${metrics.cost.model_cost_usd}`}（
              {metrics.cost.note}）
            </div>
          </Panel>
        </div>

        <div className="grid gap-3 lg:grid-cols-3">
          <Panel title="分服务（前 10）" hint={`${metrics.by_service.length} 个服务`}>
            {metrics.by_service.length ? (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-[11px] text-idle">
                      <th className="py-1 pr-2 font-normal">服务</th>
                      <th className="py-1 pr-2 text-right font-normal">终态</th>
                      <th className="py-1 pr-2 text-right font-normal">完成</th>
                      <th className="py-1 pr-2 text-right font-normal">自动</th>
                      <th className="py-1 text-right font-normal">干预</th>
                    </tr>
                  </thead>
                  <tbody>
                    {metrics.by_service.map((row) => (
                      <tr key={row.service} className="border-t border-line/60">
                        <td className="py-1 pr-2 text-muted">{row.service}</td>
                        <td className="py-1 pr-2 text-right font-mono tabular-nums">{row.closed}</td>
                        <td className="py-1 pr-2 text-right font-mono tabular-nums">{row.done}</td>
                        <td className="py-1 pr-2 text-right font-mono tabular-nums">
                          {fmtRate(row.auto_fix_rate)}
                        </td>
                        <td className="py-1 text-right font-mono tabular-nums">
                          {fmtRate(row.human_intervention_rate)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="text-xs text-idle">窗口内无终态流程</div>
            )}
          </Panel>

          <Panel title="拦截分档" hint={`近 ${metrics.period_days} 日`}>
            {activeGates.length ? (
              <div className="space-y-1">
                {activeGates.map((row) => (
                  <div key={row.key} className="flex items-center justify-between gap-3 text-xs">
                    <span className="text-muted">{row.label}</span>
                    <span className="font-mono tabular-nums text-ink">{row.count}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-idle">窗口内无闸门拦截记录</div>
            )}
          </Panel>
        </div>
      </div>

      {runbook && <RunbookDrawer code={runbook} onClose={() => setRunbook(null)} />}
    </section>
  )
}

export function GlobalSituation() {
  const navigate = useNavigate()
  const { data, isError, error } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 5000,
  })
  const metrics = useQuery({
    queryKey: ['ops-metrics'],
    queryFn: () => api.opsMetrics(7),
    refetchInterval: 30000,
  })

  if (isError) {
    return <EmptyState title="无法加载态势数据" hint={describeError(error)} />
  }
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  return (
    <div className="space-y-7">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        {STAT_CARDS.map((card) => (
          <div key={card.key} className="rounded-xl border border-line bg-panel p-4">
            <div className="text-xs text-muted">{card.label}</div>
            <div className={cn('mt-2 font-mono text-2xl font-semibold tabular-nums', card.tone)}>
              {data.counts[card.key]}
            </div>
          </div>
        ))}
      </div>

      <section>
        <h2 className="mb-3 text-sm font-medium text-muted">运行中流程</h2>
        {data.running.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {data.running.map((item) => (
              <FlowCard key={item.wf_id} item={item} />
            ))}
          </div>
        ) : (
          <EmptyState title="当前没有运行中的流程" hint="新告警接入后会自动出现在这里" />
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium text-muted">近 7 日终态分布</h2>
        <div className="rounded-xl border border-line bg-panel p-4">
          <Donut
            centerLabel="共"
            segments={[
              { key: 'DONE', label: '已完成', value: data.terminal_dist_7d.DONE ?? 0, tone: 'text-ok' },
              { key: 'SHADOWED', label: '已建议（影子档）', value: data.terminal_dist_7d.SHADOWED ?? 0, tone: 'text-idle' },
              { key: 'ESCALATED', label: '已转人工', value: data.terminal_dist_7d.ESCALATED ?? 0, tone: 'text-danger' },
              { key: 'FAILED', label: '执行失败', value: data.terminal_dist_7d.FAILED ?? 0, tone: 'text-warn' },
              { key: 'CANCELLED', label: '已取消', value: data.terminal_dist_7d.CANCELLED ?? 0, tone: 'text-idle' },
            ]}
            onSelect={(key) => navigate(`/flows?stage=${encodeURIComponent(key)}`)}
          />
        </div>
      </section>

      {metrics.data && <OpsMetricsSection metrics={metrics.data} />}
    </div>
  )
}
