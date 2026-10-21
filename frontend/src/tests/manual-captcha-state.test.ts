import { effectScope } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useManualCaptcha } from '../composables/useManualCaptcha'

const scopes: ReturnType<typeof effectScope>[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
const id = 'c'.repeat(31) + '1'
const otherId = 'c'.repeat(31) + '2'
const frame = (challenge_id = id) => ({ status: 'active', challenge_id, width: 520, height: 900, image: 'data:image/png;base64,iVBORw0KGgo=' })
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function pending() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>(accept => { resolve = accept })
  return { promise, resolve: (body: unknown, status = 200) => resolve(json(body, status)) }
}
function setup(fetcher: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetcher)
  const scope = effectScope()
  scopes.push(scope)
  return { captcha: scope.run(useManualCaptcha)!, scope }
}
async function active() {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(json(frame())))
  const setupResult = setup(fetcher)
  setupResult.captcha.start()
  await vi.advanceTimersByTimeAsync(600)
  expect(setupResult.captcha.challenge.value?.challenge_id).toBe(id)
  return { ...setupResult, fetcher }
}

describe('网页人工验证码状态', () => {
  it.each(['click', 'pointerup'] as const)('%s回帧仍是旧图时立即取图，100ms追踪到变化后恢复600ms', async type => {
    const { captcha, fetcher } = await active()
    const updated = { ...frame(), image: 'data:image/png;base64,iVBORw0KGgp1cGRhdGVk' }
    fetcher.mockResolvedValueOnce(json({ status: 'ok', frame: frame() }))
      .mockResolvedValueOnce(json(frame())).mockResolvedValue(json(updated))
    await captcha.input(id, { type, x: 100, y: 200 })
    await vi.advanceTimersByTimeAsync(0)
    expect(fetcher).toHaveBeenCalledTimes(3)
    expect(captcha.challenge.value).toEqual(frame())
    await vi.advanceTimersByTimeAsync(99)
    expect(fetcher).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(captcha.challenge.value).toEqual(updated)
    expect(fetcher).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(599)
    expect(fetcher).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetcher).toHaveBeenCalledTimes(5)
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
  })

  it('800ms旧GET在途时仍能跟踪点击后的变化，旧GET不推迟新计时器', async () => {
    const { captcha, fetcher } = await active()
    const stale = pending()
    const updated = { ...frame(), image: 'data:image/png;base64,iVBORw0KGgp1cGRhdGVk' }
    fetcher.mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(json({ status: 'ok', frame: frame() }))
      .mockResolvedValueOnce(json(frame())).mockResolvedValue(json(updated))
    await vi.advanceTimersByTimeAsync(600)
    const oldSignal = fetcher.mock.calls[1]![1].signal as AbortSignal
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    await vi.advanceTimersByTimeAsync(100)
    expect(captcha.challenge.value).toEqual(updated)
    expect(oldSignal.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(700)
    stale.resolve(frame())
    await flushPromises()
    expect(captcha.challenge.value).toEqual(updated)
    const calls = fetcher.mock.calls.length
    await vi.advanceTimersByTimeAsync(499)
    expect(fetcher).toHaveBeenCalledTimes(calls)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetcher).toHaveBeenCalledTimes(calls + 1)
  })

  it('旧版无frame的ACK同样追踪，1.5秒仍无变化后停止快速取图', async () => {
    const { captcha, fetcher } = await active()
    fetcher.mockResolvedValueOnce(json({ status: 'ok' })).mockResolvedValue(json(frame()))
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    await vi.advanceTimersByTimeAsync(1499)
    expect(fetcher).toHaveBeenCalledTimes(17) // 初次GET、一次POST、0至1400ms的15次追踪GET。
    await vi.advanceTimersByTimeAsync(600)
    expect(fetcher).toHaveBeenCalledTimes(17)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetcher).toHaveBeenCalledTimes(18)
    await vi.advanceTimersByTimeAsync(600)
    expect(fetcher).toHaveBeenCalledTimes(19)
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
  })

  it('追踪返回waiting后立即恢复常规频率', async () => {
    const { captcha, fetcher } = await active()
    fetcher.mockResolvedValueOnce(json({ status: 'ok' })).mockResolvedValue(json({ status: 'waiting' }))
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    await vi.advanceTimersByTimeAsync(0)
    expect(captcha.challenge.value).toBeNull()
    await vi.advanceTimersByTimeAsync(599)
    expect(fetcher).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetcher).toHaveBeenCalledTimes(4)
  })

  it('追踪取消后忽略迟到响应，不恢复轮询或重发操作', async () => {
    const { captcha, fetcher } = await active()
    const late = pending()
    fetcher.mockResolvedValueOnce(json({ status: 'ok', frame: frame() }))
      .mockReturnValueOnce(late.promise).mockResolvedValue(json({ status: 'ok' }))
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    await vi.advanceTimersByTimeAsync(0)
    expect(fetcher).toHaveBeenCalledTimes(3)
    await captcha.cancel(id)
    late.resolve(frame(otherId)); await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(captcha.challenge.value).toBeNull()
    expect(fetcher).toHaveBeenCalledTimes(4)
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
  })

  it('GET总数至多两个，同一输入版本不重叠，旧请求释放容量不改新计时器', async () => {
    const { captcha, fetcher } = await active()
    const gets: ReturnType<typeof pending>[] = []
    fetcher.mockImplementation((_path: string, init: RequestInit) => {
      if (init.method === 'POST') return Promise.resolve(json({ status: 'ok' }))
      const result = pending(); gets.push(result); return result.promise
    })
    await vi.advanceTimersByTimeAsync(600) // 输入前的旧GET。
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    await vi.advanceTimersByTimeAsync(0)
    expect(gets).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(500)
    expect(gets).toHaveLength(2) // 同一版本已有GET，不能重复发送。
    await captcha.input(id, { type: 'click', x: 110, y: 210 })
    await vi.advanceTimersByTimeAsync(0)
    expect(gets).toHaveLength(2) // 两个旧版本已占满容量。
    gets[0]!.resolve(frame()); await flushPromises()
    await vi.advanceTimersByTimeAsync(99)
    expect(gets).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(gets).toHaveLength(3) // 只在原计划时间重试，当前仍只有两个在途GET。
    await vi.advanceTimersByTimeAsync(500)
    expect(gets).toHaveLength(3)
  })

  it('旧版本GET的迟到401不能关闭已获得新图的当前验证', async () => {
    const { captcha, fetcher } = await active()
    const stale = pending()
    const updated = { ...frame(), image: 'data:image/png;base64,iVBORw0KGgp1cGRhdGVk' }
    fetcher.mockReturnValueOnce(stale.promise).mockResolvedValueOnce(json({ status: 'ok', frame: updated }))
      .mockResolvedValue(json(updated))
    await vi.advanceTimersByTimeAsync(600)
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    stale.resolve({ error: { code: 'AUTH_REQUIRED', message: 'private' } }, 401)
    await flushPromises()
    expect(captcha.challenge.value).toEqual(updated)
    expect(captcha.error.value).toBeNull()
    await vi.advanceTimersByTimeAsync(600)
    expect(fetcher).toHaveBeenCalledTimes(4)
  })

  it.each(['click', 'pointerup'] as const)('%s响应直接显示新图，不再等待额外GET', async type => {
    const { captcha, fetcher } = await active()
    const updated = { ...frame(), image: 'data:image/png;base64,iVBORw0KGgp1cGRhdGVk' }
    fetcher.mockResolvedValueOnce(json({ status: 'ok', frame: updated }))
    await captcha.input(id, { type, x: 100, y: 200 })
    expect(captcha.challenge.value).toEqual(updated)
    await vi.advanceTimersByTimeAsync(0)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('点击带回的新图不等待在途旧GET，旧图迟到不会覆盖或触发重复取图', async () => {
    const { captcha, fetcher } = await active()
    const stale = pending()
    const updated = { ...frame(), image: 'data:image/png;base64,iVBORw0KGgp1cGRhdGVk' }
    fetcher.mockReturnValueOnce(stale.promise).mockResolvedValueOnce(json({ status: 'ok', frame: updated }))
    await vi.advanceTimersByTimeAsync(600)
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    expect(captcha.challenge.value).toEqual(updated)
    stale.resolve(frame())
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    expect(captcha.challenge.value).toEqual(updated)
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('点击未完成时暂停周期取图，避免截图与输入争用服务端队列', async () => {
    const { captcha, fetcher } = await active()
    const click = pending()
    fetcher.mockReturnValueOnce(click.promise).mockResolvedValue(json(frame()))
    const input = captcha.input(id, { type: 'click', x: 100, y: 200 })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1800)
    expect(fetcher).toHaveBeenCalledTimes(2)
    click.resolve({ status: 'ok', frame: { ...frame(), image: 'data:image/png;base64,iVBORw0KGgp1cGRhdGVk' } })
    await input
    await vi.advanceTimersByTimeAsync(600)
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('已确认点击携带非法或其他挑战的截图时不替换画面，也不重复点击', async () => {
    const { captcha, fetcher } = await active()
    fetcher.mockResolvedValueOnce(json({ status: 'ok', frame: frame(otherId) }))
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    expect(captcha.challenge.value?.challenge_id).toBe(id)
    expect(captcha.error.value).toBe('验证码图片更新失败，请稍后重试')
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
  })

  it('验证码已完成时接受点击返回waiting，由登录请求处理最终结果', async () => {
    const { captcha, fetcher } = await active()
    fetcher.mockResolvedValueOnce(json({ status: 'ok', frame: { status: 'waiting' } }))
    await captcha.input(id, { type: 'click', x: 100, y: 200 })
    expect(captcha.challenge.value).toBeNull()
    expect(captcha.error.value).toBeNull()
  })

  it.each(['stop', 'restart'] as const)('%s之后不接受上一代点击响应中的截图', async action => {
    const { captcha, fetcher } = await active()
    const click = pending()
    fetcher.mockReturnValueOnce(click.promise).mockResolvedValue(json(frame(otherId)))
    const input = captcha.input(id, { type: 'click', x: 100, y: 200 })
    await flushPromises()
    if (action === 'stop') captcha.stop()
    else { captcha.start(); await vi.advanceTimersByTimeAsync(600) }
    click.resolve({ status: 'ok', frame: frame() })
    await input
    expect(captcha.challenge.value?.challenge_id ?? null).toBe(action === 'stop' ? null : otherId)
  })

  it('本地模式waiting不弹窗但继续检查，之后出现active再显示', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ status: 'waiting' })).mockResolvedValue(json(frame()))
    const { captcha } = setup(fetcher)
    captcha.start(); await vi.advanceTimersByTimeAsync(600)
    expect(captcha.challenge.value).toBeNull()
    await vi.advanceTimersByTimeAsync(600)
    expect(captcha.challenge.value?.challenge_id).toBe(id)
  })
  it('600ms顺序轮询，GET未完成不重叠；stop忽略迟到截图并清理计时器', async () => {
    const first = pending(), second = pending()
    const fetcher = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { captcha } = setup(fetcher)
    captcha.start()
    await vi.advanceTimersByTimeAsync(599)
    expect(fetcher).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    first.resolve({ status: 'waiting' })
    await flushPromises()
    expect(captcha.challenge.value).toBeNull()
    await vi.advanceTimersByTimeAsync(600)
    expect(fetcher).toHaveBeenCalledTimes(2)
    const signal = fetcher.mock.calls[1]![1].signal as AbortSignal
    captcha.stop()
    expect(signal.aborted).toBe(true)
    second.resolve(frame())
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(captcha.challenge.value).toBeNull()
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('新一代登录不接受上一代迟到GET', async () => {
    const old = pending()
    const fetcher = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue(json(frame(otherId)))
    const { captcha } = setup(fetcher)
    captcha.start()
    await vi.advanceTimersByTimeAsync(600)
    captcha.start()
    await vi.advanceTimersByTimeAsync(600)
    old.resolve(frame())
    await flushPromises()
    expect(captcha.challenge.value?.challenge_id).toBe(otherId)
  })

  it('down/move/up按顺序发送，操作完成后刷新且拒绝旧截图', async () => {
    const { captcha, fetcher } = await active()
    const down = pending(), move = pending(), up = pending(), stale = pending()
    fetcher.mockReturnValueOnce(stale.promise).mockReturnValueOnce(down.promise)
      .mockReturnValueOnce(move.promise).mockReturnValueOnce(up.promise).mockResolvedValue(json(frame()))
    await vi.advanceTimersByTimeAsync(600)
    const first = captcha.input(id, { type: 'pointerdown', x: 20, y: 30 })
    const second = captcha.input(id, { type: 'pointermove', x: 40, y: 30 })
    const third = captcha.input(id, { type: 'pointerup', x: 60, y: 30 })
    await flushPromises()
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
    stale.resolve(frame(otherId))
    await flushPromises()
    expect(captcha.challenge.value?.challenge_id).toBe(id)
    down.resolve({ status: 'ok' }); await flushPromises()
    move.resolve({ status: 'ok' }); await flushPromises()
    up.resolve({ status: 'ok' }); await Promise.all([first, second, third])
    await vi.advanceTimersByTimeAsync(0)
    const posts = fetcher.mock.calls.filter(([, init]) => init.method === 'POST')
    expect(posts.map(([, init]) => JSON.parse(init.body))).toEqual([
      { type: 'pointerdown', x: 20, y: 30 }, { type: 'pointermove', x: 40, y: 30 }, { type: 'pointerup', x: 60, y: 30 },
    ])
    expect(fetcher.mock.calls.at(-1)![0]).toBe('/api/auth/captcha')
    expect(posts.every(([path]) => path === `/api/auth/captcha/${id}/input`)).toBe(true)
  })

  it('取消只DELETE当前挑战一次，成功关闭后无迟到截图和轮询', async () => {
    const { captcha, fetcher } = await active()
    const deletion = pending(), stale = pending()
    fetcher.mockReturnValueOnce(stale.promise).mockReturnValueOnce(deletion.promise)
    await vi.advanceTimersByTimeAsync(600)
    const cancel = captcha.cancel(id)
    await captcha.cancel(id)
    expect(captcha.cancelling.value).toBe(true)
    stale.resolve(frame(otherId)); await flushPromises()
    expect(captcha.challenge.value?.challenge_id).toBe(id)
    deletion.resolve({ status: 'ok' }); await cancel
    await vi.advanceTimersByTimeAsync(5000)
    expect(captcha.challenge.value).toBeNull()
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'DELETE')).toHaveLength(1)
  })

  it('取消请求失败保留弹窗可人工重试，不自动重发且不显示原始错误', async () => {
    const { captcha, fetcher } = await active()
    fetcher.mockRejectedValueOnce(new Error('private-url-and-token'))
    await captcha.cancel(id)
    expect(captcha.challenge.value?.challenge_id).toBe(id)
    expect(captcha.cancelling.value).toBe(false)
    expect(captcha.error.value).toBe('取消验证尚未确认，请重试')
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'DELETE')).toHaveLength(1)
  })

  it('轮询401安静停止，不改变长登录请求应呈现的错误', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ error: { code: 'AUTH_REQUIRED', message: 'private' } }, 401))
    const { captcha } = setup(fetcher)
    captcha.start(); await vi.advanceTimersByTimeAsync(600)
    await vi.advanceTimersByTimeAsync(5000)
    expect(captcha.error.value).toBeNull()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it.each([
    { ...frame(), image: 'https://private.example/verification?token=private' },
    { ...frame(), challenge_id: '../private' }, { ...frame(), width: 0 },
  ])('拒绝非法截图合同，不把URL作为图片展示', async value => {
    const { captcha } = setup(vi.fn().mockResolvedValue(json(value)))
    captcha.start(); await vi.advanceTimersByTimeAsync(600)
    expect(captcha.challenge.value).toBeNull()
    expect(captcha.error.value).toBe('验证码暂时无法加载，请稍后重试')
  })

  it.each([[0, true], [1, false]] as const)('base64上限不含dataURL前缀：额外%s字符允许=%s', async (extra, accepted) => {
    const value = { ...frame(), image: 'data:image/png;base64,' + 'A'.repeat(4 * 1024 * 1024 + extra) }
    const { captcha } = setup(vi.fn().mockResolvedValue(json(value)))
    captcha.start(); await vi.advanceTimersByTimeAsync(600)
    expect(Boolean(captcha.challenge.value)).toBe(accepted)
  })

  it('只接受当前挑战的有限范围坐标，清理后队列不再发送输入', async () => {
    const { captcha, fetcher, scope } = await active()
    await captcha.input(otherId, { type: 'click', x: 1, y: 2 })
    await captcha.input(id, { type: 'click', x: Number.NaN, y: 2 })
    await captcha.input(id, { type: 'click', x: 520, y: 2 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    const down = pending()
    fetcher.mockReturnValueOnce(down.promise)
    const first = captcha.input(id, { type: 'pointerdown', x: 1, y: 2 })
    const second = captcha.input(id, { type: 'pointerup', x: 1, y: 2 })
    await flushPromises()
    scope.stop()
    down.resolve({ status: 'ok' }); await Promise.all([first, second])
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetcher.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
    expect(captcha.challenge.value).toBeNull()
  })
})
