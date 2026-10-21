import { ApiError } from './client'
import type { LoginCredentials, SmsContact, SmsLoginCredentials } from './types'

interface LoginChallenge { challenge_id: string; public_key: string; algorithm: string; expires_at: string }
export interface EncryptedLogin { challenge_id: string; encrypted_key: string; iv: string; ciphertext: string }
const failure = () => new ApiError(0, 'LOGIN_ENCRYPTION_FAILED', '登录加密准备失败，请重试')
const base64 = (data: ArrayBuffer | Uint8Array) => {
  let binary = ''
  for (const byte of new Uint8Array(data)) binary += String.fromCharCode(byte)
  return btoa(binary)
}
function validateChallenge(value: unknown): LoginChallenge {
  if (!value || typeof value !== 'object') throw failure()
  const challenge = value as Partial<LoginChallenge>
  if (typeof challenge.challenge_id !== 'string' || !/^[A-Za-z0-9_-]{32}$/.test(challenge.challenge_id)
    || challenge.algorithm !== 'RSA-OAEP-256+A256GCM'
    || typeof challenge.public_key !== 'string' || challenge.public_key.length > 2048
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(challenge.public_key)
    || !challenge.public_key.length || typeof challenge.expires_at !== 'string'
    || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(challenge.expires_at)
    || !Number.isFinite(Date.parse(challenge.expires_at)) || Date.parse(challenge.expires_at) <= Date.now()) throw failure()
  return challenge as LoginChallenge
}

export function encryptLogin(credentials: LoginCredentials, value: unknown, isCurrent: () => boolean): Promise<EncryptedLogin> {
  return encryptPayload({ account: credentials.account, password: credentials.password }, value, isCurrent)
}
export function encryptSmsSend(contact: SmsContact, value: unknown, isCurrent: () => boolean): Promise<EncryptedLogin> {
  return encryptPayload({ mobile: contact.mobile, country_code: contact.country_code }, value, isCurrent)
}
export function encryptSmsLogin(credentials: SmsLoginCredentials, value: unknown, isCurrent: () => boolean): Promise<EncryptedLogin> {
  return encryptPayload({ mobile: credentials.mobile, country_code: credentials.country_code, otp: credentials.otp }, value, isCurrent)
}
async function encryptPayload(payload: object, value: unknown, isCurrent: () => boolean): Promise<EncryptedLogin> {
  const checkCurrent = () => { if (!isCurrent()) throw new DOMException('Aborted', 'AbortError') }
  let rawKey: Uint8Array<ArrayBuffer> | undefined
  let plaintext: Uint8Array<ArrayBuffer> | undefined
  try {
    checkCurrent()
    const challenge = validateChallenge(value)
    if (!globalThis.crypto?.subtle || !globalThis.crypto.getRandomValues) throw failure()
    const crypto = globalThis.crypto
    const publicKey = await crypto.subtle.importKey('spki', Uint8Array.from(atob(challenge.public_key), char => char.charCodeAt(0)),
      { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt'])
    checkCurrent()
    const algorithm = publicKey.algorithm as RsaHashedKeyAlgorithm
    if (publicKey.type !== 'public' || algorithm.name !== 'RSA-OAEP' || algorithm.modulusLength !== 2048
      || algorithm.hash.name !== 'SHA-256' || base64(algorithm.publicExponent) !== 'AQAB') throw failure()
    rawKey = crypto.getRandomValues(new Uint8Array(32))
    const iv = crypto.getRandomValues(new Uint8Array(12))
    const aesKey = await crypto.subtle.importKey('raw', rawKey, { name: 'AES-GCM' }, false, ['encrypt'])
    checkCurrent()
    const encryptedKey = await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, publicKey, rawKey)
    checkCurrent()
    plaintext = new TextEncoder().encode(JSON.stringify(payload))
    const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, tagLength: 128,
      additionalData: new TextEncoder().encode(`rumi-login:v1:${challenge.challenge_id}`) }, aesKey, plaintext)
    checkCurrent()
    if (Date.parse(challenge.expires_at) <= Date.now()) throw failure()
    return { challenge_id: challenge.challenge_id, encrypted_key: base64(encryptedKey), iv: base64(iv), ciphertext: base64(ciphertext) }
  } catch {
    checkCurrent()
    throw failure()
  } finally {
    rawKey?.fill(0)
    plaintext?.fill(0)
  }
}
