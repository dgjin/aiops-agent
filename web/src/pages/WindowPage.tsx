/** 发布窗口：公告倒计时 + 立即发布 / 取消公告 / 排队补丁（设计方案 7.4）。 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, describeError } from '../lib/api'
import { useWriteAction } from '../lib/actions'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CopyableId } from '../components/CopyableId'
import { CountdownText } from '../components/CountdownText'
import { EmptyState } from '../components/EmptyState'
import { PermissionGate } from '../components/PermissionGate'
import { QueuePatchDialog } from '../components/QueuePatchDialog'
import { StageBadge } from '../components/StageBadge'
import { Toast } from '../components/Toast'
import { ReleasePipeline } from '../components/charts/ReleasePipeline'
import type { FlowItem } from '../lib/types'

type WriteHandle = ReturnType<typeof useWriteAction>

function openDeploy(item: FlowItem, write: WriteHandle) {
  write.open({
    title: '确认立即发布？',
    tone: 'danger',
    confirmLabel: '立即发布',
    detail: (
      <div className="space-y-1.5">
        <div>
          工作流：<span className="font-mono text-xs">{item.wf_id}</span>
        </div>
        <div>服务：{item.alert?.service ?? '—'}</div>
        {item.version && <div>版本：{item.version}</div>}
        <div className="text-ink">决策：跳过剩余公告倒计时，直接进入金丝雀发布</div>
      </div>
    ),
    run: () => api.deployCommand(item.wf_id, 'deploy_now'),
    success: '已下发 deploy-now，流程将进入金丝雀发布',
  })
}

function openCancel(item: FlowItem, write: WriteHandle) {
  write.open({
    title: '确认取消本次发布？',
    tone: 'danger',
    confirmLabel: '取消公告',
    detail: (
      <div className="space-y-1.5">
        <div>
          工作流：<span className="font-mono text-xs">{item.wf_id}</span>
        </div>
        <div>服务：{item.alert?.service ?? '—'}</div>
        <div className="text-ink">决策：中止公告窗口，本次修复不发布上线</div>
      </div>
    ),
    run: () => api.deployCommand(item.wf_id, 'cancel'),
    success: '已下发 cancel 命令',
  })
}

export function WindowPage() {
  const write = useWriteAction()
  const [queueFor, setQueueFor] = useState<FlowItem | null>(null)
  const { data, isError, error } = useQuery({
    queryKey: ['window'],
    queryFn: api.window,
    refetchInterval: 2000,
  })

  return (
    <div>
      <h1 className="text-lg font-medium">发布窗口</h1>
      <p className="mt-1 text-xs text-muted">公告倒计时与窗口操作（2 秒自动刷新）</p>

      {isError && (
        <div className="mt-5">
          <EmptyState title="无法加载发布窗口" hint={describeError(error)} />
        </div>
      )}

      {data &&
        (data.items.length ? (
          <div className="mt-5 space-y-4">
            {data.items.map((item) => (
              <div key={item.wf_id} className="rounded-xl border border-line bg-panel p-5">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="text-sm font-medium">{item.alert?.service ?? '未知服务'}</span>
                      <StageBadge stage={item.stage} />
                      {item.version && (
                        <span className="font-mono text-xs text-accent">{item.version}</span>
                      )}
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted">
                      <span>
                        告警 <CopyableId value={item.alert?.alert_id ?? '—'} />
                      </span>
                      {item.patch_id && (
                        <span>
                          补丁 <CopyableId value={item.patch_id} />
                        </span>
                      )}
                      <span>
                        工作流 <CopyableId value={item.wf_id} />
                      </span>
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="text-xs text-muted">公告剩余</div>
                    <CountdownText at={item.deadline?.at} className="text-4xl font-semibold" />
                  </div>
                </div>
                <div className="mt-4 border-t border-line pt-4">
                  <ReleasePipeline
                    stage={item.stage}
                    deadlineAt={item.deadline?.at}
                    windowSeconds={data.countdown_seconds}
                    version={item.version}
                  />
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
                  <PermissionGate require="operator">
                    <ActionButton tone="accent" onClick={() => openDeploy(item, write)}>
                      立即发布
                    </ActionButton>
                    <ActionButton tone="danger" onClick={() => openCancel(item, write)}>
                      取消公告
                    </ActionButton>
                    <ActionButton onClick={() => setQueueFor(item)}>排队补丁</ActionButton>
                  </PermissionGate>
                  <span className="ml-auto text-xs text-idle">到期将自动进入金丝雀发布</span>
                </div>
                {item.queued_patches.length > 0 && (
                  <div className="mt-3 border-t border-line pt-3 text-xs text-muted">
                    队列（FIFO）：
                    {item.queued_patches.map((patch, index) => (
                      <span key={`${index}-${patch.alert_id}`} className="ml-2 font-mono text-ink">
                        {patch.alert_id}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="mt-5">
            <EmptyState title="当前没有处于公告倒计时的流程" hint="审批通过后进入公告窗口的流程会出现在这里" />
          </div>
        ))}

      <QueuePatchDialog
        open={queueFor !== null}
        wfId={queueFor?.wf_id ?? ''}
        onClose={() => setQueueFor(null)}
      />
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
