/**
 * BrokerManager — multi-broker credential management page (alphago_live fork).
 *
 * Lets a user save credentials for multiple brokers, switch the active one,
 * trigger broker login, and remove. Per-broker setup instructions render in
 * a side panel. TOTP seed entry is exposed for brokers that support
 * auto-login (the seed is stored encrypted; auto-login adapters will use it
 * in phase 9 — for now the seed is just stored and not yet consumed).
 *
 * Backed by /api/broker/credentials/* endpoints (see api/brokerManager.ts).
 */

import { ArrowLeft, CheckCircle2, Copy, ExternalLink, KeyRound, Loader2, Network, Plus, RefreshCw, Trash2, XCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  type AutoLoginSchedulerStatus,
  type BrokerField,
  type BrokerInstructions,
  type HostInfo,
  type SavedBroker,
  type SaveBrokerPayload,
  type SupportedBroker,
  activateBroker,
  autoLogin,
  deleteBroker,
  getAutoLoginSchedulerStatus,
  getBrokerInstructions,
  getHostInfo,
  listSavedBrokers,
  listSupportedBrokers,
  saveBroker,
} from '@/api/brokerManager'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { showToast } from '@/utils/toast'

// ----- helpers ---------------------------------------------------------------

function StatusBadge({ status }: { status: SavedBroker['status'] }) {
  const map = {
    active: { label: 'Active', variant: 'default' as const, icon: CheckCircle2 },
    saved: { label: 'Saved', variant: 'secondary' as const, icon: KeyRound },
    expired: { label: 'Expired', variant: 'outline' as const, icon: RefreshCw },
    error: { label: 'Error', variant: 'destructive' as const, icon: XCircle },
  }
  const { label, variant, icon: Icon } = map[status]
  return (
    <Badge variant={variant} className="gap-1">
      <Icon className="h-3 w-3" />
      {label}
    </Badge>
  )
}

