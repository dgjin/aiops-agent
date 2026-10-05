/** 主题与强调色管理：localStorage 持久化 + <html> data 属性驱动 CSS token 切换。
 *
 * 设计（对应 styles.css 两层 token）：
 * - `data-theme`（dark/light）与 `data-accent`（teal/blue/violet）设在 <html> 上；
 * - initTheme() 在 main.tsx 渲染前调用（首屏前应用，避免主题闪烁）;
 * - useTheme() 供组件读写，并经自定义事件在多实例间同步。
 */

import { useCallback, useEffect, useState } from 'react'

export type ThemeMode = 'dark' | 'light'
export type AccentColor = 'teal' | 'blue' | 'violet'

const THEME_KEY = 'aiops.theme'
const ACCENT_KEY = 'aiops.accent'
const THEME_EVENT = 'aiops:theme-change'

/** 强调色选项（swatch 为深色档展示色，仅用于选择器预览）。 */
export const ACCENTS: { id: AccentColor; label: string; swatch: string }[] = [
  { id: 'teal', label: '青碧', swatch: '#2dd4bf' },
  { id: 'blue', label: '天蓝', swatch: '#38bdf8' },
  { id: 'violet', label: '紫罗兰', swatch: '#a78bfa' },
]

function read(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* localStorage 不可用时仅本次会话生效 */
  }
}

export function getTheme(): ThemeMode {
  return read(THEME_KEY, 'dark') === 'light' ? 'light' : 'dark'
}

export function getAccent(): AccentColor {
  const value = read(ACCENT_KEY, 'teal')
  return value === 'blue' || value === 'violet' ? value : 'teal'
}

function applyDom(theme: ThemeMode, accent: AccentColor): void {
  const root = document.documentElement
  root.dataset.theme = theme
  root.dataset.accent = accent
}

/** 启动时立即应用已保存偏好（main.tsx 在渲染前调用）。 */
export function initTheme(): void {
  applyDom(getTheme(), getAccent())
}

function persist(theme: ThemeMode, accent: AccentColor): void {
  write(THEME_KEY, theme)
  write(ACCENT_KEY, accent)
  applyDom(theme, accent)
  window.dispatchEvent(new CustomEvent(THEME_EVENT))
}

/** 主题 hook：读取当前值并暴露 setter（多组件实例经事件同步）。 */
export function useTheme(): {
  theme: ThemeMode
  accent: AccentColor
  setTheme: (theme: ThemeMode) => void
  setAccent: (accent: AccentColor) => void
  toggleTheme: () => void
} {
  const [theme, setThemeState] = useState<ThemeMode>(getTheme)
  const [accent, setAccentState] = useState<AccentColor>(getAccent)

  useEffect(() => {
    const sync = () => {
      setThemeState(getTheme())
      setAccentState(getAccent())
    }
    window.addEventListener(THEME_EVENT, sync)
    window.addEventListener('storage', sync) // 多标签页同步
    return () => {
      window.removeEventListener(THEME_EVENT, sync)
      window.removeEventListener('storage', sync)
    }
  }, [])

  const setTheme = useCallback((next: ThemeMode) => {
    persist(next, getAccent())
  }, [])

  const setAccent = useCallback((next: AccentColor) => {
    persist(getTheme(), next)
  }, [])

  const toggleTheme = useCallback(() => {
    persist(getTheme() === 'dark' ? 'light' : 'dark', getAccent())
  }, [])

  return { theme, accent, setTheme, setAccent, toggleTheme }
}
