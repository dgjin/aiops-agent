/** 外观菜单（顶栏）：深浅主题切换 + 强调色三选；偏好本地持久化。 */

import { useRef, useState } from 'react'
import { Moon, Palette, Sun } from 'lucide-react'
import { ACCENTS, useTheme } from '../lib/theme'
import { useDismiss } from '../lib/hooks'
import { cn } from '../lib/format'

export function ThemeMenu() {
  const { theme, accent, setTheme, setAccent } = useTheme()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useDismiss(ref, () => setOpen(false), open)

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        title="外观：主题与强调色"
        aria-label="外观设置"
        className={cn(
          'rounded-lg p-2 text-muted transition-colors hover:bg-elevated hover:text-ink',
          open && 'bg-elevated text-ink',
        )}
      >
        <Palette size={16} strokeWidth={1.8} />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1.5 w-60 rounded-xl border border-line bg-panel p-3.5 shadow-2xl">
          <div className="mb-1.5 text-[11px] text-idle">主题</div>
          <div className="grid grid-cols-2 gap-1.5">
            <button
              type="button"
              onClick={() => setTheme('dark')}
              className={cn(
                'flex items-center justify-center gap-1.5 rounded-lg border px-2 py-1.5 text-xs transition-colors',
                theme === 'dark'
                  ? 'border-accent/60 bg-accent/10 text-accent'
                  : 'border-line text-muted hover:bg-elevated hover:text-ink',
              )}
            >
              <Moon size={13} /> 深色
            </button>
            <button
              type="button"
              onClick={() => setTheme('light')}
              className={cn(
                'flex items-center justify-center gap-1.5 rounded-lg border px-2 py-1.5 text-xs transition-colors',
                theme === 'light'
                  ? 'border-accent/60 bg-accent/10 text-accent'
                  : 'border-line text-muted hover:bg-elevated hover:text-ink',
              )}
            >
              <Sun size={13} /> 浅色
            </button>
          </div>

          <div className="mb-1.5 mt-3.5 text-[11px] text-idle">强调色</div>
          <div className="flex items-center gap-2">
            {ACCENTS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setAccent(item.id)}
                title={item.label}
                aria-label={`强调色：${item.label}`}
                className={cn(
                  'h-7 w-7 rounded-full border-2 transition-transform hover:scale-110',
                  accent === item.id ? 'border-ink' : 'border-transparent',
                )}
                style={{ backgroundColor: item.swatch }}
              />
            ))}
            <span className="ml-1 text-xs text-muted">
              {ACCENTS.find((item) => item.id === accent)?.label}
            </span>
          </div>

          <div className="mt-3.5 border-t border-line pt-2.5 text-[11px] leading-relaxed text-idle">
            主题与强调色保存在本机浏览器；深浅两套均通过对比度检查（正文 ≥ AA）。
          </div>
        </div>
      )}
    </div>
  )
}
