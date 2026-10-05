/** 用户管理（仅管理员）：账号 CRUD / 角色与启停 / 重置密码 / 在线会话与强制下线。
 *
 * 服务端强制规则（前端仅做展示层辅助）：
 * - 不能删除 / 降级 / 禁用自己（本页对当前登录行隐藏相应操作）；
 * - 至少保留一个启用管理员（操作失败由确认框内错误提示兜底）；
 * - 禁用 / 删除 / 重置密码后会话即时失效或强制下线（resolve 以用户表为准）。
 */

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { api, describeError } from '../lib/api'
import { cn, fmtDateTime } from '../lib/format'
import { useWriteAction } from '../lib/actions'
import { hasRole, useSelf } from '../lib/permission'
import { ActionButton } from '../components/ActionButton'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { EmptyState } from '../components/EmptyState'
import { Modal, fieldClass, secondaryButtonClass } from '../components/Modal'
import { Toast } from '../components/Toast'
import { roleLabel } from '../components/UserMenu'
import type { SessionItem, UserItem } from '../lib/types'

/** 与服务端 USERNAME_PATTERN 一致的本地校验。 */
const USERNAME_RE = /^[A-Za-z0-9_.-]{2,32}$/

const ROLE_OPTIONS: { value: string; label: string; desc: string }[] = [
  { value: 'viewer', label: '观察者', desc: '只读：看板 / 流程 / 审计 / 帮助' },
  { value: 'operator', label: '操作员', desc: '观察者 + 审批 / 发布指令 / 排队补丁' },
  {
    value: 'admin',
    label: '管理员',
    desc: '全部权限 + 被监控应用 / 用户管理 / 令牌与 kill switch',
  },
]

const dangerButtonClass =
  'rounded-lg bg-danger/90 px-3.5 py-1.5 text-sm font-medium text-canvas transition-colors hover:bg-danger disabled:cursor-not-allowed disabled:opacity-50'

