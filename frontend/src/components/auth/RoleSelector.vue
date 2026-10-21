<script setup lang="ts">
import { shallowRef, watch } from 'vue'
import { Radio as VanRadio, RadioGroup as VanRadioGroup, Cell as VanCell, CellGroup as VanCellGroup, Button as VanButton } from 'vant'
import type { Role } from '../../api/types'
const props = defineProps<{ roles: Role[]; busy: boolean }>()
const emit = defineEmits<{ select: [roleId: string] }>()
const selected = shallowRef('')
watch(() => props.roles, roles => { selected.value = roles[0]?.id ?? '' }, { immediate: true })
</script>
<template>
  <section class="role-selector">
    <h2 class="title">选择游戏角色</h2>
    <VanRadioGroup v-model="selected" :disabled="busy">
      <VanCellGroup inset>
        <VanCell v-for="role in roles" :key="role.id" :title="role.name" :label="role.server_name" clickable @click="selected = role.id">
          <template #right-icon><VanRadio :name="role.id" :aria-label="`${role.server_name} ${role.name}`" /></template>
        </VanCell>
      </VanCellGroup>
    </VanRadioGroup>
    <VanButton class="confirm" type="primary" block :loading="busy" :disabled="!selected" @click="emit('select', selected)">使用此角色</VanButton>
  </section>
</template>
<style scoped>
.title { font-size: 18px; margin: 0 0 18px; }
.role-selector :deep(.van-cell-group--inset) { margin: 0; }
.confirm { margin-top: 20px; border-radius: 10px; }
</style>
