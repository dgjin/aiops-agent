import { useEffect, useState } from 'react'
import type { RefObject } from 'react'

/** 每秒触发重渲染的时钟 hook（倒计时显示用；时间基准统一走 serverNow()）。 */
export function useNowTick(intervalMs = 1000): void {
  const [, setTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), intervalMs)
    return () => clearInterval(timer)
  }, [intervalMs])
}

/** 点击元素外部 / 按 Esc 时回调（下拉菜单与浮层通用）。 */
export function useDismiss(
  ref: RefObject<HTMLElement | null>,
  onClose: () => void,
  active = true,
): void {
  useEffect(() => {
    if (!active) return
    const onPointer = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [ref, onClose, active])
}
