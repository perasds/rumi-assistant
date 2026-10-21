export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public retryable = false) {
    super(message)
  }
}

export async function request<T>(path: string, init: RequestInit = {}, options: { timeoutMs?: number } = {}): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  init.signal?.addEventListener('abort', abort, { once: true })
  if (init.signal?.aborted) controller.abort()
  const timeout = setTimeout(abort, options.timeoutMs ?? 15_000)
  try {
    const headers = new Headers(init.headers)
    headers.set('Accept', 'application/json')
    if (init.body) headers.set('Content-Type', 'application/json')
    const response = await fetch(path, {
      ...init, method: init.method ?? 'GET', headers,
      credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
    })
    const body: unknown = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = body && typeof body === 'object' && 'error' in body ? body.error : null
      if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string'
        && 'code' in detail && typeof detail.code === 'string') {
        throw new ApiError(response.status, detail.code, detail.message, 'retryable' in detail && detail.retryable === true)
      }
      throw new ApiError(response.status, 'HTTP_ERROR', '服务请求失败，请稍后重试')
    }
    if (body === null) throw new ApiError(502, 'INVALID_RESPONSE', '服务返回的数据无法读取，请稍后重试')
    return body as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (init.signal?.aborted) throw error
    if (controller.signal.aborted) throw new ApiError(0, 'TIMEOUT', '请求超时，请稍后重试', true)
    throw new ApiError(0, 'NETWORK_ERROR', '无法连接服务，请检查后端是否启动', true)
  } finally {
    clearTimeout(timeout)
    init.signal?.removeEventListener('abort', abort)
  }
}

export const safeMessage = (error: unknown) => error instanceof ApiError ? error.message : '请求失败，请稍后重试'
