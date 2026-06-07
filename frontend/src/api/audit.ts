import { webClient } from './client'

export interface AuditRow {
  id: number
  ts: string
  actor: 'customer' | 'admin' | 'system' | 'broker' | string
  action: string
  resource: string | null
  before: any
  after: any
  src_ip: string | null
  status: string | null
  note: string | null
}

export interface AuditQuery {
  limit?: number
  actor?: string
  action_prefix?: string
  since?: string
}

export async function getAuditLog(q: AuditQuery = {}): Promise<AuditRow[]> {
  const r = await webClient.get('/api/instance/audit', { params: q })
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'audit failed')
  return r.data.data as AuditRow[]
}

export interface AuditVerifyResult {
  ok: boolean
  total_rows: number
  verified_rows: number
  first_break_at_id: number | null
  first_break_reason: string | null
  head_hash: string | null
  head_id: number | null
  count: number
  legacy_unhashed_rows: number
}

export async function verifyAuditChain(): Promise<AuditVerifyResult> {
  const r = await webClient.get('/api/instance/audit/verify')
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'verify failed')
  return r.data.data as AuditVerifyResult
}

export interface AuditHead {
  head_hash: string | null
  head_id: number | null
  count: number
}

export async function getAuditHead(): Promise<AuditHead> {
  const r = await webClient.get('/api/instance/audit/head')
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'head failed')
  return r.data.data as AuditHead
}

export function getAuditExportUrl(q: AuditQuery = {}): string {
  const sp = new URLSearchParams()
  if (q.limit) sp.set('limit', String(q.limit))
  if (q.actor) sp.set('actor', q.actor)
  if (q.action_prefix) sp.set('action_prefix', q.action_prefix)
  if (q.since) sp.set('since', q.since)
  const qs = sp.toString()
  return `/api/instance/audit/export${qs ? '?' + qs : ''}`
}
