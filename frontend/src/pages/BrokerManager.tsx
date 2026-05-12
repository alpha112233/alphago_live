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

import { CheckCircle2, ExternalLink, KeyRound, Loader2, Plus, RefreshCw, Trash2, XCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import {
  type BrokerField,
  type BrokerInstructions,
  type SavedBroker,
  type SaveBrokerPayload,
  type SupportedBroker,
  activateBroker,
  deleteBroker,
  getBrokerInstructions,
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

    // Validate required fields
    const missing = instructions.fields
      .filter((f) => f.required && !fieldValues[f.name]?.trim())
      .map((f) => f.label)
    if (missing.length > 0) {
      showToast.error(`Required: ${missing.join(', ')}`)
      return
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
              <div className="rounded-md border bg-muted/50 p-4 space-y-2">
                <h4 className="text-sm font-medium">Setup instructions</h4>
                <pre className="whitespace-pre-wrap text-xs leading-relaxed font-sans text-muted-foreground">
                  {instructions.instructions_md}
                </pre>
                {instructions.redirect_url && (
                  <div className="text-xs">
                    <span className="text-muted-foreground">Redirect URL: </span>
                    <code className="bg-background px-1 py-0.5 rounded">{instructions.redirect_url}</code>
                  </div>
                )}
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

export default function BrokerManager() {
  const [saved, setSaved] = useState<SavedBroker[]>([])
  const [supported, setSupported] = useState<SupportedBroker[]>([])
  const [loading, setLoading] = useState(true)
  const [sheetState, setSheetState] = useState<SheetState>({ open: false, editingBroker: null })
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [busyBroker, setBusyBroker] = useState<string | null>(null)

  async function refresh() {
    setLoading(true)
    try {
      const [s, sup] = await Promise.all([listSavedBrokers(), listSupportedBrokers()])
      setSaved(s)
      setSupported(sup)
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
                  {b.last_error && <div className="text-destructive">Error: {b.last_error}</div>}
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
