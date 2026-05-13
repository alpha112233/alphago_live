/**
 * DistributionInbox — webhook receivers for external signal publishers.
 *
 * Each inbox is a one-way pipe: an admin / strategy publisher POSTs
 * {symbol, action, quantity, ...} to the inbox's URL with its API key,
 * and our backend places the order on the chosen broker. The UI here
 * lets the customer:
 *
 *   • Create inboxes (one per publisher / per use case)
 *   • Copy the webhook URL + API key to share with the publisher
 *   • Pin a specific broker per inbox (else: follows active broker)
 *   • Optional IP allowlist (defence-in-depth against leaked keys)
 *   • Rotate the API key
 *   • View recent signal log + last status
 *
 * The plaintext API key is shown ONCE on create / rotate — after that
 * only the last 4 characters are visible. Same pattern Stripe et al use.
 */

import { ArrowLeft, CheckCircle2, ChevronDown, ChevronRight, Copy, Inbox, Loader2, Plus, RefreshCw, ScrollText, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  type DistributionInbox,
  type DistributionSignal,
  type InboxCreateResult,
  createInbox,
  deleteInbox,
  listInboxSignals,
  listInboxes,
  rotateInboxKey,
  updateInbox,
} from '@/api/distribution'
import { type SavedBroker, listSavedBrokers } from '@/api/brokerManager'
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
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { showToast } from '@/utils/toast'

// ---- Copy helper -----------------------------------------------------------

async function copyToClipboard(text: string, label: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    showToast.success(`${label} copied`)
  } catch {
    showToast.error(`Failed to copy ${label}`)
  }
}

function CopyableCode({ value, label }: { value: string; label?: string }) {
  return (
    <div className="flex items-center gap-2">
      {label && <span className="text-xs text-muted-foreground">{label}</span>}
      <code className="flex-1 px-2 py-1 bg-muted rounded text-xs break-all">{value}</code>
      <Button size="sm" variant="ghost" onClick={() => copyToClipboard(value, label || 'value')}>
        <Copy className="h-3 w-3" />
      </Button>
    </div>
  )
}

// ---- Page -----------------------------------------------------------------

