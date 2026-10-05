import type { ReactNode } from 'react'
import { X } from 'lucide-react'

interface ModalProps {
  open: boolean
  title: string
  /** 宽度档：sm=表单窄弹窗（默认），md=中等（用户/会话等内容），lg=宽（帮助/详情）。 */
  size?: 'sm' | 'md' | 'lg'
  onClose: () => void
  children: ReactNode
  /** 底部操作区（按钮等）；缺省时只渲染内容。 */
  footer?: ReactNode
}

const SIZE_CLASS = { sm: 'max-w-md', md: 'max-w-xl', lg: 'max-w-3xl' } as const

/** 通用弹窗（带关闭按钮的内容模态；表单/说明类场景，与 ConfirmDialog 区分）。 */
export function Modal({ open, title, size = 'sm', onClose, children, footer }: ModalProps) {
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className={`w-full ${SIZE_CLASS[size]} rounded-xl border border-line bg-panel shadow-2xl`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <div className="text-sm font-medium text-ink">{title}</div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="rounded-md p-1 text-idle transition-colors hover:bg-elevated hover:text-ink"
          >
            <X size={15} />
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4 text-sm text-ink">{children}</div>
        {footer && (
          <div className="flex justify-end gap-2 border-t border-line px-5 py-3.5">{footer}</div>
        )}
      </div>
    </div>
  )
}

/** 表单字段通用样式（输入框 / 下拉）。 */
export const fieldClass =
  'w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink placeholder:text-idle focus:border-accent/60 focus:outline-none'

/** 主按钮 / 次级按钮样式（弹窗内表单提交共用）。 */
export const primaryButtonClass =
  'rounded-lg bg-accent/90 px-3.5 py-1.5 text-sm font-medium text-canvas transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50'

export const secondaryButtonClass =
  'rounded-lg border border-line px-3.5 py-1.5 text-sm text-muted transition-colors hover:bg-elevated hover:text-ink'