function formatRelative(iso: string | null): string {
  if (!iso) return 'never'
  const then = new Date(iso).getTime()
  const diff = Date.now() - then
  if (Number.isNaN(diff)) return iso
  if (diff < 60_000) return 'just now'
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

// ----- save / edit sheet ----------------------------------------------------

interface SheetState {
  open: boolean
  // null = add-new mode (broker picker shown). Otherwise edit-mode for that broker.
  editingBroker: string | null
}

function brokerLabel(name: string): string {
  // Light-weight prettifier; full label table can come later.
  const overrides: Record<string, string> = {
    fivepaisaxts: '5paisa XTS',
    icicidirect: 'ICICI Direct',
    indmoney: 'IndMoney',
    dhan_sandbox: 'Dhan (Sandbox)',
    aliceblue: 'Alice Blue',
    compositedge: 'CompositEdge',
    definedge: 'Definedge',
    motilal: 'Motilal Oswal',
    paytm: 'Paytm Money',
    angel: 'Angel One',
  }
  if (overrides[name]) return overrides[name]
  return name.charAt(0).toUpperCase() + name.slice(1)
}

interface BrokerFormSheetProps {
  state: SheetState
  onClose: () => void
  onSaved: () => void
  supported: SupportedBroker[]
}

function BrokerFormSheet({ state, onClose, onSaved, supported }: BrokerFormSheetProps) {
  const [selectedBroker, setSelectedBroker] = useState<string>('')
  const [instructions, setInstructions] = useState<BrokerInstructions | null>(null)
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({})
  const [activateOnSave, setActivateOnSave] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const isEdit = state.editingBroker !== null
  const broker = isEdit ? state.editingBroker! : selectedBroker

  // Reset when sheet opens/closes
  useEffect(() => {
    if (!state.open) {
      setSelectedBroker('')
      setInstructions(null)
      setFieldValues({})
      setActivateOnSave(false)
      return
    }
    if (isEdit) {
      setSelectedBroker(state.editingBroker!)
    }
  }, [state.open, state.editingBroker, isEdit])

  // Fetch per-broker fields + instructions on broker selection
  useEffect(() => {
    if (!broker) {
      setInstructions(null)
      return
    }
    setLoading(true)
    getBrokerInstructions(broker)
      .then((d) => {
        setInstructions(d)
        // Initialize empty form values for the broker's required fields
        const init: Record<string, string> = {}
        d.fields.forEach((f) => {
          init[f.name] = ''
        })
        setFieldValues(init)
      })
      .catch((e) => showToast.error(`Failed to load broker details: ${e.message}`))
      .finally(() => setLoading(false))
  }, [broker])

  function setField(name: string, value: string) {
    setFieldValues((prev) => ({ ...prev, [name]: value }))
  }

  async function handleSave() {
    if (!broker || !instructions) return

    // Required-field check. On Edit the placeholder is "Leave blank to
    // keep existing" — blanks mean "preserve what's already in the DB",
    // not "delete the value". So skip the required check entirely on
    // Edit; the backend cross-references the existing row and rejects
    // only when there's truly no saved value to preserve.
    if (!isEdit) {
      const missing = instructions.fields
        .filter((f) => f.required && !fieldValues[f.name]?.trim())
        .map((f) => f.label)
      if (missing.length > 0) {
        showToast.error(`Required: ${missing.join(', ')}`)
        return
      }
    }

    // Build payload — flatten "extra.<key>" fields into a sub-object
    const payload: SaveBrokerPayload = {
      broker,
      api_key: fieldValues.api_key || '',
      activate: activateOnSave,
    }
    const extra: Record<string, string> = {}
    for (const [k, v] of Object.entries(fieldValues)) {
      if (!v?.trim()) continue
      if (k === 'api_key') continue
      if (k.startsWith('extra.')) {
        extra[k.slice(6)] = v
        continue
      }
      switch (k) {
        case 'api_secret': payload.api_secret = v; break
        case 'api_key_market': payload.api_key_market = v; break
        case 'api_secret_market': payload.api_secret_market = v; break
        case 'client_code': payload.client_code = v; break
        case 'totp_seed': payload.totp_seed = v; break
        default: extra[k] = v
      }
    }
    if (Object.keys(extra).length > 0) payload.extra = extra

    setSaving(true)
    try {
      await saveBroker(payload)
      showToast.success(
        activateOnSave
          ? `${brokerLabel(broker)} saved and activated.`
          : `${brokerLabel(broker)} saved.`
      )
      onSaved()
      onClose()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Save failed: ${msg}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Sheet open={state.open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-2xl overflow-y-auto">
        <SheetHeader>
          <SheetTitle>{isEdit ? `Update ${brokerLabel(broker)}` : 'Add Broker'}</SheetTitle>
          <SheetDescription>
            {isEdit
              ? `Update saved credentials for ${brokerLabel(broker)}.`
              : 'Pick a broker, then enter the credentials you got from its developer console.'}
          </SheetDescription>
        </SheetHeader>

        <div className="grid gap-6 py-6 px-4">
          {!isEdit && (
            <div className="space-y-2">
              <Label htmlFor="broker-picker">Broker</Label>
              <Select value={selectedBroker} onValueChange={setSelectedBroker}>
                <SelectTrigger id="broker-picker">
                  <SelectValue placeholder="Select a broker..." />
                </SelectTrigger>
                <SelectContent>
                  {supported.map((b) => (
                    <SelectItem key={b.broker} value={b.broker}>
                      {brokerLabel(b.broker)}
                      {!b.has_instructions && <span className="text-xs text-muted-foreground ml-2">(no detailed setup guide)</span>}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {loading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading broker setup...
            </div>
          )}

          {instructions && (
            <>
              {/* Instructions */}
              <div className="rounded-md border bg-muted/50 p-4 space-y-3">
                <h4 className="text-sm font-medium">Setup instructions</h4>
                <pre className="whitespace-pre-wrap text-xs leading-relaxed font-sans text-muted-foreground">
                  {instructions.instructions_md}
                </pre>

                {/* The two values the customer needs to paste into their
                    broker's developer console — surface them prominently
                    rather than leave the customer to scroll through the
                    markdown looking for them. */}
                <div className="space-y-2 pt-1 border-t">
                  {instructions.client_ipv6 && (
                    <CopyableCode
                      value={instructions.client_ipv6}
                      label="Whitelist IP:"
                    />
                  )}
                  {instructions.redirect_url && (
                    <CopyableCode
                      value={instructions.redirect_url}
                      label="Redirect URL:"
                    />
                  )}
                </div>
              </div>

              <Separator />

              {/* Form fields */}
              <div className="space-y-4">
                {instructions.fields.map((f: BrokerField) => (
                  <div key={f.name} className="space-y-1.5">
                    <Label htmlFor={`field-${f.name}`}>
                      {f.label}
                      {f.required && <span className="text-destructive ml-0.5">*</span>}
                    </Label>
                    <Input
                      id={`field-${f.name}`}
                      type={f.type === 'password' ? 'password' : 'text'}
                      value={fieldValues[f.name] || ''}
                      onChange={(e) => setField(f.name, e.target.value)}
                      placeholder={isEdit ? 'Leave blank to keep existing' : ''}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    {f.help && <p className="text-xs text-muted-foreground">{f.help}</p>}
                  </div>
                ))}

                <div className="flex items-center gap-2 pt-2">
                  <input
                    type="checkbox"
                    id="activate-on-save"
                    className="h-4 w-4"
                    checked={activateOnSave}
                    onChange={(e) => setActivateOnSave(e.target.checked)}
                  />
                  <Label htmlFor="activate-on-save" className="cursor-pointer text-sm font-normal">
                    Make this the active broker after saving
                  </Label>
                </div>
              </div>
            </>
          )}
        </div>

        <div className="flex gap-2 px-4 pb-4">
          <Button onClick={handleSave} disabled={!instructions || saving} className="flex-1">
            {saving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            {isEdit ? 'Update' : 'Save'}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

// ----- main page ------------------------------------------------------------

function CopyableCode({ value, label }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  if (!value) return null
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      // Fallback for older browsers / non-secure contexts
      const ta = document.createElement('textarea')
      ta.value = value
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    }
  }
  return (
    <div className="flex items-center gap-2 group">
      {label && <span className="text-xs text-muted-foreground shrink-0">{label}</span>}
      <code className="flex-1 text-xs bg-background border rounded px-2 py-1 font-mono break-all select-all">
        {value}
      </code>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        onClick={copy}
        className="shrink-0 h-7 px-2"
        title="Copy to clipboard"
      >
        {copied ? <CheckCircle2 className="h-3 w-3 text-green-500" /> : <Copy className="h-3 w-3" />}
      </Button>
    </div>
  )
}


export default function BrokerManager() {
  const [saved, setSaved] = useState<SavedBroker[]>([])
  const [supported, setSupported] = useState<SupportedBroker[]>([])
  const [hostInfo, setHostInfo] = useState<HostInfo | null>(null)
  const [schedulerStatus, setSchedulerStatus] = useState<AutoLoginSchedulerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [sheetState, setSheetState] = useState<SheetState>({ open: false, editingBroker: null })
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [busyBroker, setBusyBroker] = useState<string | null>(null)

  async function refresh() {
    setLoading(true)
    try {
      const [s, sup, hi, sched] = await Promise.all([
        listSavedBrokers(),
        listSupportedBrokers(),
        getHostInfo().catch(() => null),
        getAutoLoginSchedulerStatus().catch(() => null),
      ])
      setSaved(s)
      setSupported(sup)
      setHostInfo(hi)
      setSchedulerStatus(sched)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Failed to load brokers: ${msg}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  // Brokers not yet saved — used to filter the Add picker
  const availableToAdd = useMemo(() => {
    const savedSet = new Set(saved.map((b) => b.broker))
    return supported.filter((b) => !savedSet.has(b.broker))
  }, [saved, supported])

  async function handleActivate(broker: string) {
    setBusyBroker(broker)
    try {
      await activateBroker(broker)
      showToast.success(`${brokerLabel(broker)} is now the active broker.`)
      refresh()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Activate failed: ${msg}`)
    } finally {
      setBusyBroker(null)
    }
  }

  async function handleAutoLogin(broker: string) {
    setBusyBroker(broker)
    try {
      const r = await autoLogin(broker)
      showToast.success(
        `${brokerLabel(broker)} auto-login OK${r.expires_at ? ` — token valid until ${r.expires_at}` : ''}`
      )
      refresh()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Auto-login failed: ${msg}`)
    } finally {
      setBusyBroker(null)
    }
  }

  async function handleDelete(broker: string) {
    setBusyBroker(broker)
    try {
      await deleteBroker(broker)
      showToast.success(`Removed ${brokerLabel(broker)}.`)
      refresh()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Delete failed: ${msg}`)
    } finally {
      setBusyBroker(null)
      setConfirmDelete(null)
    }
  }

  function handleConnect(_broker: string) {
    // After activate, the broker login UI at /broker will use the now-active
    // broker's credentials. Redirect there to trigger the OAuth / TOTP flow.
    window.location.href = '/broker'
  }

  return (
    <div className="container mx-auto py-6 px-4 max-w-6xl">
      <Link
        to="/broker"
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground mb-4"
      >
        <ArrowLeft className="h-4 w-4 mr-1" />
        Back to broker login
      </Link>
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Manage Brokers</h1>
          <p className="text-muted-foreground mt-1">
            Save credentials for multiple brokers and switch between them. Only one broker can be
            active at a time.
          </p>
        </div>
        <Button onClick={() => setSheetState({ open: true, editingBroker: null })} disabled={availableToAdd.length === 0}>
          <Plus className="h-4 w-4 mr-2" />
          Add Broker
        </Button>
      </div>

      {/* Your assigned static IPv6 — same address every broker should whitelist,
          unique to this Alpha Live instance. Brokers that enforce per-app IP
          whitelisting (Upstox, Dhan, Fyers, Kotak, IIFL) need this in their
          developer console before any auto-login or trading call works. */}
      {hostInfo?.client_ipv6 && (
        <Card className="mb-6 border-primary/40 bg-primary/5">
          <CardHeader className="pb-3">
            <CardTitle className="text-lg flex items-center gap-2">
              <Network className="h-4 w-4" />
              IP addresses to whitelist at your broker
            </CardTitle>
            <CardDescription>
              Most brokers accept your dedicated IPv6 below. A few are IPv4-only and need
              the shared server IPv4 instead. Each broker's setup guide tells you which to
              use — paste the right one in your broker's developer console under "Whitelisted
              IPs" (or equivalent) before connecting.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <p className="text-xs font-semibold text-emerald-300 mb-1">
                Dedicated IPv6 (per-customer)
              </p>
              <CopyableCode value={hostInfo.client_ipv6} label="IPv6:" />
              <p className="text-xs text-muted-foreground pt-1">
                Use for: Dhan, Upstox, Fyers, AngelOne, Groww, Kotak, IIFL, ICICI Direct,
                HDFC InvestRight, Definedge, FivePaisa, Paytm Money, Zerodha, Flattrade,
                IndMoney. Unique to your instance — no scarcity, no shared-IP risk.
              </p>
            </div>
            {hostInfo.client_ipv4_pool && hostInfo.client_ipv4_pool.length > 1 ? (
              <div>
                <p className="text-xs font-semibold text-emerald-300 mb-1">
                  IPv4 ISP Pool (whitelist all at IPv4-only brokers)
                </p>
                <div className="space-y-1">
                  {hostInfo.client_ipv4_pool.map((ip) => (
                    <CopyableCode
                      key={ip}
                      value={ip}
                      label={ip === hostInfo.client_ipv4_primary ? "IPv4 (preferred):" : "IPv4:"}
                    />
                  ))}
                </div>
                <p className="text-xs text-muted-foreground pt-1">
                  Use for: <strong>Arihant Capital</strong> and any other broker whose API
                  endpoint is IPv4-only. Outbound calls route via a Vodafone Idea ISP pool in
                  Mumbai, exclusive to our infrastructure. Whitelist <strong>all</strong> IPs
                  above at each IPv4-only broker — any of them may show up on a given request.
                </p>
              </div>
            ) : hostInfo.client_ipv4_primary ? (
              <>
                <div>
                  <p className="text-xs font-semibold text-emerald-300 mb-1">
                    Dedicated IPv4 — Primary (per-customer)
                  </p>
                  <CopyableCode value={hostInfo.client_ipv4_primary} label="IPv4 (primary):" />
                  <p className="text-xs text-muted-foreground pt-1">
                    Use for: <strong>Arihant Capital</strong> and any other broker whose API
                    endpoint is IPv4-only. Routed via a Vodafone Idea ISP IP from our Mumbai
                    pool, dedicated to your account.
                  </p>
                </div>
                {hostInfo.client_ipv4_secondary && (
                  <div>
                    <p className="text-xs font-semibold text-emerald-300 mb-1">
                      Dedicated IPv4 — Secondary (failover)
                    </p>
                    <CopyableCode value={hostInfo.client_ipv4_secondary} label="IPv4 (secondary):" />
                    <p className="text-xs text-muted-foreground pt-1">
                      Failover IP. Whitelist this at the same broker as your primary. If the
                      primary path is unreachable (Decodo outage, IP issue), outbound calls
                      transparently use this one. No action needed from you during failover —
                      just keep both whitelisted.
                    </p>
                  </div>
                )}
              </>
            ) : hostInfo.shared_host_ipv4 ? (
              <div>
                <p className="text-xs font-semibold text-amber-300 mb-1">
                  Static IPv4 (shared across customers on this server)
                </p>
                <CopyableCode value={hostInfo.shared_host_ipv4} label="IPv4:" />
                <p className="text-xs text-muted-foreground pt-1">
                  Use for: <strong>Arihant Capital</strong> (and any other broker whose API
                  endpoint is IPv4-only). Every customer on this server shares this IP —
                  contact support to upgrade to a dedicated IPv4.
                </p>
              </div>
            ) : null}
            {hostInfo.redirect_url_pattern && (
              <div>
                <p className="text-xs font-semibold text-slate-300 mb-1">
                  OAuth redirect URL pattern
                </p>
                <CopyableCode
                  value={hostInfo.redirect_url_pattern}
                  label="Redirect URL:"
                />
                <p className="text-xs text-muted-foreground pt-1">
                  The <code className="text-xs">&lt;broker&gt;</code> becomes the actual
                  broker name (e.g. <code className="text-xs">/upstox/callback</code>).
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Daily auto-login scheduler — surfaces the next run so customers
          can verify their saved TOTP seeds will actually fire pre-market. */}
      {schedulerStatus?.enabled && (
        <div className="mb-4 text-xs text-muted-foreground">
          <span className="font-medium">Auto-login scheduler:</span>{' '}
          {schedulerStatus.running ? 'running' : 'idle'}
          {schedulerStatus.next_run && (
            <>
              {' · '}next run{' '}
              <code className="text-xs">
                {new Date(schedulerStatus.next_run).toLocaleString('en-IN', {
                  timeZone: 'Asia/Kolkata',
                  weekday: 'short',
                  hour: '2-digit',
                  minute: '2-digit',
                  hour12: false,
                })}{' '}
                IST
              </code>
            </>
          )}
          {' · '}each saved broker is refreshed before market open.
        </div>
      )}

      {loading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading...
        </div>
      )}

      {!loading && saved.length === 0 && (
        <Card>
          <CardContent className="py-12 text-center">
            <KeyRound className="h-12 w-12 mx-auto text-muted-foreground mb-3" />
            <p className="text-muted-foreground mb-4">No brokers saved yet.</p>
            <Button onClick={() => setSheetState({ open: true, editingBroker: null })}>
              <Plus className="h-4 w-4 mr-2" />
              Add your first broker
            </Button>
          </CardContent>
        </Card>
      )}

      {!loading && saved.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {saved.map((b) => (
            <Card key={b.broker} className={b.status === 'active' ? 'border-primary' : undefined}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between">
                  <CardTitle className="text-lg">{brokerLabel(b.broker)}</CardTitle>
                  <StatusBadge status={b.status} />
                </div>
                <CardDescription className="text-xs">
                  {b.has_api_key ? 'API key set' : 'No API key'}
                  {b.has_totp_seed && ' · TOTP seed saved'}
                  {b.client_code && ` · ${b.client_code}`}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="text-xs text-muted-foreground space-y-0.5">
                  <div>Last auth: {formatRelative(b.last_auth_at)}</div>
                  <div>Last activated: {formatRelative(b.last_activated_at)}</div>
                  {b.last_error && (
                    <div className="text-destructive space-y-1">
                      <div>Error: {b.last_error}</div>
                      <button
                        type="button"
                        className="underline hover:no-underline text-xs"
                        onClick={() => setConfirmDelete(b.broker)}
                      >
                        Delete & re-add credentials
                      </button>
                    </div>
                  )}
                </div>

                <div className="flex flex-wrap gap-2">
                  {b.status === 'active' ? (
                    <Button size="sm" onClick={() => handleConnect(b.broker)} className="flex-1">
                      <ExternalLink className="h-3 w-3 mr-1" />
                      Connect
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      onClick={() => handleActivate(b.broker)}
                      disabled={busyBroker === b.broker}
                      className="flex-1"
                    >
                      {busyBroker === b.broker ? (
                        <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                      ) : (
                        <CheckCircle2 className="h-3 w-3 mr-1" />
                      )}
                      Make Active
                    </Button>
                  )}
                  {/* Auto-login button — only shown for brokers that have a
                      TOTP seed saved AND have an adapter implemented. The
                      backend returns 501 for non-implemented brokers, but
                      we gate client-side too to avoid the wasted call.
                      IndMoney is the one exception: it uses a long-lived
                      access token instead of TOTP, so we surface the button
                      regardless of has_totp_seed. */}
                  {((b.has_totp_seed && ['upstox', 'kotak', 'zerodha', 'dhan', 'fyers', 'aliceblue', 'groww', 'flattrade'].includes(b.broker)) || b.broker === 'indmoney') && (
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => handleAutoLogin(b.broker)}
                      disabled={busyBroker === b.broker}
                      title="Run automated login using your saved TOTP seed"
                    >
                      {busyBroker === b.broker ? (
                        <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                      ) : (
                        <RefreshCw className="h-3 w-3 mr-1" />
                      )}
                      Auto-login
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setSheetState({ open: true, editingBroker: b.broker })}
                  >
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setConfirmDelete(b.broker)}
                    disabled={busyBroker === b.broker}
                  >
                    <Trash2 className="h-3 w-3" />
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <BrokerFormSheet
        state={sheetState}
        supported={availableToAdd}
        onClose={() => setSheetState({ open: false, editingBroker: null })}
        onSaved={refresh}
      />

      <AlertDialog open={confirmDelete !== null} onOpenChange={(o) => !o && setConfirmDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove {confirmDelete ? brokerLabel(confirmDelete) : ''}?</AlertDialogTitle>
            <AlertDialogDescription>
              This deletes the saved credentials from this server. You'll need to re-enter them to
              use this broker again. Existing trades and history are not affected.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => confirmDelete && handleDelete(confirmDelete)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
