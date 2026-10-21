import { mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import ManualCaptcha from '../components/auth/ManualCaptcha.vue'
import type { ManualCaptchaFrame } from '../api/types'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = ''; vi.restoreAllMocks() })
const frame: ManualCaptchaFrame = { status: 'active', challenge_id: 'c'.repeat(32), width: 520, height: 900, image: 'data:image/png;base64,iVBORw0KGgo=' }
function setup() {
  const wrapper = mount(ManualCaptcha, { attachTo: document.body, props: { challenge: frame, cancelling: false }, global: { stubs: { teleport: true } } })
  wrappers.push(wrapper)
  const image = wrapper.get('img')
  vi.spyOn(image.element, 'getBoundingClientRect').mockReturnValue({ left: 20, top: 30, width: 260, height: 450 } as DOMRect)
  const pointer = (type: string, clientX: number, clientY: number) => {
    const event = new MouseEvent(type, { clientX, clientY, button: 0, bubbles: true })
    Object.defineProperty(event, 'pointerId', { value: 1 })
    image.element.dispatchEvent(event)
  }
  return { wrapper, image, pointer }
}
it('缩放截图的人工点击映射到520x900，并忽略浏览器附带click避免重复', () => {
  const { wrapper, image, pointer } = setup()
  pointer('pointerdown', 150, 255); pointer('pointerup', 150, 255)
  image.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  expect(wrapper.emitted('input')).toEqual([[frame.challenge_id, { type: 'click', x: 260, y: 450 }]])
  expect(image.attributes('width')).toBe('520')
  expect(image.attributes('height')).toBe('900')
})
it('拖动依次发送down/move/up，节流移动并在抬手前补齐最终位置', () => {
  vi.spyOn(Date, 'now').mockReturnValue(0)
  const { wrapper, pointer } = setup()
  pointer('pointerdown', 30, 40); pointer('pointermove', 60, 40); pointer('pointermove', 100, 40); pointer('pointerup', 110, 40)
  expect(wrapper.emitted('input')).toEqual([
    [frame.challenge_id, { type: 'pointerdown', x: 20, y: 20 }],
    [frame.challenge_id, { type: 'pointermove', x: 80, y: 20 }],
    [frame.challenge_id, { type: 'pointermove', x: 180, y: 20 }],
    [frame.challenge_id, { type: 'pointerup', x: 180, y: 20 }],
  ])
})
it('换挑战后旧手势不会产生输入，取消事件携带当前挑战id', async () => {
  const { wrapper, pointer } = setup()
  pointer('pointerdown', 30, 40)
  const next = { ...frame, challenge_id: 'n'.repeat(32) }
  await wrapper.setProps({ challenge: next })
  pointer('pointerup', 30, 40)
  expect(wrapper.emitted('input')).toBeUndefined()
  await wrapper.get('button[aria-label="取消人工验证"]').trigger('click')
  expect(wrapper.emitted('cancel')).toEqual([[next.challenge_id]])
})
it('取消中禁用交互，指针取消会释放已开始拖动而不产生点击', async () => {
  const { wrapper, pointer } = setup()
  pointer('pointerdown', 30, 40); pointer('pointermove', 60, 40); pointer('pointercancel', 60, 40)
  expect(wrapper.emitted('input')?.map(row => (row[1] as { type: string }).type)).toEqual(['pointerdown', 'pointermove', 'pointerup'])
  const count = wrapper.emitted('input')!.length
  await wrapper.setProps({ cancelling: true })
  pointer('pointerdown', 30, 40); pointer('pointerup', 30, 40)
  expect(wrapper.emitted('input')).toHaveLength(count)
  expect(wrapper.get('button[aria-label="取消人工验证"]').attributes('disabled')).toBeDefined()
})
