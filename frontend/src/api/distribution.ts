import { webClient } from './client'

export interface DistributionInbox {
  id: number
  name: string
  inbox_slug: string
  api_key_last4: string
  broker_override: string | null
  allowed_ips: string
  status: 'active' | 'disabled'
  last_signal_at: string | null
  last_signal_status: string | null
  last_signal_summary: string | null
  signal_count_total: number
  /** Set after hostingsol's provisioner auto-registers this inbox with
   *  publisher.alphaquark.in. Null = not yet linked (legacy / manual setup). */
  publisher_subscriber_id: number | null
  created_at: string
  updated_at: string
}

export interface InboxCreateResult extends DistributionInbox {
  api_key_plaintext: string
  signing_secret?: string
  webhook_url: string
}

export interface DistributionSignal {
  id: number
  signal_id: string
  received_at: string
  src_ip: string | null
  payload: Record<string, unknown>
  status: string
  broker_used: string | null
  broker_order_id: string | null
  error_message: string | null
}

export interface CreateInboxPayload {
  name: string
  broker_override?: string | null
  allowed_ips?: string | null
}

export interface UpdateInboxPayload {
  name?: string
  broker_override?: string | null
  allowed_ips?: string | null
  status?: 'active' | 'disabled'
}

export async function listInboxes(): Promise<DistributionInbox[]> {
  const r = await webClient.get('/api/distribution/inboxes')
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'list failed')
  return r.data.data as DistributionInbox[]
}

export async function createInbox(payload: CreateInboxPayload): Promise<InboxCreateResult> {
  const r = await webClient.post('/api/distribution/inboxes', payload)
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'create failed')
  return r.data.data as InboxCreateResult
}

export async function updateInbox(id: number, payload: UpdateInboxPayload): Promise<void> {
  const r = await webClient.put(`/api/distribution/inboxes/${id}`, payload)
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'update failed')
}

export async function rotateInboxKey(id: number): Promise<{ api_key_plaintext: string; api_key_last4: string; signing_secret?: string }> {
  const r = await webClient.post(`/api/distribution/inboxes/${id}/rotate`)
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'rotate failed')
  return r.data.data as { api_key_plaintext: string; api_key_last4: string; signing_secret?: string }
}

export async function deleteInbox(id: number): Promise<void> {
  const r = await webClient.delete(`/api/distribution/inboxes/${id}`)
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'delete failed')
}

export async function listInboxSignals(id: number, limit = 50): Promise<DistributionSignal[]> {
  const r = await webClient.get(`/api/distribution/inboxes/${id}/signals`, { params: { limit } })
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'signals fetch failed')
  return r.data.data as DistributionSignal[]
}
