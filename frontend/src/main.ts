import { createApp } from 'vue'
import 'vant/lib/index.css'
import './assets/main.css'
import App from './App.vue'
import { createAppRouter } from './router'

const router = createAppRouter()
const app = createApp(App).use(router)
void router.isReady().then(() => app.mount('#app'))
