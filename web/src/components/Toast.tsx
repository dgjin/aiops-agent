/** 操作成功提示（右下角浮层，自动消失由调用方控制）。 */
export function Toast({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <div className="fixed bottom-6 right-6 z-50 rounded-lg border border-accent/40 bg-elevated px-4 py-2.5 text-sm text-accent shadow-xl">
      {message}
    </div>
  )
}