export default function DistributionInboxPage() {
  const [inboxes, setInboxes] = useState<DistributionInbox[]>([])
  const [savedBrokers, setSavedBrokers] = useState<SavedBroker[]>([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [editingInbox, setEditingInbox] = useState<DistributionInbox | null>(null)
  const [keyDialog, setKeyDialog] = useState<InboxCreateResult | { id: number; api_key_plaintext: string; webhook_url: string; api_key_last4: string; name: string } | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<DistributionInbox | null>(null)

  async function refresh() {
    setLoading(true)
    try {
      const [list, brokers] = await Promise.all([
        listInboxes(),
        listSavedBrokers().catch(() => []),
      ])
      setInboxes(list)
      setSavedBrokers(brokers)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Failed to load inboxes: ${msg}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  async function handleRotate(inbox: DistributionInbox) {
    try {
      const result = await rotateInboxKey(inbox.id)
      setKeyDialog({
        id: inbox.id,
        api_key_plaintext: result.api_key_plaintext,
        api_key_last4: result.api_key_last4,
        webhook_url: buildWebhookUrl(inbox.inbox_slug),
        name: inbox.name,
      })
      refresh()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Rotate failed: ${msg}`)
    }
  }

  async function handleDelete(inbox: DistributionInbox) {
    try {
      await deleteInbox(inbox.id)
      showToast.success(`Deleted "${inbox.name}"`)
      setConfirmDelete(null)
      refresh()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Delete failed: ${msg}`)
    }
  }

  return (
    <div className="container mx-auto py-8 max-w-5xl px-4">
      <div className="flex items-center gap-3 mb-6">
        <Link to="/dashboard">
          <Button variant="ghost" size="sm">
            <ArrowLeft className="h-4 w-4 mr-1" />
            Dashboard
          </Button>
        </Link>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Inbox className="h-6 w-6" />
          Distribution Inbox
        </h1>
        <div className="ml-auto">
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4 mr-1" />
            New Inbox
          </Button>
        </div>
      </div>

      <Card className="mb-6 border-primary/40 bg-primary/5">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">What this is</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground space-y-2">
          <p>
            An external publisher (a strategy provider, an admin's automation, your own bot) can POST
            trade signals to an inbox URL. Each signal is validated against the inbox's API key, deduped
            on <code className="text-xs">signal_id</code>, and placed as an order on your chosen broker.
          </p>
          <p>
            Create one inbox per publisher (or per use case). Share the URL + API key with them. The signal
            payload <strong>must</strong> include the final <code className="text-xs">quantity</code> in
            shares/contracts — there's no multiplier on this side. The publisher is responsible for sizing
            (including lot rounding for F&amp;O).
          </p>
          <details className="text-xs">
            <summary className="cursor-pointer">Expected payload shape</summary>
            <pre className="bg-background border rounded p-2 mt-2 overflow-x-auto">
{`POST <webhook_url>
Authorization: Bearer <api_key>
Content-Type: application/json

{
  "signal_id":    "<unique, used to dedupe retries>",
  "symbol":       "RELIANCE" | "NIFTY25500CE",
  "exchange":     "NSE" | "NFO" | "BSE" | ...,
  "action":       "BUY" | "SELL",
  "quantity":     100,            // FINAL shares/contracts to trade
  "product":      "MIS" | "CNC" | "NRML",
  "pricetype":    "MARKET" | "LIMIT" | "SL" | "SL-M",
  "price":        0,              // for LIMIT/SL
  "trigger_price": 0              // for SL/SL-M
}`}
            </pre>
          </details>
        </CardContent>
      </Card>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading...
        </div>
      ) : inboxes.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            No inboxes yet. Create one to start receiving signals from a publisher.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {inboxes.map((inbox) => (
            <InboxRow
              key={inbox.id}
              inbox={inbox}
              savedBrokers={savedBrokers}
              onEdit={() => setEditingInbox(inbox)}
              onRotate={() => handleRotate(inbox)}
              onDelete={() => setConfirmDelete(inbox)}
            />
          ))}
        </div>
      )}

      {/* Create sheet */}
      <CreateInboxSheet
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        savedBrokers={savedBrokers}
        onCreated={(result) => {
          setCreateOpen(false)
          setKeyDialog(result)
          refresh()
        }}
      />

      {/* Edit sheet */}
      <EditInboxSheet
        inbox={editingInbox}
        savedBrokers={savedBrokers}
        onClose={() => setEditingInbox(null)}
        onSaved={() => {
          setEditingInbox(null)
          refresh()
        }}
      />

      {/* Plaintext-key dialog (shown once after create / rotate) */}
      <Dialog open={keyDialog !== null} onOpenChange={(o) => !o && setKeyDialog(null)}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>API key — copy now</DialogTitle>
            <DialogDescription>
              This is the only time we'll show this plaintext key. After you close this dialog the key is
              hashed at rest; we can't recover it. If you lose it, rotate to get a new one.
            </DialogDescription>
          </DialogHeader>
          {keyDialog && (
            <div className="space-y-3">
              <div>
                <Label className="text-xs">Webhook URL</Label>
                <CopyableCode value={keyDialog.webhook_url} />
              </div>
              <div>
                <Label className="text-xs">API key</Label>
                <CopyableCode value={keyDialog.api_key_plaintext} />
              </div>
              <p className="text-xs text-muted-foreground">
                Send both to your publisher. The publisher uses{' '}
                <code className="text-xs">Authorization: Bearer &lt;api_key&gt;</code> on every POST.
              </p>
            </div>
          )}
          <DialogFooter>
            <Button onClick={() => setKeyDialog(null)}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <AlertDialog open={confirmDelete !== null} onOpenChange={(o) => !o && setConfirmDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete inbox?</AlertDialogTitle>
            <AlertDialogDescription>
              "{confirmDelete?.name}" will be removed permanently, along with its signal log. The publisher
              POSTing to this inbox will start getting 404. Cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => confirmDelete && handleDelete(confirmDelete)}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

// ---- Per-inbox row --------------------------------------------------------

function buildWebhookUrl(slug: string): string {
  return `${window.location.origin}/distribution/inbox/${slug}`
}

interface InboxRowProps {
  inbox: DistributionInbox
  savedBrokers: SavedBroker[]
  onEdit: () => void
  onRotate: () => void
  onDelete: () => void
}

function InboxRow({ inbox, onEdit, onRotate, onDelete }: InboxRowProps) {
  const [showSignals, setShowSignals] = useState(false)

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start gap-2">
          <div className="flex-1">
            <CardTitle className="text-base">{inbox.name}</CardTitle>
            <div className="flex items-center gap-2 mt-1">
              <Badge variant={inbox.status === 'active' ? 'default' : 'secondary'}>
                {inbox.status}
              </Badge>
              <Badge variant="outline">
                routes to: {inbox.broker_override || 'active broker'}
              </Badge>
              {inbox.last_signal_at && (
                <span className="text-xs text-muted-foreground">
                  last: {new Date(inbox.last_signal_at).toLocaleString('en-IN')} ·{' '}
                  {inbox.last_signal_status} · {inbox.signal_count_total} total
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-1">
            <Button size="sm" variant="outline" onClick={onEdit}>Edit</Button>
            <Button size="sm" variant="outline" onClick={onRotate} title="Rotate API key">
              <RefreshCw className="h-3 w-3" />
            </Button>
            <Button size="sm" variant="ghost" onClick={onDelete} title="Delete">
              <Trash2 className="h-3 w-3 text-destructive" />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <CopyableCode value={buildWebhookUrl(inbox.inbox_slug)} label="URL:" />
        <div className="text-xs text-muted-foreground">
          API key: <code className="text-xs">…{inbox.api_key_last4}</code>{' '}
          <span className="text-muted-foreground/60">(rotate to view plaintext)</span>
          {inbox.allowed_ips && (
            <span className="ml-3">
              IP allowlist: <code className="text-xs">{inbox.allowed_ips}</code>
            </span>
          )}
        </div>
        <button
          type="button"
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowSignals((s) => !s)}
        >
          {showSignals ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <ScrollText className="h-3 w-3" />
          Recent signals
        </button>
        {showSignals && <InboxSignals inboxId={inbox.id} />}
      </CardContent>
    </Card>
  )
}

function InboxSignals({ inboxId }: { inboxId: number }) {
  const [signals, setSignals] = useState<DistributionSignal[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    listInboxSignals(inboxId, 20)
      .then(setSignals)
      .catch(() => setSignals([]))
      .finally(() => setLoading(false))
  }, [inboxId])

  if (loading) return <div className="text-xs text-muted-foreground py-2"><Loader2 className="h-3 w-3 animate-spin inline mr-1" />Loading…</div>
  if (signals.length === 0) return <div className="text-xs text-muted-foreground py-2">No signals received yet.</div>

  return (
    <div className="border rounded mt-1 max-h-60 overflow-y-auto">
      {signals.map((s) => (
        <div key={s.id} className="px-2 py-1.5 border-b last:border-b-0 text-xs flex items-start gap-2">
          <Badge variant={s.status === 'placed' ? 'default' : s.status === 'duplicate' ? 'secondary' : 'destructive'} className="shrink-0">
            {s.status}
          </Badge>
          <div className="flex-1 space-y-0.5">
            <div className="font-mono">
              {(s.payload.action as string) || '?'} {(s.payload.symbol as string) || '?'} qty=
              {(s.payload.quantity as number) ?? '?'}
            </div>
            <div className="text-muted-foreground">
              {new Date(s.received_at).toLocaleString('en-IN')} ·{' '}
              {s.broker_order_id ? `order ${s.broker_order_id}` : (s.error_message || '—')}
              {s.broker_used && <span className="ml-2">via {s.broker_used}</span>}
              {s.src_ip && <span className="ml-2 text-muted-foreground/70">from {s.src_ip}</span>}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

// ---- Create sheet ---------------------------------------------------------

interface CreateInboxSheetProps {
  open: boolean
  onClose: () => void
  savedBrokers: SavedBroker[]
  onCreated: (result: InboxCreateResult) => void
}

function CreateInboxSheet({ open, onClose, savedBrokers, onCreated }: CreateInboxSheetProps) {
  const [name, setName] = useState('')
  const [brokerOverride, setBrokerOverride] = useState('')
  const [allowedIps, setAllowedIps] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open) {
      setName(''); setBrokerOverride(''); setAllowedIps(''); setSaving(false)
    }
  }, [open])

  async function handleCreate() {
    if (!name.trim()) {
      showToast.error('Name is required')
      return
    }
    setSaving(true)
    try {
      const result = await createInbox({
        name: name.trim(),
        broker_override: brokerOverride || null,
        allowed_ips: allowedIps.trim() || null,
      })
      onCreated(result)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Create failed: ${msg}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="overflow-y-auto sm:max-w-md">
        <SheetHeader>
          <SheetTitle>New Distribution Inbox</SheetTitle>
          <SheetDescription>
            Each inbox is a separate webhook URL + API key. Use different inboxes for different publishers
            so you can revoke independently and see per-publisher signal logs.
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-4 mt-6">
          <div>
            <Label htmlFor="ib-name">Name</Label>
            <Input id="ib-name" placeholder="e.g. Mukul's swing strategy" value={name} onChange={(e) => setName(e.target.value)} />
            <p className="text-xs text-muted-foreground mt-1">Display label, just for your reference.</p>
          </div>
          <div>
            <Label htmlFor="ib-broker">Route to broker (optional)</Label>
            <select
              id="ib-broker"
              value={brokerOverride}
              onChange={(e) => setBrokerOverride(e.target.value)}
              className="w-full mt-1 px-3 py-2 border rounded-md bg-background text-sm"
            >
              <option value="">(use my active broker)</option>
              {savedBrokers.map((b) => (
                <option key={b.broker} value={b.broker}>{b.broker}</option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground mt-1">
              Pin signals from this inbox to a specific broker. Leave blank to follow your active broker.
            </p>
          </div>
          <div>
            <Label htmlFor="ib-ips">IP allowlist (optional)</Label>
            <Input
              id="ib-ips"
              placeholder="e.g. 203.0.113.5, 198.51.100.0/24"
              value={allowedIps}
              onChange={(e) => setAllowedIps(e.target.value)}
            />
            <p className="text-xs text-muted-foreground mt-1">
              Comma-separated IPs or CIDRs. Leave blank to allow any source IP.
            </p>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-6">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={handleCreate} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <CheckCircle2 className="h-4 w-4 mr-1" />}
            Create
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

// ---- Edit sheet -----------------------------------------------------------

interface EditInboxSheetProps {
  inbox: DistributionInbox | null
  savedBrokers: SavedBroker[]
  onClose: () => void
  onSaved: () => void
}

function EditInboxSheet({ inbox, savedBrokers, onClose, onSaved }: EditInboxSheetProps) {
  const [name, setName] = useState('')
  const [brokerOverride, setBrokerOverride] = useState('')
  const [allowedIps, setAllowedIps] = useState('')
  const [status, setStatus] = useState<'active' | 'disabled'>('active')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (inbox) {
      setName(inbox.name)
      setBrokerOverride(inbox.broker_override || '')
      setAllowedIps(inbox.allowed_ips || '')
      setStatus(inbox.status)
    }
  }, [inbox])

  async function handleSave() {
    if (!inbox) return
    setSaving(true)
    try {
      await updateInbox(inbox.id, {
        name: name.trim() || undefined,
        broker_override: brokerOverride || null,
        allowed_ips: allowedIps.trim() || null,
        status,
      })
      showToast.success('Inbox updated')
      onSaved()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      showToast.error(`Update failed: ${msg}`)
    } finally {
      setSaving(false)
    }
  }

  const slug = inbox?.inbox_slug || ''
  const webhookUrl = useMemo(() => (slug ? buildWebhookUrl(slug) : ''), [slug])

  return (
    <Sheet open={inbox !== null} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="overflow-y-auto sm:max-w-md">
        <SheetHeader>
          <SheetTitle>Edit Inbox</SheetTitle>
          <SheetDescription>
            Change routing, name, IP allowlist, or pause this inbox. The API key + URL stay the same; rotate
            the key from the inbox list if you need a fresh one.
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-4 mt-6">
          {webhookUrl && (
            <div>
              <Label className="text-xs">Webhook URL (unchanged)</Label>
              <CopyableCode value={webhookUrl} />
            </div>
          )}
          <div>
            <Label htmlFor="ie-name">Name</Label>
            <Input id="ie-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <Label htmlFor="ie-broker">Route to broker</Label>
            <select
              id="ie-broker"
              value={brokerOverride}
              onChange={(e) => setBrokerOverride(e.target.value)}
              className="w-full mt-1 px-3 py-2 border rounded-md bg-background text-sm"
            >
              <option value="">(use my active broker)</option>
              {savedBrokers.map((b) => (
                <option key={b.broker} value={b.broker}>{b.broker}</option>
              ))}
            </select>
          </div>
          <div>
            <Label htmlFor="ie-ips">IP allowlist</Label>
            <Input id="ie-ips" placeholder="e.g. 203.0.113.5, 198.51.100.0/24" value={allowedIps} onChange={(e) => setAllowedIps(e.target.value)} />
          </div>
          <div>
            <Label htmlFor="ie-status">Status</Label>
            <select
              id="ie-status"
              value={status}
              onChange={(e) => setStatus(e.target.value as 'active' | 'disabled')}
              className="w-full mt-1 px-3 py-2 border rounded-md bg-background text-sm"
            >
              <option value="active">active — accepting signals</option>
              <option value="disabled">disabled — incoming POSTs rejected with 403</option>
            </select>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-6">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={handleSave} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <CheckCircle2 className="h-4 w-4 mr-1" />}
            Save
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}
