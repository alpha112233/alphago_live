/**
 * Infrastructure ("Your Instance") page — what the customer owns.
 *
 * Shows the customer's dedicated runtime: subdomain, IPv6, IPv4, CPU,
 * RAM, disk, uptime, active broker, container ID + image. The goal is
 * to make it visible that this is a dedicated containerized instance,
 * not a shared service.
 */

import {
  Activity,
  CheckCircle2,
  Copy,
  Cpu,
  Database,
  Globe,
  HardDrive,
  Loader2,
  Lock,
  Network,
  Server,
  Shield,
} from 'lucide-react'
import { useEffect, useState } from 'react'

import { getInstanceInfo, type InstanceInfo } from '@/api/instance'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'

function fmtBytes(n: number): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`
}

function fmtDuration(sec: number): string {
  if (!sec || sec < 0) return '—'
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function CopyableValue({ value, mono = true }: { value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false)
  if (!value) return <span className="text-muted-foreground">—</span>

  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      // fallback
    }
  }
  return (
    <div className="flex items-center gap-2 group">
      <code className={`flex-1 text-xs ${mono ? 'font-mono' : ''} break-all`}>{value}</code>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="h-6 w-6 p-0 opacity-50 group-hover:opacity-100"
        onClick={copy}
        aria-label="Copy"
      >
        {copied ? <CheckCircle2 className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
      </Button>
    </div>
  )
}

function StatRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1.5 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{value}</span>
    </div>
  )
}

export default function Infrastructure() {
  const [info, setInfo] = useState<InstanceInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getInstanceInfo()
      .then((d) => setInfo(d))
      .catch((e: any) => setError(e?.message || 'failed to load instance info'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <Loader2 className="h-6 w-6 animate-spin" />
      </div>
    )
  }
  if (error || !info) {
    return (
      <div className="container max-w-4xl py-8">
        <Card className="border-destructive/40 bg-destructive/5">
          <CardHeader>
            <CardTitle>Failed to load instance info</CardTitle>
            <CardDescription className="text-destructive">{error}</CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  return (
    <div className="container max-w-6xl py-6 space-y-4">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <Server className="h-6 w-6" />
          Your Instance
        </h1>
        <p className="text-sm text-muted-foreground mt-1">
          Your dedicated trading container — its identity, resources, and isolation.
        </p>
      </div>

      {/* Top summary card */}
      <Card className="border-primary/40 bg-primary/5">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <CardTitle className="text-lg">{info.network.subdomain || 'your-instance'}</CardTitle>
            <div className="flex items-center gap-2">
              <Badge variant="outline" className="border-emerald-500/50 text-emerald-300">
                <span className="mr-1.5 h-2 w-2 rounded-full bg-emerald-400 inline-block" />
                Running
              </Badge>
            </div>
          </div>
          <CardDescription>
            <CopyableValue value={info.network.url} mono={false} />
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <div className="text-xs text-muted-foreground">Uptime</div>
              <div className="font-medium">{fmtDuration(info.runtime.uptime_seconds)}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Image</div>
              <div className="font-mono text-xs">{info.runtime.image_sha || '—'}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Container</div>
              <div className="font-mono text-xs">{info.runtime.container_id || info.runtime.hostname || '—'}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Active broker</div>
              <div className="font-medium">{info.broker.active_broker || '—'}</div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Grid of detail cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Network identity */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Network className="h-4 w-4" />
              Network identity
            </CardTitle>
            <CardDescription>Dedicated to your instance — whitelist these at your broker.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div>
              <p className="text-xs font-semibold text-emerald-300 mb-1">IPv6 (per-customer)</p>
              <CopyableValue value={info.network.ipv6} />
            </div>
            {info.network.ipv4_primary && (
              <div>
                <p className="text-xs font-semibold text-emerald-300 mb-1">
                  IPv4 (dedicated — used only by IPv4-only brokers)
                </p>
                <CopyableValue value={info.network.ipv4_primary} />
              </div>
            )}
            {info.network.ipv4_secondary && (
              <div>
                <p className="text-xs font-semibold text-emerald-300 mb-1">IPv4 secondary (failover)</p>
                <CopyableValue value={info.network.ipv4_secondary} />
              </div>
            )}
            {/* Pool-routing note removed 2026-06-11: allocation is
                per-customer dedicated (per-port pinned); the pool list is
                infra inventory, not something customers whitelist. */}
          </CardContent>
        </Card>

        {/* Compute */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Cpu className="h-4 w-4" />
              Compute
            </CardTitle>
            <CardDescription>CPU + memory available to this container.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <StatRow
              label="CPU limit"
              value={
                info.compute.cpu_limit_cores != null
                  ? `${info.compute.cpu_limit_cores} cores`
                  : `${info.compute.cpu_count_host} cores (host)`
              }
            />
            <StatRow label="Load (1m avg)" value={info.compute.load_1m.toString()} />
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="text-muted-foreground">RAM used</span>
                <span>
                  {fmtBytes(info.compute.mem_used_bytes)} / {fmtBytes(info.compute.mem_total_bytes)}
                </span>
              </div>
              <Progress value={info.compute.mem_used_pct} className="h-2" />
              <p className="text-xs text-muted-foreground mt-1">{info.compute.mem_used_pct}% used</p>
            </div>
          </CardContent>
        </Card>

        {/* Storage */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <HardDrive className="h-4 w-4" />
              Storage
            </CardTitle>
            <CardDescription>Your container's disk — holds your encrypted DBs.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="text-muted-foreground">Used</span>
                <span>
                  {fmtBytes(info.storage.used_bytes)} / {fmtBytes(info.storage.total_bytes)}
                </span>
              </div>
              <Progress value={info.storage.used_pct} className="h-2" />
              <p className="text-xs text-muted-foreground mt-1">
                {info.storage.used_pct}% used · {fmtBytes(info.storage.free_bytes)} free
                {info.storage.path ? ` · ${info.storage.path}` : ''}
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Broker activity */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Activity className="h-4 w-4" />
              Broker activity
            </CardTitle>
            <CardDescription>Brokers saved on your instance.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <StatRow label="Active broker" value={info.broker.active_broker || '—'} />
            <StatRow label="Last activated" value={fmtTs(info.broker.last_activated_at)} />
            <StatRow label="Last auth" value={fmtTs(info.broker.last_auth_at)} />
            <div className="pt-2">
              <p className="text-xs font-semibold text-muted-foreground mb-1">Saved brokers</p>
              <div className="flex flex-wrap gap-1.5">
                {info.broker.saved_brokers.length === 0 && (
                  <span className="text-xs text-muted-foreground">none</span>
                )}
                {info.broker.saved_brokers.map((b) => (
                  <Badge key={b.broker} variant={b.status === 'active' ? 'default' : 'outline'}>
                    {b.broker}
                  </Badge>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Data sovereignty — the compliance card */}
      <Card className="border-emerald-500/30 bg-emerald-500/5">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Shield className="h-4 w-4 text-emerald-400" />
            Data sovereignty
          </CardTitle>
          <CardDescription>What's encrypted with a key unique to your instance.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="flex items-start gap-2">
            <Lock className="h-4 w-4 text-emerald-400 mt-0.5 shrink-0" />
            <div>
              <p>
                <strong>Per-instance encryption key</strong> ·{' '}
                {info.data_sovereignty.encryption_key_unique_per_instance
                  ? `present (${info.data_sovereignty.encryption_key_present_first8})`
                  : 'NOT SET'}
              </p>
              <p className="text-xs text-muted-foreground">
                Your <code>API_KEY_PEPPER</code> is a 64-char hex secret generated at provisioning and written
                into your container's env. It derives the Fernet cipher key that encrypts every credential
                below. Without it, no one — including AlphaQuark operators — can decrypt your data.
              </p>
            </div>
          </div>
          <div>
            <p className="text-xs font-semibold text-muted-foreground mb-1">Encrypted at rest</p>
            <ul className="text-xs space-y-1 list-disc list-inside">
              {info.data_sovereignty.encrypted_at_rest.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          </div>
          <div>
            <p className="text-xs font-semibold text-muted-foreground mb-1 flex items-center gap-1.5">
              <Database className="h-3 w-3" />
              Your data lives in
            </p>
            <ul className="text-xs space-y-1 list-disc list-inside">
              {info.data_sovereignty.stored_per_container_db.map((x) => (
                <li key={x} className="font-mono">
                  {x}
                </li>
              ))}
            </ul>
          </div>
        </CardContent>
      </Card>

      <Card className="border-slate-700/50">
        <CardContent className="pt-6">
          <p className="text-xs text-muted-foreground flex items-start gap-2">
            <Globe className="h-3 w-3 mt-0.5 shrink-0" />
            <span>
              Your instance is a dedicated container with a dedicated subdomain, IPv6 address, optional
              IPv4, and encrypted database. CPU and memory are reserved by Docker; storage is on the host's
              filesystem and isolated by container. The orchestration plane (provisioning, lifecycle) is
              operated by AlphaQuark on shared infrastructure. For a fully-isolated VPS or cloud instance,
              contact support to upgrade.
            </span>
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
