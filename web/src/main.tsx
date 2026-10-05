import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ApiError } from './lib/api'
import { initTheme } from './lib/theme'
import './styles.css'

// 首屏前应用已保存的主题偏好（避免浅色用户先看到深色闪烁）
initTheme()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 4xx 是确定性错误（未授权 / 权限不足 / 鉴权未配置），重试既无意义，
      // 又会让页面先卡在「加载中…」——直接失败，让各页立刻显示对应提示。
      retry: (failureCount, error) =>
        error instanceof ApiError && error.status >= 400 && error.status < 500
          ? false
          : failureCount < 1,
      refetchOnWindowFocus: false,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
