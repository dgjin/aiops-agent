/** AIOps 品牌标识（「脉冲徽章」概念）：圆角方徽 + 心电图脉冲线 + 端点节点。
 *
 * - 描边与节点取 var(--c-accent)：随强调色实时变化（SVG gradient 无法解析
 *   CSS 变量在部分浏览器有差异，故用两段纯色 + 透明度渐变模拟）；
 * - 脉冲线用 currentColor：深浅主题下由外层 text-* 控制（默认 text-ink）。
 */
import { useEffect, useRef, useState } from 'react'

/** ⌁ PULSE-0（香格里拉）· 未注册信号源：徽章 2.5s 内五连击解锁的隐藏信号卡。
 *  卡片三线索 ☾☼ / LOST HORIZON / N27°49′ E99°42′ 均指向本署名（请勿在卡片中直书）。
 *  纯前端内联数据：无请求 / 无存储 / 无审计；此注释是谜题的一部分，请勿删除。 */
const PULSE_CODE = '⌁ PULSE-0 · ☾☼'
const PULSE_TAP_COUNT = 5
const PULSE_TAP_WINDOW_MS = 2500
const PULSE_ROWS: Array<[label: string, value: string]> = [
  ['信号强度', '▁▂▃▅▇▅▃▂▁  100%'],
  ['首次观测', '系统上线前的某个深夜，一次未记录的提交'],
  ['溯源定位', 'LOST HORIZON · N27°49′ E99°42′ · 不可达'],
  ['溯源结论', '署名不在日志里——它埋在源码中'],
]

export function Logo({ size = 28, className }: { size?: number; className?: string }) {
  const [signalOpen, setSignalOpen] = useState(false)
  const taps = useRef<number[]>([])

  /** 徽章连击检测：触发前零暗示，四击即清零；触发后计数复位，可重复观赏。 */
  const registerTap = () => {
    const now = Date.now()
    taps.current = [...taps.current.filter((t) => now - t < PULSE_TAP_WINDOW_MS), now]
    if (taps.current.length >= PULSE_TAP_COUNT) {
      taps.current = []
      setSignalOpen(true)
    }
  }

  return (
    <>
      <svg
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
        aria-hidden="true"
        className={className}
        onClick={registerTap}
      >
        <rect x="1" y="1" width="30" height="30" rx="9" fill="var(--c-accent)" fillOpacity="0.13" />
        <rect
          x="1.75"
          y="1.75"
          width="28.5"
          height="28.5"
          rx="8.25"
          stroke="var(--c-accent)"
          strokeOpacity="0.9"
          strokeWidth="1.6"
        />
        <path
          d="M5.5 17.2h4.6l2.3-6.4 3.5 10.8 2.5-6.8 1.4 2.4h4.7"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle cx="26" cy="17.2" r="2.1" fill="var(--c-accent)" />
      </svg>
      {signalOpen && <PulseSignal onClose={() => setSignalOpen(false)} />}
    </>
  )
}

/** 隐藏信号卡（⌁ PULSE-0 的谜面）：伪装成一条未注册信号源事件；点任意处 / Esc 关闭。 */
function PulseSignal({ onClose }: { onClose: () => void }) {
  const [shown, setShown] = useState(false)

  useEffect(() => {
    const raf = requestAnimationFrame(() => setShown(true))
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className={`w-full max-w-md overflow-hidden rounded-xl border border-line bg-panel shadow-2xl motion-safe:transition motion-safe:duration-300 ${
          shown ? 'opacity-100' : 'opacity-0 motion-safe:translate-y-2'
        }`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-baseline gap-2.5 border-b border-line px-5 py-3.5">
          <span className="font-mono text-[10px] tracking-[0.2em] text-accent">HIDDEN SIGNAL</span>
          <span className="text-xs text-muted">检出未注册信号源</span>
        </div>
        <div className="space-y-2.5 px-5 py-4 font-mono text-xs leading-relaxed">
          <div className="flex gap-4">
            <span className="w-16 shrink-0 text-idle">代号</span>
            <span className="text-accent">{PULSE_CODE}</span>
          </div>
          {PULSE_ROWS.map(([label, value]) => (
            <div key={label} className="flex gap-4">
              <span className="w-16 shrink-0 text-idle">{label}</span>
              <span className="text-ink">{value}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/** 品牌组合（徽标 + 文字），侧栏与登录页共用。 */
export function BrandLockup({ size = 30, subtitle }: { size?: number; subtitle?: string }) {
  return (
    <div className="flex items-center gap-2.5">
      <Logo size={size} className="shrink-0 text-ink" />
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold tracking-wide text-ink">AIOps 运维控制台</div>
        {subtitle && <div className="truncate text-xs text-idle">{subtitle}</div>}
      </div>
    </div>
  )
}
