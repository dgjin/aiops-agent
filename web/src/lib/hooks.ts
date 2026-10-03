import { useEffect, useState } from 'react'

/** 每秒触发重渲染的时钟 hook（倒计时显示用；时间基准统一走 serverNow()）。 */
export function useNowTick(intervalMs = 1000): void {
  const [, setTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), intervalMs)
    return () => clearInterval(timer)
  }, [intervalMs])
}
