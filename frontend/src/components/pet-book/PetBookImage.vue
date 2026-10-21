<script setup lang="ts">
import { shallowRef, watch } from 'vue'

const props = defineProps<{ src: string | null; alt: string }>()
const failed = shallowRef(false)
watch(() => props.src, () => { failed.value = false })
</script>

<template>
  <span class="pet-image-frame">
    <img v-if="src && !failed" class="pet-portrait" :src="src" :alt="alt" width="100" height="100" loading="lazy" decoding="async" @error="failed = true" />
    <span v-else class="missing-image" role="img" :aria-label="`${alt}暂无图片`">?</span>
  </span>
</template>

<style scoped>
.pet-image-frame { display: flex; align-items: center; justify-content: center; flex-shrink: 0; overflow: hidden; }
.pet-portrait { display: block; width: 100%; height: 100%; object-fit: contain; }
.missing-image { color: var(--text-muted); font-size: 28px; }
</style>
