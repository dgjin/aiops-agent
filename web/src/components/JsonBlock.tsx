/** JSON 原文展示（等宽、可滚动）。 */
export function JsonBlock({ data, className }: { data: unknown; className?: string }) {
  return (
    <pre
      className={
        'overflow-x-auto rounded-lg border border-line bg-canvas/60 p-3 font-mono text-xs leading-5 text-muted ' +
        (className ?? '')
      }
    >
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}
