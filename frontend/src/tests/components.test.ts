import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GameLoginForm from '../components/auth/GameLoginForm.vue'
import LoginView from '../views/LoginView.vue'
import PetSearch from '../components/market/PetSearch.vue'

describe('登录表单', () => {
  it('账号登录等待时提示手动完成官方验证，结束后移除提示', async () => {
    const wrapper = mount(LoginView, { props: {
      capabilities: { provider: 'bilibili_game', integration_ready: true, methods: ['account'], reason_code: null, message: null },
      session: { status: 'anonymous', authenticated: false, role: null, roles: [], message: null },
      error: null, busy: true, qrBusy: false, qrChallenge: null, qrState: null,
    } })
    await flushPromises()
    expect(wrapper.text()).toContain('官方验证页')
    expect(wrapper.text()).not.toContain('扫码登录')
    await wrapper.setProps({ busy: false })
    expect(wrapper.text()).not.toContain('官方验证页')
    wrapper.unmount()
  })
  it('未接通时禁用登录，不能发出账号密码', async () => {
    const wrapper = mount(GameLoginForm, { props: { disabled: true, busy: false } })
    const submit = wrapper.find('button[type="submit"]')
    expect(submit.exists()).toBe(true)
    expect(submit.attributes('disabled')).toBeDefined()
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('login')).toBeUndefined()
  })
  it('正常提交发出凭证后，立即清除密码输入', async () => {
    const wrapper = mount(GameLoginForm, { props: { disabled: false, busy: false } })
    expect(wrapper.find('input[name="account"]').exists()).toBe(true)
    await wrapper.find('input[name="account"]').setValue('test-only')
    await wrapper.find('input[name="password"]').setValue('test-password-only')
    await wrapper.get('button[aria-label="显示密码"]').trigger('click')
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('login')?.[0]).toEqual([{ account: 'test-only', password: 'test-password-only' }])
    expect((wrapper.find('input[name="password"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.get('input[name="password"]').attributes('type')).toBe('password')
    expect(wrapper.get('button[aria-label="显示密码"]').attributes('aria-pressed')).toBe('false')
  })
  it('默认隐藏密码，显示和隐藏切换保留输入且不提交', async () => {
    const wrapper = mount(GameLoginForm, { props: { disabled: false, busy: false } })
    const password = wrapper.get('input[name="password"]')
    expect(wrapper.get('input[name="account"]').attributes('type')).toBe('text')
    expect(password.attributes('type')).toBe('password')
    await password.setValue('test-password-only')
    const show = wrapper.get('button[aria-label="显示密码"]')
    expect(show.attributes('type')).toBe('button')
    expect(show.attributes('aria-pressed')).toBe('false')
    expect(show.attributes('aria-controls')).toBe(password.attributes('id'))
    await show.trigger('click')
    expect(password.attributes('type')).toBe('text')
    expect((password.element as HTMLInputElement).value).toBe('test-password-only')
    const hide = wrapper.get('button[aria-label="隐藏密码"]')
    expect(hide.attributes('aria-pressed')).toBe('true')
    await hide.trigger('click')
    expect(password.attributes('type')).toBe('password')
    expect((password.element as HTMLInputElement).value).toBe('test-password-only')
    expect(wrapper.emitted('login')).toBeUndefined()
  })
  it('密码眼睛按钮没有可见文字，切换图标且不会误提交', async () => {
    const wrapper = mount(GameLoginForm, { props: { disabled: false, busy: false } })
    try {
      await wrapper.get('input[name="account"]').setValue('test-only')
      await wrapper.get('input[name="password"]').setValue('test-password-only')
      const show = wrapper.get('button[aria-label="显示密码"]')
      expect(show.attributes('type')).toBe('button')
      expect(show.text()).toBe('')
      expect(show.find('.van-icon-eye-o').exists()).toBe(true)
      expect(show.get('.van-icon').attributes('aria-hidden')).toBe('true')
      await show.trigger('click')
      const hide = wrapper.get('button[aria-label="隐藏密码"]')
      expect(hide.text()).toBe('')
      expect(hide.find('.van-icon-closed-eye').exists()).toBe(true)
      expect(hide.find('.van-icon-eye-o').exists()).toBe(false)
      expect(wrapper.emitted('login')).toBeUndefined()
      await hide.trigger('click')
      expect(wrapper.get('button[aria-label="显示密码"]').find('.van-icon-eye-o').exists()).toBe(true)
      expect(wrapper.get('input[name="password"]').attributes('type')).toBe('password')
      expect((wrapper.get('input[name="password"]').element as HTMLInputElement).value).toBe('test-password-only')
      expect(wrapper.emitted('login')).toBeUndefined()
    } finally {
      wrapper.unmount()
    }
  })
  it.each(['disabled', 'busy'] as const)('%s 时不能切换密码显示状态', async (flag) => {
    const wrapper = mount(GameLoginForm, { props: { disabled: false, busy: false } })
    const password = wrapper.get('input[name="password"]')
    await wrapper.setProps({ [flag]: true })
    const show = wrapper.get('button[aria-label="显示密码"]')
    expect(show.attributes('disabled')).toBeDefined()
    await show.trigger('click')
    expect(password.attributes('type')).toBe('password')
    await wrapper.setProps({ [flag]: false })
    await show.trigger('click')
    expect(password.attributes('type')).toBe('text')
    await wrapper.setProps({ [flag]: true })
    const hide = wrapper.get('button[aria-label="隐藏密码"]')
    expect(hide.attributes('disabled')).toBeDefined()
    await hide.trigger('click')
    expect(password.attributes('type')).toBe('text')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('login')).toBeUndefined()
  })
})

