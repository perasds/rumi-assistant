import { generateKeyPairSync, webcrypto } from 'node:crypto'

// 每个测试进程现场生成，仅供合成登录请求使用。
const keys = generateKeyPairSync('rsa', { modulusLength: 2048, publicExponent: 0x10001,
  publicKeyEncoding: { type: 'spki', format: 'der' }, privateKeyEncoding: { type: 'pkcs8', format: 'der' } })
export const testCrypto = webcrypto as unknown as Crypto
export function loginChallenge() {
  return { challenge_id: 'synthetic_challenge_id_012345678', public_key: keys.publicKey.toString('base64'),
    algorithm: 'RSA-OAEP-256+A256GCM', expires_at: new Date(Date.now() + 120_000).toISOString() }
}
export interface LoginEnvelope { challenge_id: string; encrypted_key: string; iv: string; ciphertext: string }
const bytes = (value: string) => new Uint8Array(Buffer.from(value, 'base64'))
export async function decryptLogin(body: LoginEnvelope) {
  const key = await testCrypto.subtle.importKey('pkcs8', new Uint8Array(keys.privateKey),
    { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['decrypt'])
  const raw = await testCrypto.subtle.decrypt({ name: 'RSA-OAEP' }, key, bytes(body.encrypted_key))
  const aes = await testCrypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['decrypt'])
  const plaintext = await testCrypto.subtle.decrypt({ name: 'AES-GCM', iv: bytes(body.iv), tagLength: 128,
    additionalData: new TextEncoder().encode(`rumi-login:v1:${body.challenge_id}`) }, aes, bytes(body.ciphertext))
  return JSON.parse(new TextDecoder().decode(plaintext))
}
