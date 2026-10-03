/** 空状态占位（含引导文案）。 */
export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-line px-6 py-12 text-center">
      <div className="text-sm text-muted">{title}</div>
      {hint && <div className="mt-1.5 text-xs text-idle">{hint}</div>}
    </div>
  )
}
