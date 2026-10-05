/** AIOps 品牌标识（「脉冲徽章」概念）：圆角方徽 + 心电图脉冲线 + 端点节点。
 *
 * - 描边与节点取 var(--c-accent)：随强调色实时变化（SVG gradient 无法解析
 *   CSS 变量在部分浏览器有差异，故用两段纯色 + 透明度渐变模拟）；
 * - 脉冲线用 currentColor：深浅主题下由外层 text-* 控制（默认 text-ink）。
 */
export function Logo({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      className={className}
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
