<script setup lang="ts">
import { useServerClock } from '../../composables/useServerClock'

const { displayTime } = useServerClock()
</script>
<template>
  <time class="server-clock" aria-label="服务器时间" title="北京时间" aria-description="北京时间">{{ displayTime }}</time>
</template>
<style scoped>
.server-clock { display: inline-block; flex-shrink: 0; width: 8ch; color: var(--text-muted); font-size: 12px; line-height: 1.4; font-variant-numeric: tabular-nums; white-space: nowrap; text-align: center; }
</style>
