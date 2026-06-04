/**
 * Multi-broker management API client (alphago_live fork).
 *
 * Backs onto blueprints/broker_credentials.py's new endpoints:
 *   GET    /api/broker/credentials/list
 *   POST   /api/broker/credentials/save
 *   PUT    /api/broker/credentials/<broker>/activate
 *   DELETE /api/broker/credentials/<broker>
 *   GET    /api/broker/credentials/<broker>/instructions
 *   GET    /api/broker/supported
 */

import { webClient } from './client'

export interface SavedBroker {
  broker: string
  status: 'saved' | 'active' | 'expired' | 'error'
  has_api_key: boolean
  has_api_secret: boolean
  has_totp_seed: boolean
  client_code: string
  last_activated_at: string | null
  last_auth_at: string | null
  last_error: string | null
  notes: string
  created_at: string
  updated_at: string
}

export interface BrokerField {
  name: string
  label: string
  type: 'text' | 'password'
  required: boolean
  help?: string
}

export interface BrokerInstructions {
  status: string
  broker: string
  fields: BrokerField[]
  instructions_md: string
  redirect_url: string
  client_ipv6: string
  client_ipv4_primary?: string
  client_ipv4_secondary?: string
  client_ipv4_pool?: string[]
  shared_host_ipv4?: string
}

export interface HostInfo {
  client_ipv6: string
  client_ipv4_primary?: string
  client_ipv4_secondary?: string
  client_ipv4_pool?: string[]
  shared_host_ipv4?: string
  host_server: string
  redirect_url_pattern: string
}

export async function getHostInfo(): Promise<HostInfo> {
  const r = await webClient.get('/api/broker/credentials/host-info')
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'host-info failed')
  return r.data.data as HostInfo
}

export interface SupportedBroker {
  broker: string
  has_fields_meta: boolean
  has_instructions: boolean
}

export interface SaveBrokerPayload {
  broker: string
  api_key: string
  api_secret?: string
  api_key_market?: string
  api_secret_market?: string
  client_code?: string
  totp_seed?: string
  extra?: Record<string, string>
  notes?: string
  activate?: boolean
}

export async function listSavedBrokers(): Promise<SavedBroker[]> {
  const r = await webClient.get('/api/broker/credentials/list')
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'list failed')
  return r.data.data as SavedBroker[]
}

export async function getBrokerInstructions(broker: string): Promise<BrokerInstructions> {
  const r = await webClient.get(`/api/broker/credentials/${broker}/instructions`)
  return r.data as BrokerInstructions
}

export async function listSupportedBrokers(): Promise<SupportedBroker[]> {
  const r = await webClient.get('/api/broker/supported')
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'supported failed')
  return r.data.data as SupportedBroker[]
}

export async function saveBroker(payload: SaveBrokerPayload): Promise<{ broker: string; activated: boolean }> {
  const r = await webClient.post('/api/broker/credentials/save', payload)
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'save failed')
  return r.data
}

export async function activateBroker(broker: string): Promise<void> {
  const r = await webClient.put(`/api/broker/credentials/${broker}/activate`)
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'activate failed')
}

export async function deleteBroker(broker: string): Promise<void> {
  const r = await webClient.delete(`/api/broker/credentials/${broker}`)
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'delete failed')
}

export interface AutoLoginResult {
  broker: string
  access_token_masked: string
  user_id?: string
  expires_at?: string
}

export async function autoLogin(broker: string): Promise<AutoLoginResult> {
  const r = await webClient.post(`/api/broker/credentials/${broker}/auto-login`)
  if (r.data?.status !== 'success') {
    throw new Error(r.data?.message || 'auto-login failed')
  }
  return r.data as AutoLoginResult
}

export interface AutoLoginSchedulerStatus {
  enabled: boolean | null
  running: boolean
  next_run: string | null
  error?: string
}

export async function getAutoLoginSchedulerStatus(): Promise<AutoLoginSchedulerStatus> {
  const r = await webClient.get('/api/broker/credentials/auto-login-status')
  if (r.data?.status !== 'success') {
    throw new Error(r.data?.message || 'auto-login-status failed')
  }
  return r.data.data as AutoLoginSchedulerStatus
}
