/** 登录页（公开）：用户名 + 密码换取会话令牌；附静态令牌应急入口。
 *
 * - 成功后缓存清零并回到来源路径（RequireAuth 记录的 from）；
 * - 静态令牌入口面向自动化 / 排障场景（与「用户菜单 → 访问令牌」等价）；
 * - 改密后跳转会携带 state.notice 展示提示。
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Ticket } from 'lucide-react'
import { api, describeError, normalizeToken, setToken } from '../lib/api'
import { cn } from '../lib/format'
import { Logo } from '../components/Logo'
import { ThemeMenu } from '../components/ThemeMenu'
import { fieldClass, primaryButtonClass, secondaryButtonClass } from '../components/Modal'

export function Login() {
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const state = (location.state ?? {}) as { from?: string; notice?: string }

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tokenOpen, setTokenOpen] = useState(false)
  const [tokenDraft, setTokenDraft] = useState('')

  /** 凭证写入后统一入口：清缓存（避免串身份数据）→ 回来源路径。 */
  const enter = (token: string) => {
    setToken(token)
    queryClient.clear()
    const from = state.from && !state.from.startsWith('/login') ? state.from : '/'
    navigate(from, { replace: true })
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !username.trim() || !password) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.login(username.trim(), password)
      enter(result.token)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  const useToken = () => {
    const normalized = normalizeToken(tokenDraft)
    if (!normalized) return
    enter(normalized)
  }

  return (
    <div className="relative flex min-h-full items-center justify-center px-6 py-16">
      <div className="absolute right-5 top-5">
        <ThemeMenu />
      </div>

      <div className="w-full max-w-sm">
        <div className="mb-7 flex flex-col items-center gap-3">
          <Logo size={46} className="text-ink" />
          <div className="text-center">
            <div className="text-lg font-semibold tracking-wide text-ink">AIOps 运维控制台</div>
            <div className="mt-0.5 text-xs text-idle">自动运维智能体 · 全链路处置与发布管控</div>
          </div>
        </div>

        <div className="rounded-2xl border border-line bg-panel p-6 shadow-2xl">
          {state.notice && (
            <div className="mb-4 rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-xs text-ok">
              {state.notice}
            </div>
          )}

          <form onSubmit={submit} className="space-y-4">
            <label className="block space-y-1.5">
              <span className="text-xs text-muted">用户名</span>
              <input
                autoFocus
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="admin"
                className={fieldClass}
              />
            </label>
            <label className="block space-y-1.5">
              <span className="text-xs text-muted">密码</span>
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="••••••••"
                className={fieldClass}
              />
            </label>

            {error && (
              <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={busy || !username.trim() || !password}
              className={cn(primaryButtonClass, 'w-full')}
            >
              {busy ? '登录中…' : '登录'}
            </button>
          </form>

          <div className="mt-4 border-t border-line pt-3">
            <button
              type="button"
              onClick={() => setTokenOpen((value) => !value)}
              className="flex w-full items-center justify-between text-xs text-idle transition-colors hover:text-muted"
            >
              <span className="inline-flex items-center gap-1.5">
                <Ticket size={12} />
                使用静态访问令牌（自动化 / 应急）
              </span>
              <ChevronDown
                size={12}
                className={cn('transition-transform', tokenOpen && 'rotate-180')}
              />
            </button>
            {tokenOpen && (
              <div className="mt-2.5 space-y-2">
                <div className="flex gap-1.5">
                  <input
                    type="password"
                    value={tokenDraft}
                    onChange={(event) => setTokenDraft(event.target.value)}
                    onKeyDown={(event) => event.key === 'Enter' && useToken()}
                    placeholder="Bearer token"
                    className={cn(fieldClass, 'flex-1 font-mono text-xs')}
                  />
                  <button
                    type="button"
                    onClick={useToken}
                    disabled={!tokenDraft.trim()}
                    className={cn(secondaryButtonClass, 'shrink-0')}
                  >
                    进入
                  </button>
                </div>
                <p className="text-[11px] leading-relaxed text-idle">
                  适用于脚本 / CI 等无法交互登录的场景；令牌保存在本机浏览器，会覆盖当前登录会话。
                </p>
              </div>
            )}
          </div>
        </div>

        <p className="mt-5 text-center text-[11px] leading-relaxed text-idle">
          初始管理员账号 admin（初始密码由部署环境 AIOPS_BOOTSTRAP_PASSWORD 指定，见部署手册）；
          登录会话 12 小时内活跃自动续期。
        </p>
      </div>
    </div>
  )
}
