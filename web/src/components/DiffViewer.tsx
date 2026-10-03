import { cn } from '../lib/format'

/** unified diff 行级渲染（等宽 + 行号 + 增删着色）。 */
export function DiffViewer({ diff, className }: { diff: string; className?: string }) {
  const lines = diff.split('\n')
  return (
    <div
      className={cn(
        'overflow-x-auto rounded-lg border border-line bg-canvas/70 font-mono text-xs leading-6',
        className,
      )}
    >
      {lines.map((line, index) => {
        let cls = 'text-muted'
        if (line.startsWith('+++') || line.startsWith('---')) cls = 'text-info'
        else if (line.startsWith('@@')) cls = 'bg-info/10 text-info'
        else if (line.startsWith('+')) cls = 'bg-ok/10 text-ok'
        else if (line.startsWith('-')) cls = 'bg-danger/10 text-danger'
        return (
          <div key={index} className="flex min-w-max">
            <span className="w-10 shrink-0 select-none pr-3 text-right text-idle">{index + 1}</span>
            <span className={cn('whitespace-pre pr-4', cls)}>{line || ' '}</span>
          </div>
        )
      })}
    </div>
  )
}