/** 创建用户弹窗（挂载即打开；每次打开都是全新状态）。 */
function CreateUserDialog({ existing, onClose }: { existing: string[]; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('viewer')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const clean = username.trim()
  const nameError = !clean
    ? '用户名必填'
    : !USERNAME_RE.test(clean)
      ? '须为 2-32 位字母 / 数字 / . _ - 组合'
      : existing.includes(clean)
        ? '用户名已存在'
        : ''
  const valid = !nameError && password.length >= 8

  const submit = async () => {
    if (!valid) return
    setBusy(true)
    setError(null)
    try {
      await api.createUser(clean, password, role)
      await queryClient.invalidateQueries({ queryKey: ['users'] })
      onClose()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <ConfirmDialog
      open
      title="创建用户"
      confirmLabel="创建"
      busy={busy}
      error={error}
      confirmDisabled={!valid}
      onConfirm={submit}
      onClose={() => {
        if (!busy) onClose()
      }}
    >
      <div className="space-y-3.5">
        <div>
          <label className="mb-1 block text-xs text-muted">用户名 *</label>
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="例：zhang.wei"
            autoComplete="off"
            className={fieldClass}
          />
          {nameError && <p className="mt-1 text-[11px] text-danger">{nameError}</p>}
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted">初始密码 *（至少 8 位）</label>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="创建后由本人登录修改"
            autoComplete="new-password"
            className={fieldClass}
          />
          {password.length > 0 && password.length < 8 && (
            <p className="mt-1 text-[11px] text-danger">密码长度不能少于 8 位</p>
          )}
        </div>
        <div className="space-y-1.5">
          <div className="text-xs text-muted">角色</div>
          {ROLE_OPTIONS.map((option) => (
            <label
              key={option.value}
              className={cn(
                'flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 text-xs transition-colors',
                role === option.value
                  ? 'border-accent/60 bg-accent/10'
                  : 'border-line hover:bg-elevated',
              )}
            >
              <input
                type="radio"
                checked={role === option.value}
                onChange={() => setRole(option.value)}
                className="mt-0.5"
              />
              <span>
                <span className="text-ink">{option.label}</span>
                <div className="text-muted">{option.desc}</div>
              </span>
            </label>
          ))}
        </div>
      </div>
    </ConfirmDialog>
  )
}

/** 重置他人密码弹窗：成功后强制下线其全部会话。 */
function ResetPasswordDialog({
  user,
  onDone,
  onClose,
}: {
  user: UserItem
  onDone: (message: string) => void
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    if (password.length < 8) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.resetUserPassword(user.username, password)
      await queryClient.invalidateQueries({ queryKey: ['users'] })
      onDone(
        `已重置「${user.username}」的密码，并吊销 ${result.revoked_sessions} 个会话（强制下线）`,
      )
      onClose()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <ConfirmDialog
      open
      title={`重置「${user.username}」的密码`}
      confirmLabel="重置密码"
      busy={busy}
      error={error}
      confirmDisabled={password.length < 8}
      onConfirm={submit}
      onClose={() => {
        if (!busy) onClose()
      }}
    >
      <div className="space-y-3">
        <div>
          <label className="mb-1 block text-xs text-muted">新密码 *（至少 8 位）</label>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            onKeyDown={(event) => event.key === 'Enter' && submit()}
            autoComplete="new-password"
            className={fieldClass}
          />
        </div>
        <p className="text-[11px] leading-relaxed text-idle">
          重置后该用户现有密码立即失效，全部登录会话被吊销（强制下线）；请通过安全渠道告知新密码。
        </p>
      </div>
    </ConfirmDialog>
  )
}

/** 在线会话弹窗：查看有效会话（脱敏指纹）并支持全部强制下线。 */
function SessionsModal({ user, onClose }: { user: UserItem; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const { data, isError, error: loadError } = useQuery({
    queryKey: ['user-sessions', user.username],
    queryFn: () => api.userSessions(user.username),
    refetchInterval: 15000,
  })

  const sessions = data?.sessions ?? []

  const revokeAll = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.revokeUserSessions(user.username)
      await queryClient.invalidateQueries({ queryKey: ['user-sessions', user.username] })
      setNotice(`已吊销 ${result.revoked_sessions} 个会话（强制下线）`)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      title={`「${user.username}」的在线会话`}
      size="md"
      onClose={onClose}
      footer={
        <>
          <button type="button" className={secondaryButtonClass} onClick={onClose}>
            关闭
          </button>
          <button
            type="button"
            className={dangerButtonClass}
            disabled={busy || sessions.length === 0}
            onClick={revokeAll}
          >
            {busy ? '处理中…' : `全部强制下线（${sessions.length}）`}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {error && (
          <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {error}
          </div>
        )}
        {notice && (
          <div className="rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-xs text-ok">
            {notice}
          </div>
        )}
        {isError ? (
          <p className="text-xs text-danger">{describeError(loadError)}</p>
        ) : sessions.length === 0 ? (
          <p className="text-xs text-muted">
            当前没有有效的登录会话。该用户的静态令牌（如有）不受会话吊销影响。
          </p>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="py-2 pr-3 font-medium">会话指纹</th>
                <th className="py-2 pr-3 font-medium">创建时间</th>
                <th className="py-2 pr-3 font-medium">最近活动</th>
                <th className="py-2 font-medium">到期时间</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((session: SessionItem) => (
                <tr key={session.id} className="border-b border-line/60 last:border-0">
                  <td className="py-2 pr-3 font-mono text-muted">{session.id}…</td>
                  <td className="py-2 pr-3 text-muted">{fmtDateTime(session.created_at)}</td>
                  <td className="py-2 pr-3 text-muted">{fmtDateTime(session.last_seen_at)}</td>
                  <td className="py-2 text-muted">{fmtDateTime(session.expires_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="text-[11px] leading-relaxed text-idle">
          会话在 12 小时内活跃会自动续期；「强制下线」吊销全部有效会话，该用户需重新登录。
        </p>
      </div>
    </Modal>
  )
}

export function Users() {
  const { data, isError, error } = useQuery({
    queryKey: ['users'],
    queryFn: api.users,
    refetchInterval: 30000,
  })
  const self = useSelf()
  const me = self?.user
  const write = useWriteAction()
  const [createOpen, setCreateOpen] = useState(false)
  const [resetTarget, setResetTarget] = useState<UserItem | null>(null)
  const [sessionsTarget, setSessionsTarget] = useState<UserItem | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  // 越权拦截：非管理员给出说明（/api/users 服务端同样强制 admin）
  if (self && !hasRole(self.role, 'admin')) {
    return (
      <EmptyState
        title="仅管理员可访问"
        hint="当前账号权限不足；如需访问用户管理，请由管理员调整你的角色"
      />
    )
  }
  if (isError) return <EmptyState title="无法加载用户列表" hint={describeError(error)} />
  if (!data) return <div className="text-sm text-muted">加载中…</div>

  const users = data.users
  const adminCount = users.filter(
    (user) => user.role === 'admin' && user.state === 'active',
  ).length

  const notify = (message: string) => {
    setToast(message)
    setTimeout(() => setToast(null), 4000)
  }

  const changeRole = (user: UserItem, role: string) => {
    if (role === user.role) return
    write.open({
      title: `将「${user.username}」调整为${roleLabel(role)}？`,
      confirmLabel: '调整角色',
      detail: (
        <p className="text-sm text-muted">
          角色即时生效（现有会话立即按新角色鉴权）：{roleLabel(user.role)} → {roleLabel(role)}
          ；变更记入审计。
        </p>
      ),
      run: () => api.updateUser(user.username, { role }),
      success: `已将「${user.username}」调整为${roleLabel(role)}`,
    })
  }

  const toggleState = (user: UserItem) => {
    const disable = user.state === 'active'
    write.open({
      title: disable ? `禁用「${user.username}」？` : `启用「${user.username}」？`,
      tone: disable ? 'danger' : 'default',
      confirmLabel: disable ? '禁用' : '启用',
      detail: (
        <p className="text-sm text-muted">
          {disable
            ? '禁用后立即无法登录，且现有登录会话即时失效（无需再手动吊销）。'
            : '启用后该账号可正常登录与使用控制台。'}
        </p>
      ),
      run: () => api.updateUser(user.username, { state: disable ? 'disabled' : 'active' }),
      success: disable ? `已禁用「${user.username}」` : `已启用「${user.username}」`,
    })
  }

  const remove = (user: UserItem) => {
    write.open({
      title: `删除用户「${user.username}」？`,
      tone: 'danger',
      confirmLabel: '删除',
      detail: (
        <p className="text-sm text-muted">
          删除不可恢复；其全部登录会话将被吊销（强制下线），操作记入审计。
        </p>
      ),
      run: () => api.deleteUser(user.username),
      success: `已删除「${user.username}」`,
    })
  }

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">用户管理</h1>
          <p className="mt-1 text-xs text-muted">
            共 {users.length} 个账号 · {adminCount} 个启用管理员；服务端强制保留至少一个启用管理员，且不能删除
            / 降级 / 禁用自己；30 秒自动刷新
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-accent/90 px-3.5 py-1.5 text-sm font-medium text-canvas hover:bg-accent"
        >
          <Plus className="h-4 w-4" />
          创建用户
        </button>
      </div>

      <div className="mt-5 overflow-x-auto rounded-xl border border-line bg-panel">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted">
              <th className="px-4 py-3 font-medium">用户名</th>
              <th className="px-4 py-3 font-medium">角色</th>
              <th className="px-4 py-3 font-medium">状态</th>
              <th className="px-4 py-3 font-medium">创建时间</th>
              <th className="px-4 py-3 font-medium">更新时间</th>
              <th className="px-4 py-3 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => {
              const isSelf = user.username === me
              return (
                <tr key={user.username} className="border-b border-line/60 last:border-0">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="text-ink">{user.username}</span>
                      {isSelf && (
                        <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] text-accent">
                          当前登录
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <select
                      value={user.role}
                      disabled={isSelf}
                      title={isSelf ? '不能调整自己的角色' : '调整角色'}
                      onChange={(event) => changeRole(user, event.target.value)}
                      className="rounded-lg border border-line bg-canvas px-2 py-1 text-xs text-ink focus:border-accent/60 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {ROLE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-4 py-3">
                    {user.state === 'active' ? (
                      <span className="inline-flex items-center gap-1.5 text-xs text-muted">
                        <span className="h-1.5 w-1.5 rounded-full bg-ok" />
                        启用
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-xs text-danger">
                        <span className="h-1.5 w-1.5 rounded-full bg-danger" />
                        已禁用
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-idle">{fmtDateTime(user.created_at)}</td>
                  <td className="px-4 py-3 text-xs text-idle">{fmtDateTime(user.updated_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1.5 whitespace-nowrap">
                      <ActionButton onClick={() => setSessionsTarget(user)}>会话</ActionButton>
                      <ActionButton onClick={() => setResetTarget(user)}>重置密码</ActionButton>
                      {!isSelf && (
                        <ActionButton onClick={() => toggleState(user)}>
                          {user.state === 'active' ? '禁用' : '启用'}
                        </ActionButton>
                      )}
                      {!isSelf && (
                        <ActionButton tone="danger" onClick={() => remove(user)}>
                          删除
                        </ActionButton>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <CreateUserDialog
          existing={users.map((user) => user.username)}
          onClose={() => setCreateOpen(false)}
        />
      )}
      {resetTarget && (
        <ResetPasswordDialog
          user={resetTarget}
          onDone={notify}
          onClose={() => setResetTarget(null)}
        />
      )}
      {sessionsTarget && (
        <SessionsModal user={sessionsTarget} onClose={() => setSessionsTarget(null)} />
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

      <Toast message={write.toast ?? toast} />
    </div>
  )
}
