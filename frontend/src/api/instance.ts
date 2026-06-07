import webClient from './webClient'

export interface NetworkIdentity {
  subdomain: string
  url: string
  ipv6: string
  ipv4_primary: string
  ipv4_secondary: string
  ipv4_pool: string[]
  is_ipv4_dedicated: boolean
}

export interface ComputeInfo {
  cpu_count_host: number
  cpu_limit_cores: number | null
  mem_total_bytes: number
  mem_used_bytes: number
  mem_used_pct: number
  load_1m: number
}

export interface StorageInfo {
  total_bytes: number
  used_bytes: number
  free_bytes: number
  used_pct: number
  path?: string
}

export interface BrokerInfo {
  active_broker: string | null
  last_activated_at: string | null
  last_auth_at: string | null
  saved_brokers: Array<{ broker: string; status: string; last_activated_at: string | null }>
}

export interface RuntimeInfo {
  uptime_seconds: number
  boot_time_utc: string | null
  image_sha: string
  hostname: string
  container_id?: string
}

export interface DataSovereigntyInfo {
  encryption_key_unique_per_instance: boolean
  encryption_key_present_first8: string
  encrypted_at_rest: string[]
  stored_per_container_db: string[]
}

export interface InstanceInfo {
  network: NetworkIdentity
  compute: ComputeInfo
  storage: StorageInfo
  broker: BrokerInfo
  runtime: RuntimeInfo
  data_sovereignty: DataSovereigntyInfo
}

export async function getInstanceInfo(): Promise<InstanceInfo> {
  const r = await webClient.get('/api/instance/info')
  if (r.data?.status !== 'success') throw new Error(r.data?.message || 'instance/info failed')
  return r.data.data as InstanceInfo
}
