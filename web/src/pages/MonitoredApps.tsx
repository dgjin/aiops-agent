/** 被监控应用维护：清单增删改 + 启停 + 导入导出 + 批量 + 实时探测（写盘即热生效）。 */

import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Download, Plus, Upload } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, downloadJson, fmtDateTime } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { hasRole, useSelf } from '../lib/permission'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { EmptyState } from '../components/EmptyState'
import { ProbeBadge, ReadinessLine, WatcherLine } from '../components/MonitoredAppStatus'
import { PermissionGate } from '../components/PermissionGate'
import { Toast } from '../components/Toast'
import type { MonitoredApp, MonitoredAppInput } from '../lib/types'

const INPUT_CLS =
  'w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink placeholder:text-idle focus:border-accent/50 focus:outline-none'
const INPUT_ERR_CLS = 'border-danger/60'

const EMPTY_FORM: MonitoredAppInput = {
  name: '',
  url: '',
  service: 'nl2sql',
  log_path: '',
  probe_keyword: '',
  health_path: '',
  repo: '',
  enabled: true,
  note: '',
}

const SERVICE_RE = /^[\w\-.:]+$/

/** 字段级校验（与服务端规则一致：名称必填唯一、地址需 http(s)、service 需匹配 label 规范）。 */
export function validateApp(
  form: MonitoredAppInput,
  existingNames: string[],
  originalName?: string,
): Record<string, string> {
  const errors: Record<string, string> = {}
  const name = form.name.trim()
  if (!name) errors.name = '名称必填'
  else if (existingNames.some((item) => item === name && item !== originalName)) {
    errors.name = `名称已存在：${name}`
  }

  const url = form.url.trim()
  if (!url) errors.url = '地址必填'
  else if (!/^https?:\/\//i.test(url)) errors.url = '地址必须以 http:// 或 https:// 开头'
  else {
    try {
      new URL(url)
    } catch {
      errors.url = '地址格式非法'
    }
  }

  const service = (form.service ?? '').trim()
  if (service && !SERVICE_RE.test(service)) errors.service = '仅允许字母 / 数字 / . _ - :'

  return errors
}

function Field({
  label,
  error,
  hint,
  children,
}: {
  label: string
  error?: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div>
      <label className="mb-1 block text-xs text-muted">{label}</label>
      {children}
      {error ? (
        <p className="mt-1 text-[11px] text-danger">{error}</p>
      ) : (
        hint && <p className="mt-1 text-[11px] text-idle">{hint}</p>
      )}
    </div>
  )
}

/** 新增 / 编辑表单：实时字段级校验，存在错误时禁用提交。 */
function AppFormDialog({
  open,
  initial,
  existingNames,
  onClose,
}: {
  open: boolean
  initial: MonitoredApp | null
  existingNames: string[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<MonitoredAppInput>(EMPTY_FORM)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [touched, setTouched] = useState(false)
  const [lastKey, setLastKey] = useState<string | null>(null)
  const [discovering, setDiscovering] = useState(false)
  const [discoverMsg, setDiscoverMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const key = `${open}:${initial?.id ?? 'new'}`
  if (open && key !== lastKey) {
    setLastKey(key)
    setForm(
      initial
        ? {
            name: initial.name,
            url: initial.url,
            service: initial.service,
            log_path: initial.log_path,
            probe_keyword: initial.probe_keyword,
            health_path: initial.health_path ?? '',
            repo: initial.repo ?? '',
            enabled: initial.enabled,
            note: initial.note,
          }
        : EMPTY_FORM,
    )
    setError(null)
    setTouched(false)
    setDiscoverMsg(null)
  }
  if (!open && lastKey !== null) setLastKey(null)

  const patch = (next: Partial<MonitoredAppInput>) => {
    setTouched(true)
    setForm((prev) => ({ ...prev, ...next }))
  }

  /**
   * 从标准接口自动探测（GET <url>/.well-known/aiops.json，AIOps Manifest v1.0）：
   * 成功则预填技术字段（名称仅当为空时填充，避免覆盖人工命名）；
   * 未接入标准接口是常态，提示可手动填写。
   */
  const discover = async () => {
    const url = form.url.trim()
    if (!/^https?:\/\//i.test(url)) {
      setDiscoverMsg({ ok: false, text: '请先填写合法地址（http:// 或 https://）' })
      return
    }
    setDiscovering(true)
    setDiscoverMsg(null)
    try {
      const result = await api.discoverMonitoredApp(url)
      if (result.ok && result.suggested) {
        const { name, service, probe_keyword, health_path, log_path } = result.suggested
        const applied: string[] = []
        const next: Partial<MonitoredAppInput> = {}
        if (name && !form.name.trim()) {
          next.name = name
          applied.push('名称')
        }
        if (service) {
          next.service = service
          applied.push('service')
        }
        if (probe_keyword) {
          next.probe_keyword = probe_keyword
          applied.push('健康关键字')
        }
        if (health_path) {
          next.health_path = health_path
          applied.push('健康路径')
        }
        if (log_path) {
          next.log_path = log_path
          applied.push('日志路径')
        }
        if (Object.keys(next).length > 0) patch(next)
        const warn = result.warnings.length > 0 ? `（${result.warnings.join('；')}）` : ''
        setDiscoverMsg({
          ok: true,
          text:
            applied.length > 0
              ? `已从标准接口预填：${applied.join(' / ')}${warn}`
              : `标准接口可用，未提供可预填字段${warn}`,
        })
      } else {
        setDiscoverMsg({
          ok: false,
          text: `未检测到标准接口（${result.error || '应用未部署 /.well-known/aiops.json'}），可手动填写`,
        })
      }
    } catch (err) {
      setDiscoverMsg({ ok: false, text: `探测失败：${describeError(err)}，可手动填写` })
    } finally {
      setDiscovering(false)
    }
  }

  const errors = validateApp(form, existingNames, initial?.name)
  const hasErrors = Object.keys(errors).length > 0
  const showError = (field: string) => (touched ? errors[field] : undefined)

  const submit = async () => {
    setTouched(true)
    if (hasErrors) {
      setError('请先修正表单中的校验错误')
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (initial) await api.updateMonitoredApp(initial.id, form)
      else await api.createMonitoredApp(form)
      await queryClient.invalidateQueries()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title={initial ? `编辑被监控应用：${initial.name}` : '新增被监控应用'}
      confirmLabel={initial ? '保存' : '创建'}
      busy={busy}
      error={error}
      confirmDisabled={hasErrors}
      onConfirm={submit}
      onClose={() => {
        if (!busy) onClose()
      }}
    >
      <div className="space-y-3">
        <Field label="名称 *" error={showError('name')}>
          <input
            value={form.name}
            onChange={(e) => patch({ name: e.target.value })}
            placeholder="例如 智能问数据分析系统"
            className={cn(INPUT_CLS, showError('name') && INPUT_ERR_CLS)}
          />
        </Field>
        <Field label="地址 *（http:// 或 https://）" error={showError('url')}>
          <div className="flex gap-2">
            <input
              value={form.url}
              onChange={(e) => {
                patch({ url: e.target.value })
                setDiscoverMsg(null)
              }}
              placeholder="http://localhost:3000/"
              className={cn(INPUT_CLS, 'flex-1 font-mono text-xs', showError('url') && INPUT_ERR_CLS)}
            />
            <button
              type="button"
              onClick={discover}
              disabled={discovering}
              className="shrink-0 rounded-lg border border-accent/40 px-3 py-2 text-xs text-accent hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-50"
              title="读取应用的 /.well-known/aiops.json 标准接口（AIOps Manifest v1.0），自动预填名称 / service / 健康关键字 / 日志路径"
            >
              {discovering ? '探测中…' : '从标准接口探测'}
            </button>
          </div>
          {discoverMsg && (
            <p className={cn('mt-1 text-[11px]', discoverMsg.ok ? 'text-ok' : 'text-warn')}>
              {discoverMsg.text}
            </p>
          )}
        </Field>
        <Field
          label="Loki service 标签"
          error={showError('service')}
          hint="仅允许字母 / 数字 / . _ - :（与 Loki label 规范一致）"
        >
          <input
            value={form.service}
            onChange={(e) => patch({ service: e.target.value })}
            placeholder="nl2sql"
            className={cn(INPUT_CLS, 'font-mono text-xs', showError('service') && INPUT_ERR_CLS)}
          />
        </Field>
        <Field label="日志路径（供采集器按清单采集；支持通配）" hint="留空则该应用只做健康探测、不采集日志">
          <input
            value={form.log_path ?? ''}
            onChange={(e) => patch({ log_path: e.target.value })}
            placeholder="/path/to/app/logs/app_server*.log"
            className={cn(INPUT_CLS, 'font-mono text-xs')}
          />
        </Field>
        <Field
          label="页面关键字（可选）"
          hint={'留空则仅探测连接与状态码；配置后响应内容须包含该关键字才算在线（如 <div id="root"），可发现「端口活着但页面白屏」类故障'}
        >
          <input
            value={form.probe_keyword ?? ''}
            onChange={(e) => patch({ probe_keyword: e.target.value })}
            placeholder={'例如 <div id="root"'}
            className={cn(INPUT_CLS, 'font-mono text-xs')}
          />
        </Field>
        <Field
          label="健康检查路径（可选）"
          hint="Manifest 自动预填；非空时探测与巡检请求该路径（如 /health），关键字也在该页校验"
        >
          <input
            value={form.health_path ?? ''}
            onChange={(e) => patch({ health_path: e.target.value })}
            placeholder="/health"
            className={cn(INPUT_CLS, 'font-mono text-xs')}
          />
        </Field>
        <Field
          label="修复仓库路径（可选）"
          hint="供 AIOps 修复引擎定位并生成补丁（如白屏/坏页面自动修复）；留空则不参与自动修复"
        >
          <input
            value={form.repo ?? ''}
            onChange={(e) => patch({ repo: e.target.value })}
            placeholder="/path/to/app/repo"
            className={cn(INPUT_CLS, 'font-mono text-xs')}
          />
        </Field>
        <Field label="备注">
          <input
            value={form.note}
            onChange={(e) => patch({ note: e.target.value })}
            placeholder="可选"
            className={INPUT_CLS}
          />
        </Field>
        <label className="flex items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => patch({ enabled: e.target.checked })}
            className="h-3.5 w-3.5 accent-current"
          />
          启用（启用后才会发起健康探测）
        </label>
        <p className="text-xs text-idle">保存后立即生效（清单写盘，BFF 不做进程缓存，无需重启）。</p>
      </div>
    </ConfirmDialog>
  )
}

export function MonitoredApps() {
  const { data, isError, error } = useQuery({
    queryKey: ['monitored-apps'],
    queryFn: api.monitoredApps,
    refetchInterval: 15000,
  })
  const queryClient = useQueryClient()
  const write = useWriteAction()
  const canManage = hasRole(useSelf()?.role, 'admin')
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<MonitoredApp | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [importOpen, setImportOpen] = useState(false)
  const [importPayload, setImportPayload] = useState<unknown[]>([])
  const [importMode, setImportMode] = useState<'merge' | 'replace'>('merge')
  const [importBusy, setImportBusy] = useState(false)
  const [importError, setImportError] = useState<string | null>(null)
  const [importResult, setImportResult] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  if (isError) return <EmptyState title="无法加载被监控应用" hint={describeError(error)} />
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  const items = data.items
  const names = items.map((item) => item.name)
  const allSelected = items.length > 0 && selected.length === items.length

  const openCreate = () => {
    setEditing(null)
    setFormOpen(true)
  }
  const openEdit = (app: MonitoredApp) => {
    setEditing(app)
    setFormOpen(true)
  }

  const confirmToggle = (app: MonitoredApp) =>
    write.open({
      title: app.enabled ? `停用「${app.name}」？` : `启用「${app.name}」？`,
      confirmLabel: app.enabled ? '停用' : '启用',
      detail: <p className="text-sm text-muted">停用后不再发起健康探测（清单与配置保留）。</p>,
      run: () => api.toggleMonitoredApp(app.id),
      success: app.enabled ? `已停用「${app.name}」` : `已启用「${app.name}」`,
    })

  const confirmDelete = (app: MonitoredApp) =>
    write.open({
      title: `删除「${app.name}」？`,
      tone: 'danger',
      confirmLabel: '删除',
      detail: <p className="text-sm text-muted">删除后该条目从清单移除，操作会记入审计。</p>,
      run: () => api.deleteMonitoredApp(app.id),
      success: `已删除「${app.name}」`,
    })

  const confirmDuplicate = (app: MonitoredApp) =>
    write.open({
      title: `复制「${app.name}」？`,
      confirmLabel: '复制',
      detail: <p className="text-sm text-muted">将以新名称创建一份相同配置（名称自动去重）。</p>,
      run: () => api.duplicateMonitoredApp(app.id),
      success: `已复制「${app.name}」`,
    })

  const confirmBatch = (action: 'enable' | 'disable' | 'delete') => {
    const label = { enable: '启用', disable: '停用', delete: '删除' }[action]
    return write.open({
      title: `批量${label} ${selected.length} 个条目？`,
      tone: action === 'delete' ? 'danger' : 'default',
      confirmLabel: label,
      detail: (
        <p className="text-sm text-muted">
          将{label}已选中的 {selected.length} 个被监控应用；操作记入审计。
        </p>
      ),
      run: async () => {
        const result = await api.batchMonitoredApps(selected, action)
        setSelected([])
        return result
      },
      success: `批量${label}完成`,
    })
  }

  const doExport = async () => {
    const payload = await api.exportMonitoredApps()
    downloadJson(`monitored-apps-${new Date().toISOString().slice(0, 10)}.json`, payload)
  }

  const onPickFile = async (file: File) => {
    setImportError(null)
    try {
      const parsed = JSON.parse(await file.text())
      const list = Array.isArray(parsed) ? parsed : parsed?.items
      if (!Array.isArray(list) || list.length === 0) {
        setImportError('文件中未找到可用条目（需为数组，或含 items 数组的对象）')
        return
      }
      setImportPayload(list)
      setImportOpen(true)
    } catch (err) {
      setImportError(`JSON 解析失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  const confirmImport = async () => {
    setImportBusy(true)
    setImportError(null)
    try {
      const result = await api.importMonitoredApps(importPayload, importMode)
      await queryClient.invalidateQueries()
      setImportOpen(false)
      setSelected([])
      const failed = result.errors?.length ?? 0
      setImportResult(
        `导入完成：新增 ${result.added}、更新 ${result.updated}` +
          (failed ? `、失败 ${failed}` : '') +
          `（共 ${result.total} 条）`,
      )
      setTimeout(() => setImportResult(null), 6000)
    } catch (err) {
      setImportError(err instanceof Error ? err.message : String(err))
    } finally {
      setImportBusy(false)
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">被监控应用</h1>
          <p className="mt-1 text-xs text-muted">
            维护需要监控的应用清单（改动热生效、支持标准接口自动探测，均无需重启）；15 秒自动刷新探测状态
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <ActionButton onClick={doExport}>
            <span className="inline-flex items-center gap-1.5">
              <Download className="h-3.5 w-3.5" />
              导出
            </span>
          </ActionButton>
          <PermissionGate require="admin">
            <ActionButton onClick={() => fileRef.current?.click()}>
              <span className="inline-flex items-center gap-1.5">
                <Upload className="h-3.5 w-3.5" />
                导入
              </span>
            </ActionButton>
            <input
              ref={fileRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                e.target.value = ''
                if (file) void onPickFile(file)
              }}
            />
            <button
              type="button"
              onClick={openCreate}
              className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-accent/90 px-3.5 py-1.5 text-sm font-medium text-canvas hover:bg-accent"
            >
              <Plus className="h-4 w-4" />
              新增
            </button>
          </PermissionGate>
        </div>
      </div>

      {importError && (
        <div className="mt-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          {importError}
        </div>
      )}
      {importResult && (
        <div className="mt-3 rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-xs text-ok">
          {importResult}
        </div>
      )}

      {selected.length > 0 && canManage && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-accent/40 bg-accent/5 px-3 py-2 text-xs">
          <span className="text-muted">已选 {selected.length} 项</span>
          <ActionButton onClick={() => confirmBatch('enable')}>批量启用</ActionButton>
          <ActionButton onClick={() => confirmBatch('disable')}>批量停用</ActionButton>
          <ActionButton tone="danger" onClick={() => confirmBatch('delete')}>
            批量删除
          </ActionButton>
          <button type="button" onClick={() => setSelected([])} className="text-idle hover:text-ink">
            取消选择
          </button>
        </div>
      )}

      <div className="mt-5 overflow-x-auto rounded-xl border border-line bg-panel">
        {items.length === 0 ? (
          <div className="p-6">
            <EmptyState title="暂无被监控应用" hint="点击右上角「新增」，或用「导入」批量录入" />
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="px-4 py-3 font-medium">
                  {canManage && (
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={() => setSelected(allSelected ? [] : items.map((item) => item.id))}
                      className="h-3.5 w-3.5 accent-current"
                      aria-label="全选"
                    />
                  )}
                </th>
                <th className="px-4 py-3 font-medium">名称</th>
                <th className="px-4 py-3 font-medium">地址</th>
                <th className="px-4 py-3 font-medium">service</th>
                <th className="px-4 py-3 font-medium">日志路径</th>
                <th className="px-4 py-3 font-medium">状态</th>
                <th className="px-4 py-3 font-medium">更新时间</th>
                <th className="px-4 py-3 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((app) => (
                <tr key={app.id} className="border-b border-line/60 last:border-0">
                  <td className="px-4 py-3">
                    {canManage && (
                      <input
                        type="checkbox"
                        checked={selected.includes(app.id)}
                        onChange={() =>
                          setSelected((prev) =>
                            prev.includes(app.id) ? prev.filter((id) => id !== app.id) : [...prev, app.id],
                          )
                        }
                        className="h-3.5 w-3.5 accent-current"
                        aria-label={`选择 ${app.name}`}
                      />
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-ink">{app.name}</div>
                    {app.note && (
                      <div className="mt-0.5 max-w-[16rem] truncate text-xs text-idle" title={app.note}>
                        {app.note}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-muted">{app.url}</td>
                  <td className="px-4 py-3 font-mono text-xs text-muted">{app.service}</td>
                  <td className="px-4 py-3">
                    {app.log_path ? (
                      <span className="block max-w-[18rem] truncate font-mono text-xs text-muted" title={app.log_path}>
                        {app.log_path}
                      </span>
                    ) : (
                      <span className="text-xs text-idle">未配置（仅探测）</span>
                    )}
                    {app.probe_keyword && (
                      <span
                        className="mt-0.5 block max-w-[18rem] truncate font-mono text-xs text-muted"
                        title={`页面关键字：${app.probe_keyword}`}
                      >
                        关键字：{app.probe_keyword}
                      </span>
                    )}
                    {app.repo && (
                      <span
                        className="mt-0.5 block max-w-[18rem] truncate font-mono text-xs text-muted"
                        title={`修复仓库：${app.repo}`}
                      >
                        仓库：{app.repo}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <ProbeBadge app={app} />
                    {app.enabled && app.watcher && <WatcherLine watcher={app.watcher} />}
                    {!app.probe.running && app.probe.error && app.enabled && (
                      <div className="mt-0.5 max-w-[22rem] truncate text-[10px] text-idle" title={app.probe.error}>
                        {app.probe.error}
                      </div>
                    )}
                    {app.readiness && <ReadinessLine readiness={app.readiness} />}
                  </td>
                  <td className="px-4 py-3 text-xs text-idle">{fmtDateTime(app.updated_at)}</td>
                  <td className="px-4 py-3">
                    {canManage ? (
                      <div className="flex justify-end gap-1.5 whitespace-nowrap">
                        <Link
                          to={`/systems/${encodeURIComponent(app.id)}`}
                          title="以该系统为视角查看健康 / 流程 / 链路（系统工作台）"
                          className="rounded-lg border border-accent/40 px-3.5 py-1.5 text-sm font-medium text-accent transition-colors hover:bg-accent/10"
                        >
                          工作台
                        </Link>
                        <ActionButton onClick={() => openEdit(app)}>编辑</ActionButton>
                        <ActionButton onClick={() => confirmDuplicate(app)}>
                          <span className="inline-flex items-center gap-1">
                            <Copy className="h-3 w-3" />
                            复制
                          </span>
                        </ActionButton>
                        <ActionButton onClick={() => confirmToggle(app)}>
                          {app.enabled ? '停用' : '启用'}
                        </ActionButton>
                        <ActionButton tone="danger" onClick={() => confirmDelete(app)}>
                          删除
                        </ActionButton>
                      </div>
                    ) : (
                      <div className="text-right text-xs text-idle">仅管理员可编辑</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <AppFormDialog
        open={formOpen}
        initial={editing}
        existingNames={names}
        onClose={() => setFormOpen(false)}
      />

      <ConfirmDialog
        open={importOpen}
        title={`导入 ${importPayload.length} 个条目`}
        confirmLabel={importMode === 'replace' ? '清空并导入' : '合并导入'}
        busy={importBusy}
        error={importError}
        onConfirm={confirmImport}
        onClose={() => {
          if (!importBusy) setImportOpen(false)
        }}
      >
        <div className="space-y-3 text-sm">
          <div className="text-muted">
            共 {importPayload.length} 条。选择导入方式：
          </div>
          <label className="flex items-start gap-2 text-xs">
            <input
              type="radio"
              checked={importMode === 'merge'}
              onChange={() => setImportMode('merge')}
              className="mt-0.5"
            />
            <span>
              <span className="text-ink">合并（merge）</span>
              <div className="text-muted">按名称匹配：已存在则更新，不存在则新增</div>
            </span>
          </label>
          <label className="flex items-start gap-2 text-xs">
            <input
              type="radio"
              checked={importMode === 'replace'}
              onChange={() => setImportMode('replace')}
              className="mt-0.5"
            />
            <span>
              <span className="text-danger">替换（replace）</span>
              <div className="text-muted">先清空现有清单再导入（不可撤销，操作记入审计）</div>
            </span>
          </label>
        </div>
      </ConfirmDialog>

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
