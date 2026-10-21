import { computed, onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { ApiError, request, safeMessage } from '../api/client'
import { encryptLogin, encryptSmsLogin, encryptSmsSend } from '../api/loginEncryption'
import { useManualCaptcha } from './useManualCaptcha'
import type { AuthCapabilities, LoginCredentials, LoginMethod, QrChallenge, QrState, Session, SmsContact, SmsLoginCredentials, SmsSendResult } from '../api/types'

const anonymous = (): Session => ({ status: 'anonymous', authenticated: false, role: null, roles: [], message: null })

export function useGameSession() {
  const captcha = useManualCaptcha()
  const capabilities = shallowRef<AuthCapabilities | null>(null)
  const session = shallowRef<Session>(anonymous())
  const error = shallowRef<string | null>(null)
  const qrState = shallowRef<QrState | null>(null)
  const qrChallenge = shallowRef<QrChallenge | null>(null)
  const busy = shallowRef(false)
  const smsResendAfterSeconds = shallowRef(0)
  const qrBusy = shallowRef(false)
  let generation = 0
  let qrGeneration = 0
  let controller: AbortController | null = null
  let initializing = false
  let explicitlyLoggedOut = false
  let smsTimer: ReturnType<typeof setTimeout> | null = null
  let smsCooldownUntil = 0
  let pendingAuthChallenge: string | null = null
  const connected = computed(() => session.value.authenticated && session.value.status === 'connected')

  function available(method: LoginMethod) {
    return capabilities.value?.integration_ready === true && capabilities.value.methods.includes(method)
  }
  function clearQr() {
    qrGeneration++
    qrChallenge.value = null
    qrState.value = null
    qrBusy.value = false
  }
  function stopSmsTimer() {
    if (smsTimer !== null) clearTimeout(smsTimer)
    smsTimer = null
  }
  function clearSmsCooldown() {
    stopSmsTimer()
    smsCooldownUntil = 0
    smsResendAfterSeconds.value = 0
  }
  function resumeLogin() {
    stopSmsTimer()
    smsResendAfterSeconds.value = Math.max(0, Math.ceil((smsCooldownUntil - Date.now()) / 1000))
    if (smsResendAfterSeconds.value > 0) smsTimer = setTimeout(resumeLogin, 1000)
    else smsCooldownUntil = 0
  }
  function startSmsCooldown(seconds: number) {
    smsCooldownUntil = Date.now() + seconds * 1000
    resumeLogin()
  }
  function cancelPendingAuth() {
    const challengeId = pendingAuthChallenge
    pendingAuthChallenge = null
    if (challengeId) {
      void request(`/api/auth/pending/${challengeId}`, { method: 'DELETE', keepalive: true }).catch(() => {})
    }
  }
  async function loadLoginChallenge(signal: AbortSignal, isCurrent: () => boolean) {
    const challenge = await request<unknown>('/api/auth/login-challenge', { signal })
    if (isCurrent() && challenge && typeof challenge === 'object' && 'challenge_id' in challenge
      && typeof challenge.challenge_id === 'string' && /^[A-Za-z0-9_-]{32}$/.test(challenge.challenge_id)) {
      pendingAuthChallenge = challenge.challenge_id
    }
    return challenge
  }
  function cancelLogin() {
    cancelPendingAuth()
    generation++
    controller?.abort()
    controller = null
    captcha.stop()
    stopSmsTimer()
    busy.value = false
    initializing = false
    error.value = null
  }
  function invalidateSession() {
    cancelLogin()
    clearSmsCooldown()
    clearQr()
    session.value = { ...anonymous(), status: 'expired' }
  }
  function expire() {
    // An initialization owns state while old business requests may still finish.
    if (!initializing) invalidateSession()
  }
  function fail(reason: unknown) {
    if (reason instanceof ApiError && reason.status === 401) invalidateSession()
    else error.value = safeMessage(reason)
  }
  async function initialize() {
    cancelPendingAuth()
    controller?.abort()
    captcha.stop()
    const id = ++generation
    const operation = new AbortController()
    controller = operation
    const isCurrent = () => id === generation && !operation.signal.aborted
    initializing = true
    busy.value = true
    error.value = null
    try {
      const [capability, current] = await Promise.all([
        request<AuthCapabilities>('/api/auth/capabilities', { signal: operation.signal }),
        request<Session>('/api/session', { signal: operation.signal }),
      ])
      if (!isCurrent()) return
      capabilities.value = capability
      session.value = current
    } catch (reason) {
      if (isCurrent()) fail(reason)
    } finally {
      if (id === generation) {
        busy.value = false
        initializing = false
        if (controller === operation) controller = null
      }
    }
  }
  async function changeSession<T = Session>(path: string, body?: object | ((signal: AbortSignal, isCurrent: () => boolean) => Promise<object>), method = 'POST', timeoutMs?: number,
    accept: (value: T) => void = value => { session.value = value as Session; if (session.value.authenticated) clearSmsCooldown() }) {
    cancelPendingAuth()
    const id = ++generation
    controller?.abort()
    captcha.stop()
    initializing = false
    const operation = new AbortController()
    controller = operation
    const isCurrent = () => id === generation && !operation.signal.aborted
    clearQr()
    busy.value = true
    error.value = null
    const authenticating = path === '/api/auth/login' || path.startsWith('/api/auth/sms/')
    try {
      const payload = typeof body === 'function' ? await body(operation.signal, isCurrent) : body
      if (!isCurrent()) return
      const pendingSession = request<T>(path, { method, body: payload ? JSON.stringify(payload) : undefined, signal: operation.signal }, { timeoutMs })
      if (authenticating) captcha.start()
      const current = await pendingSession
      if (isCurrent()) accept(current)
    } catch (reason) {
      if (id === generation) {
        if (authenticating) {
          session.value = anonymous()
          error.value = safeMessage(reason)
        } else if (path === '/api/session' && method === 'DELETE'
          && !(reason instanceof ApiError && reason.status === 401)) {
          error.value = '退出未确认，请重试，自动任务可能仍在运行'
        } else fail(reason)
      }
    } finally {
      if (id === generation) {
        if (authenticating) { pendingAuthChallenge = null; captcha.stop() }
        busy.value = false
        if (controller === operation) controller = null
      }
    }
  }
  async function login(credentials: LoginCredentials) {
    if (busy.value && !initializing) return
    if (!available('account')) {
      error.value = capabilities.value?.message ?? '账号登录尚未接通'
      return
    }
    explicitlyLoggedOut = false
    await changeSession('/api/auth/login', async (signal, isCurrent) => {
      const challenge = await loadLoginChallenge(signal, isCurrent)
      return encryptLogin(credentials, challenge, isCurrent)
    }, 'POST', 300_000)
  }
  function validContact(contact: SmsContact) {
    return /^\d{5,20}$/.test(contact.mobile) && /^[1-9][0-9]{0,3}$/.test(contact.country_code)
  }
  function smsAvailable() {
    if (available('sms')) return true
    error.value = capabilities.value?.message ?? '验证码登录尚未接通'
    return false
  }
  async function sendSms(contact: SmsContact) {
    resumeLogin()
    if ((busy.value && !initializing) || smsResendAfterSeconds.value > 0) return
    if (!smsAvailable()) return
    if (!validContact(contact)) { error.value = '请输入有效的国家码和手机号'; return }
    const input = { mobile: contact.mobile, country_code: contact.country_code }
    explicitlyLoggedOut = false
    await changeSession<SmsSendResult>('/api/auth/sms/send', async (signal, isCurrent) => {
      const challenge = await loadLoginChallenge(signal, isCurrent)
      return encryptSmsSend(input, challenge, isCurrent)
    }, 'POST', 300_000, result => {
      if (!Number.isInteger(result?.resend_after_seconds) || result.resend_after_seconds < 1 || result.resend_after_seconds > 3600) {
        throw new ApiError(502, 'INVALID_RESPONSE', '验证码发送结果无法确认，请稍后重试')
      }
      startSmsCooldown(result.resend_after_seconds)
    })
  }
  async function loginSms(credentials: SmsLoginCredentials) {
    if (busy.value && !initializing) return
    if (!smsAvailable()) return
    if (!validContact(credentials) || !/^\d{6}$/.test(credentials.otp)) { error.value = '请输入有效的手机号和验证码'; return }
    const input = { mobile: credentials.mobile, country_code: credentials.country_code, otp: credentials.otp }
    explicitlyLoggedOut = false
    await changeSession('/api/auth/sms/login', async (signal, isCurrent) => {
      const challenge = await loadLoginChallenge(signal, isCurrent)
      return encryptSmsLogin(input, challenge, isCurrent)
    }, 'POST', 300_000, current => {
      clearSmsCooldown()
      session.value = current
    })
  }
  async function selectRole(roleId: string) {
    if (!session.value.roles.some(role => role.id === roleId)) return
    await changeSession('/api/session/role', { role_id: roleId })
  }
  async function logout() {
    if (busy.value && !initializing) return
    explicitlyLoggedOut = true
    clearSmsCooldown()
    await changeSession('/api/session', undefined, 'DELETE', initializing ? 45_000 : undefined)
  }
  async function createQr() {
    if (!available('qr')) {
      error.value = capabilities.value?.message ?? '扫码登录尚未接通'
      return
    }
    explicitlyLoggedOut = false
    clearQr()
    const id = qrGeneration
    const sessionId = generation
    qrBusy.value = true
    error.value = null
    try {
      const challenge = await request<QrChallenge>('/api/auth/qr', { method: 'POST' })
      if (id !== qrGeneration || sessionId !== generation) return
      qrChallenge.value = challenge
      qrState.value = { status: 'waiting', session: null, message: null }
    } catch (reason) {
      if (id === qrGeneration && sessionId === generation) fail(reason)
    } finally {
      if (id === qrGeneration) qrBusy.value = false
    }
  }
  async function checkQr() {
    if (!qrChallenge.value || qrBusy.value) return
    const id = qrGeneration
    const sessionId = generation
    qrBusy.value = true
    try {
      const state = await request<QrState>(`/api/auth/qr/${encodeURIComponent(qrChallenge.value.challenge_id)}`)
      if (id !== qrGeneration || sessionId !== generation) return
      qrState.value = state
      if (state.status === 'confirmed' && state.session) session.value = state.session
    } catch (reason) {
      if (id === qrGeneration && sessionId === generation) fail(reason)
    } finally {
      if (id === qrGeneration) qrBusy.value = false
    }
  }
  function resume() {
    if (!busy.value && !qrBusy.value && !qrChallenge.value && !explicitlyLoggedOut) void initialize()
  }
  function onVisibilityChange() {
    if (document.visibilityState === 'visible') resume()
  }
  function onPageShow(event: PageTransitionEvent) {
    if (event.persisted) resume()
  }
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('pageshow', onPageShow)
  onScopeDispose(() => {
    document.removeEventListener('visibilitychange', onVisibilityChange)
    window.removeEventListener('pageshow', onPageShow)
    cancelLogin(); clearQr()
  })
  return {
    capabilities: shallowReadonly(capabilities), session: shallowReadonly(session), error: shallowReadonly(error),
    busy: shallowReadonly(busy), smsResendAfterSeconds: shallowReadonly(smsResendAfterSeconds), qrBusy: shallowReadonly(qrBusy), qrChallenge: shallowReadonly(qrChallenge), qrState: shallowReadonly(qrState),
    connected, initialize, login, sendSms, loginSms, cancelLogin, resumeLogin, selectRole, logout, createQr, checkQr, expire,
    captchaChallenge: captcha.challenge, captchaCancelling: captcha.cancelling, captchaError: captcha.error,
    sendCaptchaInput: captcha.input, cancelCaptcha: captcha.cancel,
  }
}
