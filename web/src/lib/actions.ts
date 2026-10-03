/** 写操作编排：确认层 → mutation → 缓存失效 → 提示（设计方案 6.3、7.3）。 */

import type { ReactNode } from 'react'
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

export interface ActionSpec {
  title: string
  tone?: 'default' | 'danger'
  confirmLabel?: string
  detail?: ReactNode
  run: () => Promise<unknown>
  success: string
}

export function useWriteAction() {
  const queryClient = useQueryClient()
  const [spec, setSpec] = useState<ActionSpec | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const open = (next: ActionSpec) => {
    setError(null)
    setSpec(next)
  }

  const close = () => {
    if (busy) return
    setSpec(null)
    setError(null)
  }

  const confirm = async () => {
    if (!spec) return
    setBusy(true)
    setError(null)
    try {
      await spec.run()
      setSpec(null)
      setToast(spec.success)
      setTimeout(() => setToast(null), 4000)
      await queryClient.invalidateQueries()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return { spec, open, close, confirm, busy, error, toast }
}
