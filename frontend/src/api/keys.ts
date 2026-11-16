import client from './client'

export interface ChannelModelPrice {
  model: string
  fixed_price: boolean
  prices: { prompt: number; completion: number; cached: number; cache_write: number; request: number } | null
}

export interface KeyChannel {
  id: number
  name: string
  provider: string
  price_multiplier: number
  healthy: boolean
  models: ChannelModelPrice[]
}

export interface CallableModelPrice {
  model: string
  prompt: number
  completion: number
  cached: number
  cache_write: number
  request: number
  varies_by_route: boolean
}

export interface KeyBillRow {
  api_key_id: number
  api_key_name: string
  key_prefix: string
  request_count: number
  written: number
  pending: number
}

export interface KeyBill {
  tenant: string
  start: string
  end: string
  generated_at: string
  items: KeyBillRow[]
  total: { request_count: number, written: number, pending: number }
}

export const keysApi = {
  bill: async (start: string, end: string): Promise<KeyBill> =>
    (await client.get('/keys/bill', { params: { start, end } })).data,
  billCsv: async (start: string, end: string, lang: 'zh' | 'en'): Promise<Blob> =>
    (await client.get('/keys/bill.csv', { params: { start, end, lang }, responseType: 'blob' })).data,
  models: async (keyId?: number): Promise<CallableModelPrice[]> =>
    (await client.get('/keys/models', { params: keyId ? { key_id: keyId } : {} })).data,
  list: async (page = 1, size = 20) => (await client.get('/keys', { params: { page, size } })).data,
  summary: async () => (await client.get('/keys/summary')).data,
  channels: async (tenantId?: number): Promise<KeyChannel[]> =>
    (await client.get('/keys/channels', { params: tenantId ? { tenant_id: tenantId } : {} })).data,
  create: async (body: any) => (await client.post('/keys', body)).data,
  update: async (id: number, body: any) => (await client.put(`/keys/${id}`, body)).data,
  remove: async (id: number) => (await client.delete(`/keys/${id}`)).data,
  usage: async (id: number) => (await client.get(`/keys/${id}/usage`)).data,
}
