/** 顶栏用户菜单：身份展示（会话/静态令牌）/ 修改密码 / 访问令牌 / 登出。
 *
 * 身份数据来自 /api/auth/status 的 `self` 字段（批次 1 后端新增）。
 * 「访问令牌」弹窗承载原左下角 TokenControl 的全部能力（静态令牌通道 +
 * 轮换状态 + 手动轮换），供自动化/应急场景使用。
 */

import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, KeyRound, LogOut, ShieldCheck, Ticket } from 'lucide-react'
import { api, describeError, getToken, normalizeToken, setToken } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { useDismiss } from '../lib/hooks'
import { useWriteAction } from '../lib/actions'
import { ConfirmDialog } from './ConfirmDialog'
import { Modal, fieldClass, primaryButtonClass, secondaryButtonClass } from './Modal'
import { Toast } from './Toast'

export const ROLE_LABEL: Record<string, string> = {
  viewer: '观察者',
  operator: '操作员',
  admin: '管理员',
}

export function roleLabel(role: string | null | undefined): string {
  if (!role) return '未知'
  return ROLE_LABEL[role] ?? role
}

/** 把秒数说成人话（轮换间隔 / 宽限期）。 */
function humanSeconds(seconds: number): string {
  if (!seconds) return '—'
  if (seconds % 86400 === 0) return `${seconds / 86400} 天`
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`
  return `${seconds} 秒`
}

function MenuItem({
  icon: Icon,
  label,
  desc,
  danger = false,
  onClick,
}: {
  icon: typeof KeyRound
  label: string
  desc?: string
  danger?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm transition-colors hover:bg-elevated',
        danger ? 'text-danger' : 'text-muted hover:text-ink',
      )}
    >
      <Icon size={15} strokeWidth={1.8} className="shrink-0" />
      <span className="flex-1">{label}</span>
      {desc && <span className="text-[10px] text-idle">{desc}</span>}
    </button>
  )
}

/** 访问令牌弹窗（静态通道）：设置令牌 / 自动轮换状态 / 手动轮换。 */
function TokenModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [draft, setDraft] = useState(() => getToken())
  const [savedToken, setSavedToken] = useState(() => getToken())
  const [saved, setSaved] = useState(false)
  const savedMasked = savedToken ? `${savedToken.slice(0, 4)}••••${savedToken.slice(-2)}` : ''
  const dirty = draft !== savedToken

  const { data } = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    refetchInterval: 30000,
    retry: false,
  })
  const write = useWriteAction()

  const save = () => {
    const normalized = normalizeToken(draft)
    setToken(normalized) // 自动去掉 Bearer 前缀 / 首尾引号与空白
    setDraft(normalized)
    setSavedToken(normalized)
    setSaved(true)
    setTimeout(() => setSaved(false), 2500)
    // 令牌变化后重新拉取全部数据（原请求均为未授权状态）
    window.location.reload()
  }

  const self = data?.self_token
  const inGrace = self?.state === 'previous'
  const isSession = data?.self?.source === 'session'

  return (
    <Modal open={open} title="访问令牌（静态通道）" onClose={onClose}>
      <div className="space-y-3">
        <p className="text-xs leading-relaxed text-muted">
          用户名密码登录的会话已由系统自动管理。此处仅在**自动化调用 / 应急排障**时使用长期静态令牌；
          它会覆盖当前登录会话（保存后页面重新加载）。
        </p>
        <div className="space-y-1.5">
          <div className="text-[11px] text-idle">
            静态令牌{' '}
            {isSession ? (
              <span className="text-muted">当前为会话登录（系统自动管理，无需填写）</span>
            ) : savedToken ? (
              <span className="font-mono text-ok">已设置 {savedMasked}</span>
            ) : (
              <span className="text-danger">未设置</span>
            )}
            {dirty && <span className="text-warn">（有未保存的修改，请点「保存」）</span>}
          </div>
          <div className="flex gap-1.5">
            <input
              type="password"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && save()}
              placeholder="Bearer token（留空并保存可清除）"
              className={cn(fieldClass, 'flex-1 font-mono text-xs')}
            />
            <button type="button" onClick={save} className={primaryButtonClass}>
              {saved ? '已保存' : '保存'}
            </button>
          </div>
        </div>

        {data && (
          <div className="space-y-1 rounded-lg border border-line bg-canvas px-3 py-2.5 text-[11px] text-idle">
            <div>
              自动轮换{' '}
              {data.auto_rotation_enabled ? (
                <span className="text-muted">每 {humanSeconds(data.interval_seconds)}</span>
              ) : (
                <span className="text-muted">已关闭</span>
              )}
            </div>
            {data.next_rotation_at && (
              <div>
                下次轮换 <span className="font-mono">{fmtDateTime(data.next_rotation_at)}</span>
              </div>
            )}
            {inGrace && (
              <div className="text-warn">
                当前令牌已轮换、处于宽限期（{humanSeconds(data.grace_seconds)}），请尽快更新
              </div>
            )}
            {data.allow_reveal && (
              <div className="text-warn">已开启接口回显令牌（AIOPS_TOKEN_ALLOW_REVEAL=true）</div>
            )}
            {/* 仅在自动轮换开启时提供手动轮换：轮换关闭的部署里点它只会把令牌换成
                "只写进 600 注册表文件"的新值、界面又取不回来，反而把人锁在门外。 */}
            {data.auto_rotation_enabled && data.self?.role === 'admin' && (
              <button
                type="button"
                onClick={() =>
                  write.open({
                    title: '立即轮换所有访问令牌？',
                    confirmLabel: '轮换',
                    detail: (
                      <div className="space-y-1.5 text-xs">
                        <div className="text-muted">
                          将为全部身份生成新令牌；**旧令牌在宽限期（
                          {humanSeconds(data.grace_seconds)}）内仍然有效**，因此当前会话不会立刻失效。
                        </div>
                        <div className="text-muted">
                          新令牌写入 <span className="font-mono">{data.registry_path}</span>（权限 600）
                          {data.allow_reveal ? '，并会在此后提示中回显一次。' : '，需从该文件读取。'}
                        </div>
                      </div>
                    ),
                    run: () => api.rotateTokens('manual'),
                    success: '已轮换令牌（旧令牌宽限期内仍可用）',
                  })
                }
                className="underline hover:text-ink"
              >
                立即轮换
              </button>
            )}
          </div>
        )}
      </div>

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
    </Modal>
  )
}

/** 修改密码弹窗（本人；需原密码；成功后全部会话吊销 → 重新登录）。 */
function PasswordModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const mismatch = confirmPassword.length > 0 && newPassword !== confirmPassword
  const valid = oldPassword.length > 0 && newPassword.length >= 8 && newPassword === confirmPassword

  const reset = () => {
    setOldPassword('')
    setNewPassword('')
    setConfirmPassword('')
    setError(null)
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.changeOwnPassword(oldPassword, newPassword)
      reset()
      if (result.relogin_required) {
        // 全部会话（含当前）已被吊销：清凭证并回登录页
        setToken('')
        queryClient.clear()
        navigate('/login', { replace: true, state: { notice: '密码已修改，请使用新密码重新登录' } })
        return
      }
      onClose()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="修改密码"
      onClose={() => {
        reset()
        onClose()
      }}
      footer={
        <>
          <button
            type="button"
            className={secondaryButtonClass}
            onClick={() => {
              reset()
              onClose()
            }}
          >
            取消
          </button>
          <button
            type="button"
            className={primaryButtonClass}
            disabled={!valid || busy}
            onClick={submit}
          >
            {busy ? '提交中…' : '修改密码'}
          </button>
        </>
      }
    >
      <div className="space-y-3.5">
        <label className="block space-y-1.5">
          <span className="text-xs text-muted">原密码</span>
          <input
            type="password"
            className={fieldClass}
            value={oldPassword}
            onChange={(event) => setOldPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>
        <label className="block space-y-1.5">
          <span className="text-xs text-muted">新密码（至少 8 位）</span>
          <input
            type="password"
            className={fieldClass}
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            autoComplete="new-password"
          />
        </label>
        <label className="block space-y-1.5">
          <span className="text-xs text-muted">确认新密码</span>
          <input
            type="password"
            className={fieldClass}
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            autoComplete="new-password"
          />
          {mismatch && <span className="text-[11px] text-danger">两次输入的新密码不一致</span>}
        </label>
        {error && (
          <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {error}
          </div>
        )}
        <p className="text-[11px] leading-relaxed text-idle">
          修改成功后，你的全部登录会话（含其他设备）将被吊销，需要重新登录。
        </p>
      </div>
    </Modal>
  )
}

/** 顶栏用户菜单。 */
export function UserMenu() {
  const { data } = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    refetchInterval: 30000,
    retry: false,
  })
  const self = data?.self
  const [open, setOpen] = useState(false)
  const [tokenOpen, setTokenOpen] = useState(false)
  const [passwordOpen, setPasswordOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useDismiss(ref, () => setOpen(false), open)
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const user = self?.user ?? '…'
  const initial = (self?.user ?? '?').slice(0, 1).toUpperCase()
  const isSession = self?.source === 'session'

  const logout = async () => {
    setOpen(false)
    if (isSession) {
      try {
        await api.logout()
      } catch {
        /* 会话可能已失效：本地清理照常进行 */
      }
    }
    setToken('')
    queryClient.clear()
    navigate('/login', { replace: true })
  }

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={cn(
          'flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-elevated',
          open && 'bg-elevated',
        )}
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent">
          {initial}
        </span>
        <span className="max-w-[10rem] truncate text-sm text-ink">{user}</span>
        <ChevronDown size={13} className="text-idle" />
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1.5 w-64 rounded-xl border border-line bg-panel shadow-2xl">
          <div className="border-b border-line px-3.5 py-3">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-ink">{user}</span>
              <span className="rounded-full bg-accent/12 px-2 py-0.5 text-[10px] text-accent">
                {roleLabel(self?.role)}
              </span>
            </div>
            <div className="mt-1 flex items-center gap-1.5 text-[11px] text-idle">
              {isSession ? (
                <>
                  <ShieldCheck size={11} className="text-ok" /> 会话登录（自动续期）
                </>
              ) : (
                <>
                  <Ticket size={11} className="text-warn" /> 静态令牌（长期有效）
                </>
              )}
            </div>
            {isSession && self?.expires_at && (
              <div className="mt-0.5 text-[11px] text-idle">
                会话有效至 <span className="font-mono">{fmtDateTime(self.expires_at)}</span>
              </div>
            )}
          </div>
          <div className="p-1.5">
            {isSession && (
              <MenuItem
                icon={KeyRound}
                label="修改密码"
                onClick={() => {
                  setOpen(false)
                  setPasswordOpen(true)
                }}
              />
            )}
            <MenuItem
              icon={Ticket}
              label="访问令牌…"
              desc="静态/应急通道"
              onClick={() => {
                setOpen(false)
                setTokenOpen(true)
              }}
            />
            <MenuItem icon={LogOut} label="登出" danger onClick={logout} />
          </div>
        </div>
      )}

      <TokenModal open={tokenOpen} onClose={() => setTokenOpen(false)} />
      <PasswordModal open={passwordOpen} onClose={() => setPasswordOpen(false)} />
    </div>
  )
}
