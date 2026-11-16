<template>
  <div>
    <div class="price-toolbar">
      <span>{{ t('prices.scopeKey') }}</span>
      <el-select v-model="keyId" clearable :placeholder="t('prices.scopeAll')" style="width: 260px" @change="load">
        <el-option v-for="key in keys" :key="key.id" :label="`${key.name} (${key.key_prefix}…)`" :value="key.id" />
      </el-select>
    </div>
    <p class="price-hint">{{ t('prices.unit') }}</p>
    <el-table :data="rows" v-loading="loading" stripe :empty-text="t('prices.empty')">
      <el-table-column prop="model" :label="t('common.model')" min-width="180">
        <template #default="{ row }">
          {{ row.model }}
          <el-tag v-if="row.varies_by_route" size="small" type="warning" effect="plain" class="varies-tag">{{ t('prices.variesTag') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('prices.input')" min-width="120"><template #default="{ row }">{{ price(row.prompt) }}</template></el-table-column>
      <el-table-column :label="t('prices.output')" min-width="120"><template #default="{ row }">{{ price(row.completion) }}</template></el-table-column>
      <el-table-column :label="t('prices.cached')" min-width="130"><template #default="{ row }">{{ price(row.cached) }}</template></el-table-column>
      <el-table-column :label="t('prices.cacheWrite')" min-width="130"><template #default="{ row }">{{ price(row.cache_write) }}</template></el-table-column>
      <el-table-column :label="t('prices.request')" min-width="100"><template #default="{ row }">{{ price(row.request) }}</template></el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { keysApi, type CallableModelPrice } from '@/api/keys'
import { useI18n } from '@/i18n'

defineProps<{ keys: { id: number; name: string; key_prefix: string }[] }>()

const { t } = useI18n()
const keyId = ref<number | undefined>()
const rows = ref<CallableModelPrice[]>([])
const loading = ref(false)

function price(value: number) {
  return '$' + value.toLocaleString(undefined, { maximumFractionDigits: 6 })
}

async function load() {
  loading.value = true
  try {
    rows.value = await keysApi.models(keyId.value || undefined)
  } catch {
    rows.value = []
    ElMessage.error(t('keys.loadFailed'))
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.price-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.price-hint {
  color: #86909c;
  font-size: 12px;
}

.varies-tag {
  margin-left: 4px;
}
</style>
