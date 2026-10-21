import { createRouter, createWebHashHistory, type RouterHistory } from 'vue-router'

export type AppMenu = 'book' | 'trade' | 'orders' | 'mine'
export type OrderTab = 'orders' | 'inspection'

export const menuPaths: Record<AppMenu, string> = {
  book: '/book', trade: '/trade', orders: '/orders', mine: '/mine',
}
const returnPaths = new Set([...Object.values(menuPaths), '/orders/inspection'])

export function safeReturnPath(value: unknown): string {
  return typeof value === 'string' && returnPaths.has(value) ? value : '/book'
}

declare module 'vue-router' {
  interface RouteMeta {
    menu?: AppMenu
    requiresAuth?: boolean
    orderTab?: OrderTab
  }
}

// App keeps the existing views mounted so changing routes preserves their state.
const routeSurface = { render: () => null }

export function createAppRouter(history: RouterHistory = createWebHashHistory()) {
  const router = createRouter({
    history,
    routes: [
      { path: '/book', component: routeSurface, meta: { menu: 'book' } },
      { path: '/trade', component: routeSurface, meta: { menu: 'trade', requiresAuth: true } },
      { path: '/orders', component: routeSurface, meta: { menu: 'orders', requiresAuth: true, orderTab: 'orders' } },
      { path: '/orders/inspection', component: routeSurface, meta: { menu: 'orders', requiresAuth: true, orderTab: 'inspection' } },
      { path: '/mine', component: routeSurface, meta: { menu: 'mine', requiresAuth: true } },
      { path: '/login', component: routeSurface, meta: { menu: 'book' } },
      { path: '/', redirect: '/book' },
      { path: '/:pathMatch(.*)*', redirect: '/book' },
    ],
  })
  router.beforeEach(to => {
    if (to.path !== '/login') return
    const redirect = safeReturnPath(to.query.redirect)
    if (to.query.redirect !== redirect || Object.keys(to.query).length !== 1 || to.hash) {
      return { path: '/login', query: { redirect }, replace: true }
    }
  })
  return router
}
