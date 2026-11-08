/** 审批中心：待办审批队列（2 秒轮询，按截止时间升序，一级 / 二级区分）（设计方案 7.4）。 */

import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, describeError } from '../lib/api'
import { cn, diffStat, shortHash } from '../lib/format'
import { ApprovalDialog, useApprovalDialog } from '../components/ApprovalDialog'
import { ActionButton } from '../components/ActionButton'
import { CopyableId } from '../components/CopyableId'
import { CountdownText } from '../components/CountdownText'
import { EmptyState } from '../components/EmptyState'
import { PermissionGate } from '../components/PermissionGate'
import { StageBadge } from '../components/StageBadge'
import { Toast } from '../components/Toast'
import type { FlowItem } from '../lib/types'

type ApprovalHandle = ReturnType<typeof useApprovalDialog>

function openDecision(
  item: FlowItem,
  second: boolean,
  decision: 'approve' | 'reject',
  approval: ApprovalHandle,
) {
  const approve = decision === 'approve'
  const stat = diffStat(item.patch?.diff)
  approval.open({
    wfId: item.wf_id,
    gate: second ? 'second_approval' : 'approval',
    decision,
    binding: item.evidence_binding ?? null,
    caseId: item.case_id || item.alert?.alert_id || '',
    summary: (
      <div className="space-y-2.5 text-sm">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <div>服务：{item.alert?.service ?? '—'}</div>
          <div>
            置信度：
            <span className="font-mono text-ink">
              {item.confidence != null ? item.confidence.toFixed(2) : '—'}
            </span>
          </div>
          <div className="col-span-2">
            工作流：<span className="font-mono text-xs">{item.wf_id}</span>
          </div>
          <div className="col-span-2">
            告警：<span className="font-mono text-xs">{item.alert?.alert_id ?? '—'}</span>
          </div>
        </div>

        {item.patch ? (
          <div className="rounded-lg border border-line bg-canvas p-2.5">
            <div className="text-xs">
              补丁改动：<span className="text-ink">{stat.files} 个文件</span>
              <span className="ml-2 font-mono text-ok">+{stat.added}</span>
              <span className="ml-1 font-mono text-danger">-{stat.removed}</span>
            </div>
            <div className="mt-1 truncate font-mono text-[11px] text-muted" title={item.patch.files.join('、')}>
              {item.patch.files.join('、')}
            </div>
            {item.patch.risk && <div className="mt-1 text-[11px] text-idle">风险：{item.patch.risk}</div>}
          </div>
        ) : (
          <div className="text-xs text-idle">（未取到补丁信息）</div>
        )}

        {item.test_report && (
          <div
            className={cn(
              'rounded-lg border p-2.5 text-xs',
              item.test_report.passed
                ? 'border-ok/30 bg-ok/10 text-ok'
                : 'border-danger/30 bg-danger/10 text-danger',
            )}
          >
            测试结论：{item.test_report.passed ? '通过' : '未通过'} · {item.test_report.unit_tests || '—'}
            {item.test_report.sast && (
              <div className="mt-0.5 text-[11px] text-idle">SAST：{item.test_report.sast}</div>
            )}
          </div>
        )}

        {item.requested_by && (
          <div className="text-[11px] text-idle">
            发起人：{item.requested_by}（职责分离：发起人不得审批自己的变更）
          </div>
        )}
        <div className="text-ink">
          决策：{approve ? '批准（进入公告窗口）' : '驳回（转人工处理）'}
        </div>
        <Link
          to={`/flows/${encodeURIComponent(item.wf_id)}`}
          className="inline-block text-xs text-accent hover:underline"
        >
          查看完整详情（diff / 测试报告 / 事件时间线）→
        </Link>
      </div>
    ),
  })
}

export function Approvals() {
  const approval = useApprovalDialog()
  const { data, isError, error } = useQuery({
    queryKey: ['approvals'],
    queryFn: api.approvals,
    refetchInterval: 2000,
  })

  return (
    <div>
      <h1 className="text-lg font-medium">审批中心</h1>
      <p className="mt-1 text-xs text-muted">待办审批实时队列（2 秒自动刷新，按截止时间升序）</p>

      {isError && (
        <div className="mt-5">
          <EmptyState title="无法加载审批列表" hint={describeError(error)} />
        </div>
      )}

      {data &&
        (data.items.length ? (
          <div className="mt-5 space-y-3">
            {data.items.map((item) => {
              const second = item.pending === 'second_approval'
              return (
                <div key={item.wf_id} className="rounded-xl border border-line bg-panel p-4">
                  <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-3">
                        <span className="text-sm font-medium">{item.alert?.service ?? '未知服务'}</span>
                        <StageBadge stage={item.stage} />
                        <span
                          className={cn(
                            'rounded border px-1.5 py-0.5 text-[10px]',
                            second ? 'border-danger/40 text-danger' : 'border-warn/40 text-warn',
                          )}
                        >
                          {second ? '二级审批' : '一级审批'}
                        </span>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
                        <span>
                          告警 <CopyableId value={item.alert?.alert_id ?? '—'} />
                        </span>
                        <span>
                          工作流 <CopyableId value={item.wf_id} />
                        </span>
                        {item.patch_id && (
                          <span>
                            补丁 <CopyableId value={item.patch_id} />
                          </span>
                        )}
                        <span>
                          置信度{' '}
                          <span className="font-mono text-ink">
                            {item.confidence != null ? item.confidence.toFixed(2) : '—'}
                          </span>
                        </span>
                        {item.patch && (
                          <span>
                            改动{' '}
                            <span className="font-mono text-ok">+{diffStat(item.patch.diff).added}</span>{' '}
                            <span className="font-mono text-danger">-{diffStat(item.patch.diff).removed}</span>
                          </span>
                        )}
                        {item.test_report && (
                          <span className={item.test_report.passed ? 'text-ok' : 'text-danger'}>
                            测试 {item.test_report.passed ? '通过' : '未通过'}
                            {item.test_report.unit_tests ? ` · ${item.test_report.unit_tests}` : ''}
                          </span>
                        )}
                        {item.requested_by && <span>发起人 {item.requested_by}</span>}
                        {item.evidence_binding ? (
                          <span className="font-mono">
                            diff {shortHash(item.evidence_binding.diff_hash)} · run{' '}
                            {shortHash(item.evidence_binding.run_id, 16)}
                          </span>
                        ) : (
                          <span className="text-idle">旧协议流程（无证据绑定）</span>
                        )}
                      </div>
                      {item.approval_error && (
                        <div className="mt-2 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
                          上次审批未生效（{item.approval_error.code}）：{item.approval_error.reason}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-4">
                      <div className="text-right">
                        <div className="text-[10px] text-muted">剩余时间</div>
                        <CountdownText at={item.deadline?.at} className="text-lg font-semibold" />
                      </div>
                      <div className="flex gap-2">
                        <PermissionGate require="operator">
                          <ActionButton
                            tone="accent"
                            onClick={() => openDecision(item, second, 'approve', approval)}
                          >
                            批准
                          </ActionButton>
                          <ActionButton
                            tone="danger"
                            onClick={() => openDecision(item, second, 'reject', approval)}
                          >
                            驳回
                          </ActionButton>
                        </PermissionGate>
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="mt-5">
            <EmptyState title="当前没有待办审批" hint="进入待审批阶段的流程会实时出现在这里" />
          </div>
        ))}

      <ApprovalDialog
        request={approval.request}
        onClose={approval.close}
        onDone={approval.done}
      />
      <Toast message={approval.toast} />
    </div>
  )
}
