<template>
  <div>
    <div class="bill-toolbar">
      <el-date-picker
        v-model="range"
        type="daterange"
        value-format="YYYY-MM-DD"
        :clearable="false"
        :start-placeholder="t('bill.start')"
        :end-placeholder="t('bill.end')"
        @change="load"
      />
      <el-button type="primary" :loading="exporting" @click="exportCsv">{{ t('bill.export') }}</el-button>
    </div>
    <el-alert type="warning" :closable="false" show-icon class="bill-notice" :title="t('bill.noticeOverspend')"
      :description="t('bill.noticePending', { time: generatedAt })" />
    <el-table :data="rows" v-loading="loading" stripe show-summary :summary-method="summary" :empty-text="t('bill.empty')">
      <el-table-column prop="api_key_name" :label="t('common.name')" min-width="160" />
      <el-table-column :label="t('keys.prefix')" min-width="120"><template #default="{ row }">{{ row.key_prefix }}…</template></el-table-column>
      <el-table-column prop="request_count" :label="t('bill.requests')" min-width="100" />
      <el-table-column :label="t('bill.written')" min-width="130"><template #default="{ row }">{{ money(row.written) }}</template></el-table-column>
      <el-table-column :label="t('bill.pending')" min-width="130"><template #default="{ row }">{{ money(row.pending) }}</template></el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import dayjs from 'dayjs'
import { ElMessage } from 'element-plus'
import { keysApi, type KeyBill, type KeyBillRow } from '@/api/keys'
import { useI18n } from '@/i18n'

const { t, locale } = useI18n()
// The server groups usage by UTC day, so the default range is the current UTC month.
const today = new Date().toISOString().slice(0, 10)
const range = ref<[string, string]>([today.slice(0, 8) + '01', today])
const rows = ref<KeyBillRow[]>([])
const total = ref<KeyBill['total'] | null>(null)
const generatedAt = ref('')
const loading = ref(false)
const exporting = ref(false)

function money(value: number) {
  return '$' + Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 6 })
}

function summary() {
  if (!total.value) return []
  return [t('bill.total'), '', String(total.value.request_count), money(total.value.written), money(total.value.pending)]
}

async function load() {
  loading.value = true
  try {
    const bill = await keysApi.bill(range.value[0], range.value[1])
    rows.value = bill.items
    total.value = bill.total
    generatedAt.value = dayjs(bill.generated_at).format('YYYY-MM-DD HH:mm:ss')
  } catch {
    rows.value = []
    total.value = null
    ElMessage.error(t('bill.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function exportCsv() {
  exporting.value = true
  try {
    const blob = await keysApi.billCsv(range.value[0], range.value[1], locale.current)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `five-api-bill-${range.value[0]}-${range.value[1]}.csv`
    link.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error(t('bill.exportFailed'))
  } finally {
    exporting.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.bill-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
}

.bill-notice {
  margin-bottom: 12px;
}
</style>
