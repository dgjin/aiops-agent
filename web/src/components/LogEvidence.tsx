/** 日志证据（转人工核对）：原始日志 + Drain3 模板聚类，按 workflow_id 读取归档。
 *
 * - 流程详情「日志证据」Tab 与转人工待办弹窗共用；
 * - 证据在 TRIAGING 阶段归档（早于 LLM 根因），进程重启 / 换实例后仍可查；
 * - 归档只保留末尾固定行数，截断时明确提示「非全量」，避免核对时误判；
 * - Loki 不可用（loki_available=false）时醒目提示——升级原因常是「无日志证据」。
 */

import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Search, X } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, downloadJson, fmtDateTime } from '../lib/format'
import { CopyableId } from './CopyableId'
import { EmptyState } from './EmptyState'

/** 摘要小格（标签 + 值）。 */
function Meta({
  label,
  children,
  tone,
}: {
  label: string
  children: ReactNode
  tone?: 'danger' | 'warn'
}) {
  return (
    <div className="text-xs">
      <div className="text-idle">{label}</div>
      <div
        className={cn(
          'mt-0.5',
          tone === 'danger' ? 'text-danger' : tone === 'warn' ? 'text-warn' : 'text-ink',
        )}
      >
        {children}
      </div>
    </div>
  )
}

/** 错误级别日志行着色（核对时快速定位真正的报错行）。 */
const isErrorLine = (line: string): boolean =>
  /\b(error|exception|fatal|panic|traceback|failed)\b/i.test(line)

export function LogEvidence({ wfId }: { wfId: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['flow-evidence', wfId],
    queryFn: () => api.flowEvidence(wfId),
    staleTime: 60_000,
    retry: false,
  })
  const [query, setQuery] = useState('')
  const evidence = data?.evidence

  /** 保留原始行号（过滤后仍能对应归档中的位置）。 */
  const lines = useMemo(() => {
    const all = (evidence?.lines ?? []).map((line, index) => ({ line, index }))
    const q = query.trim().toLowerCase()
    return q ? all.filter((item) => item.line.toLowerCase().includes(q)) : all
  }, [evidence, query])

  if (isLoading) return <div className="text-sm text-muted">加载日志证据…</div>
  if (isError || !evidence) {
    return (
      <EmptyState
        title="无可核对日志"
        hint={isError ? describeError(error) : '该流程没有归档日志证据'}
      />
    )
  }

  return (
    <div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
        <Meta label="采集时间">{fmtDateTime(evidence.collected_at)}</Meta>
        <Meta
          label="Loki"
          tone={evidence.loki_available ? undefined : 'danger'}
        >
          {evidence.loki_available ? '可用' : '不可用（当时无日志）'}
        </Meta>
        <Meta label="日志行（归档 / 原始）" tone={evidence.truncated ? 'warn' : undefined}>
          {evidence.stored_lines} / {evidence.total_lines}
          {evidence.truncated && '（已截断，保留末尾）'}
        </Meta>
        <Meta label="模板 / 调用链">
          {evidence.templates.length} 个模板 · {evidence.trace_ids.length} 个 trace
        </Meta>
      </div>

      {evidence.trace_ids.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-xs text-idle">关联 trace_id</div>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {evidence.trace_ids.map((traceId) => (
              <CopyableId key={traceId} value={traceId} />
            ))}
          </div>
        </div>
      )}

      {evidence.templates.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-xs text-muted">
            模板聚类（Drain3 降噪，按出现次数降序）
          </div>
          <div className="overflow-hidden rounded-lg border border-line">
            <table className="w-full text-xs">
              <thead className="bg-elevated text-muted">
                <tr>
                  <th className="w-16 px-3 py-1.5 text-left font-normal">次数</th>
                  <th className="px-3 py-1.5 text-left font-normal">模板</th>
                </tr>
              </thead>
              <tbody>
                {evidence.templates.map((template) => (
                  <tr key={template.id} className="border-t border-line/60">
                    <td className="whitespace-nowrap px-3 py-1.5 font-mono text-warn">
                      {template.count}x
                    </td>
                    <td className="break-all px-3 py-1.5 font-mono text-ink/80">
                      {template.pattern}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="mt-4">
        <div className="flex flex-wrap items-center gap-2">
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
              placeholder="在日志中搜索（错误码 / 关键字 / trace_id）"
              className="w-80 rounded-lg border border-line bg-panel py-1.5 pl-8 pr-7 text-xs text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none"
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
          <span className="text-xs text-muted">
            显示 <span className="text-ink">{lines.length}</span> / {evidence.lines.length} 行
          </span>
          <button
            type="button"
            onClick={() => downloadJson(`${wfId}-evidence.json`, evidence)}
            className="ml-auto inline-flex items-center gap-1 rounded-lg border border-line px-2.5 py-1.5 text-xs text-muted transition-colors hover:bg-elevated hover:text-ink"
          >
            <Download size={12} />
            导出证据
          </button>
        </div>
        <div className="mt-2 max-h-80 overflow-auto rounded-lg border border-line bg-canvas px-3 py-2 font-mono text-[11px] leading-relaxed">
          {lines.length === 0 ? (
            <div className="py-2 text-idle">没有匹配的日志行</div>
          ) : (
            lines.map(({ line, index }) => (
              <div key={index} className="flex gap-3">
                <span className="w-10 shrink-0 select-none text-right text-idle">{index + 1}</span>
                <span
                  className={cn(
                    'whitespace-pre-wrap break-all',
                    isErrorLine(line) ? 'text-danger/90' : 'text-ink/75',
                  )}
                >
                  {line}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