describe('中文名称筛选', () => {
  it('移除搜索标题、覆盖说明与统计，保留输入和目录状态', () => {
    const wrapper = mount(PetSearch, { props: {
      query: '蜜桃', petIds: [], busy: true, error: '目录暂不可用',
      catalog: { items: [], total: 306, named_count: 0, image_count: 297 },
    } })
    try {
      expect(wrapper.find('.search-heading').exists()).toBe(false)
      expect(wrapper.find('.coverage-note').exists()).toBe(false)
      expect(wrapper.text()).not.toContain('查找噜咪')
      expect(wrapper.text()).not.toContain('按名称或形态筛选')
      expect(wrapper.text()).not.toContain('名称筛选仅覆盖')
      expect(wrapper.get('input[type="search"]').attributes('placeholder')).toBe('搜索宠物中文名')
      expect(wrapper.get('input[type="search"]').attributes('disabled')).toBeDefined()
      expect(wrapper.find('.coverage').exists()).toBe(false)
      expect(wrapper.get('[role="alert"]').text()).toBe('目录暂不可用')
      expect(wrapper.get('.loading').text()).toBe('查询中')
    } finally {
      wrapper.unmount()
    }
  })
  it('输入中文形成 query 更新事件', async () => {
    const wrapper = mount(PetSearch, { props: { query: '', petIds: [], catalog: null, busy: false, error: null } })
    expect(wrapper.find('input[type="search"]').exists()).toBe(true)
    await wrapper.find('input[type="search"]').setValue('蜜桃')
    expect(wrapper.emitted('update:query')?.[0]).toEqual(['蜜桃'])
    expect(wrapper.emitted('search')).toBeUndefined()
  })
  it('右侧搜索按钮和回车分别提交一次，不清空输入或误触发表单', async () => {
    const wrapper = mount(PetSearch, { props: { query: '蜜桃', petIds: [], catalog: null, busy: false, error: null } })
    try {
      const button = wrapper.get('button[aria-label="搜索商品"]')
      expect(button.text()).toBe('搜索')
      expect(button.attributes('type')).toBe('button')
      await button.trigger('click')
      expect(wrapper.emitted('search')).toEqual([[]])
      expect(wrapper.emitted('update:query')).toBeUndefined()
      expect((wrapper.get('input[type="search"]').element as HTMLInputElement).value).toBe('蜜桃')
      await wrapper.get('input[type="search"]').trigger('keypress', { key: 'Enter', keyCode: 13 })
      expect(wrapper.emitted('search')).toEqual([[], []])
    } finally {
      wrapper.unmount()
    }
  })
  it('目录忙碌时搜索按钮与回车都不能提交', async () => {
    const wrapper = mount(PetSearch, { props: { query: '蜜桃', petIds: [], catalog: null, busy: true, error: null } })
    try {
      expect(wrapper.get('button[aria-label="搜索商品"]').attributes('disabled')).toBeDefined()
      expect(wrapper.get('input[type="search"]').attributes('disabled')).toBeDefined()
      await wrapper.get('button[aria-label="搜索商品"]').trigger('click')
      await wrapper.get('input[type="search"]').trigger('keypress', { key: 'Enter', keyCode: 13 })
      expect(wrapper.emitted('search')).toBeUndefined()
      await wrapper.setProps({ busy: false })
      await wrapper.get('button[aria-label="搜索商品"]').trigger('click')
      expect(wrapper.emitted('search')).toEqual([[]])
    } finally {
      wrapper.unmount()
    }
  })
  it('仅草稿与已提交名称一致时展示候选和无匹配提示', async () => {
    const wrapper = mount(PetSearch, { props: {
      query: ' 蜜桃 ', activeQuery: '', petIds: [], busy: false, error: null,
      catalog: { total: 2, named_count: 1, image_count: 1, items: [
        { pet_id: '1000101', name: '蜜桃', display_name: '蜜桃', image_url: null, name_verified: true },
      ] },
    } })
    try {
      expect(wrapper.find('.pet-options').exists()).toBe(false)
      expect(wrapper.find('.hint').exists()).toBe(false)
      await wrapper.setProps({ activeQuery: '蜜桃' })
      expect(wrapper.find('[data-pet-id="1000101"]').exists()).toBe(true)
      await wrapper.setProps({ query: '灰灰', catalog: { total: 2, named_count: 1, image_count: 1, items: [] } })
      expect(wrapper.find('.pet-options').exists()).toBe(false)
      expect(wrapper.find('.hint').exists()).toBe(false)
      await wrapper.setProps({ activeQuery: '灰灰' })
      expect(wrapper.get('.hint').text()).toContain('不能据此判断交易行没有商品')
    } finally {
      wrapper.unmount()
    }
  })
  it('未知名称保留不能据此判断无商品的提示', () => {
    const wrapper = mount(PetSearch, { props: {
      query: '蜜桃', petIds: [], busy: false, error: null,
      catalog: { items: [], total: 306, named_count: 0, image_count: 297 },
    } })
    expect(wrapper.text()).toContain('不能据此判断交易行没有商品')
  })
  it('同名形态可独立选中，输出实际 ID 集合', async () => {
    const wrapper = mount(PetSearch, { props: {
      query: '蜜桃', petIds: [], busy: false, error: null,
      catalog: { total: 2, named_count: 2, image_count: 2, items: [
        { pet_id: '1000101', name: '蜜桃', display_name: '蜜桃', image_url: null, name_verified: true },
        { pet_id: '1000102', name: '蜜桃', display_name: '蜜桃', image_url: null, name_verified: true },
      ] },
    } })
    expect(wrapper.find('[data-pet-id="1000102"]').exists()).toBe(true)
    await wrapper.find('[data-pet-id="1000102"]').trigger('click')
    expect(wrapper.emitted('update:petIds')?.[0]).toEqual([['1000102']])
  })
})
