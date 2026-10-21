import { onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { ApiError, request } from '../api/client'
import type { ManualCaptchaFrame, ManualCaptchaInput, ManualCaptchaState } from '../api/types'

interface Operation {
  controller: AbortController
  timer: ReturnType<typeof setTimeout> | null
  polling: Set<number>
  followup: { image: string; until: number } | null
  cancelling: boolean
  revision: number
  pendingInputs: number
  inputs: Promise<void>
}
const inputTypes = new Set(['click', 'pointerdown', 'pointermove', 'pointerup'])
const imagePrefix = 'data:image/png;base64,'
function validate(value: ManualCaptchaState): ManualCaptchaState {
  if (value?.status === 'waiting') return value
  if (value?.status !== 'active' || !/^[A-Za-z0-9_-]{32}$/.test(value.challenge_id)
    || value.width !== 520 || value.height !== 900 || typeof value.image !== 'string'
    || value.image.length > 4 * 1024 * 1024 + imagePrefix.length || !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(value.image)) {
    throw new ApiError(502, 'INVALID_RESPONSE', '验证码暂时无法加载，请稍后重试')
  }
  return value
}

export function useManualCaptcha() {
  const challenge = shallowRef<ManualCaptchaFrame | null>(null)
  const cancelling = shallowRef(false)
  const error = shallowRef<string | null>(null)
  let current: Operation | null = null
  let disposed = false
  const valid = (operation: Operation) => current === operation && !operation.controller.signal.aborted
  function clearTimer(operation: Operation) {
    if (operation.timer !== null) clearTimeout(operation.timer)
    operation.timer = null
  }
  function stop() {
    const operation = current
    current = null
    if (operation) { clearTimer(operation); operation.controller.abort() }
    challenge.value = null
    cancelling.value = false
    error.value = null
  }
  function schedule(operation: Operation, delay = 600) {
    clearTimer(operation)
    if (valid(operation) && !operation.cancelling && operation.pendingInputs === 0) {
      operation.timer = setTimeout(() => { operation.timer = null; void poll(operation) }, delay)
    }
  }
  function nextDelay(operation: Operation) {
    if (operation.followup && Date.now() >= operation.followup.until) operation.followup = null
    return operation.followup ? 100 : 600
  }
  async function poll(operation: Operation) {
    if (!valid(operation) || operation.cancelling || operation.pendingInputs !== 0) return
    if (operation.followup && Date.now() >= operation.followup.until) {
      operation.followup = null
      schedule(operation)
      return
    }
    const revision = operation.revision
    if (operation.polling.has(revision)) return
    // A previous screenshot may still be downloading. Leave it running: aborting
    // the server command would close the human verification session.
    if (operation.polling.size >= 2) { schedule(operation, nextDelay(operation)); return }
    clearTimer(operation)
    operation.polling.add(revision)
    try {
      const value = validate(await request<ManualCaptchaState>('/api/auth/captcha', { signal: operation.controller.signal }))
      if (valid(operation) && !operation.cancelling && revision === operation.revision && operation.pendingInputs === 0) {
        challenge.value = value.status === 'active' ? value : null
        error.value = null
        if (value.status === 'waiting' || value.image !== operation.followup?.image) operation.followup = null
      }
    } catch (reason) {
      if (valid(operation) && !operation.cancelling && revision === operation.revision) {
        // The long login response owns authentication errors. A transient
        // captcha-session 401 must never replace its result or expire it.
        if (reason instanceof ApiError && reason.status === 401) stop()
        else error.value = '验证码暂时无法加载，请稍后重试'
      }
    } finally {
      operation.polling.delete(revision)
      if (valid(operation) && revision === operation.revision) schedule(operation, nextDelay(operation))
    }
  }
  function start() {
    stop()
    if (disposed) return
    const operation: Operation = { controller: new AbortController(), timer: null, polling: new Set(),
      followup: null, cancelling: false, revision: 0, pendingInputs: 0, inputs: Promise.resolve() }
    current = operation
    schedule(operation)
  }
  function input(challengeId: string, value: ManualCaptchaInput): Promise<void> {
    const operation = current, frame = challenge.value
    if (!operation || !valid(operation) || operation.cancelling || !frame || frame.challenge_id !== challengeId
      || !inputTypes.has(value.type) || !Number.isFinite(value.x) || !Number.isFinite(value.y)
      || value.x < 0 || value.y < 0 || value.x >= frame.width || value.y >= frame.height) return Promise.resolve()
    const payload = { type: value.type, x: value.x, y: value.y }
    clearTimer(operation)
    operation.followup = null
    const revision = ++operation.revision
    operation.pendingInputs++
    let acknowledged = false, receivedFeedback = false, beforeImage = frame.image
    operation.inputs = operation.inputs.then(async () => {
      if (!valid(operation) || operation.cancelling || challenge.value?.challenge_id !== challengeId) return
      beforeImage = challenge.value.image
      try {
        const result = await request<{ frame?: ManualCaptchaState }>(`/api/auth/captcha/${challengeId}/input`, {
          method: 'POST', body: JSON.stringify(payload), signal: operation.controller.signal,
        })
        acknowledged = true
        if (valid(operation) && !operation.cancelling && revision === operation.revision && result?.frame !== undefined) {
          const next = validate(result.frame)
          if (next.status === 'active' && next.challenge_id !== challengeId) throw new Error('INVALID_FRAME')
          challenge.value = next.status === 'active' ? next : null
          error.value = null
          receivedFeedback = next.status === 'waiting' || next.image !== beforeImage
        }
      } catch (reason) {
        if (valid(operation) && !operation.cancelling && revision === operation.revision) {
          if (reason instanceof ApiError && reason.status === 401) stop()
          else error.value = acknowledged ? '验证码图片更新失败，请稍后重试' : '验证码操作未确认，请重试'
        }
      }
    }).finally(() => {
      operation.pendingInputs--
      if (valid(operation) && !operation.cancelling && revision === operation.revision && (value.type === 'click' || value.type === 'pointerup')) {
        if (acknowledged && !receivedFeedback) operation.followup = { image: beforeImage, until: Date.now() + 1500 }
        schedule(operation, receivedFeedback ? 600 : 0)
      }
    })
    return operation.inputs
  }
  async function cancel(challengeId: string) {
    const operation = current
    if (!operation || !valid(operation) || operation.cancelling || challenge.value?.challenge_id !== challengeId) return
    operation.cancelling = true
    operation.followup = null
    cancelling.value = true
    clearTimer(operation)
    try {
      await request(`/api/auth/captcha/${challengeId}`, { method: 'DELETE', signal: operation.controller.signal })
      if (valid(operation)) stop()
    } catch (reason) {
      if (valid(operation)) {
        if (reason instanceof ApiError && reason.status === 401) stop()
        else {
          operation.cancelling = false
          cancelling.value = false
          error.value = '取消验证尚未确认，请重试'
          schedule(operation)
        }
      }
    }
  }
  onScopeDispose(() => { disposed = true; stop() })
  return { challenge: shallowReadonly(challenge), cancelling: shallowReadonly(cancelling), error: shallowReadonly(error), start, stop, input, cancel }
}
