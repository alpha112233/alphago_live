/**
 * AuditLog page — compliance trail of every significant action on
 * this customer's container. Reads /api/instance/audit and renders a
 * filterable table + CSV export button.
 */

import { Download, FileBarChart, Filter, Loader2, RefreshCw } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { type AuditRow, getAuditExportUrl, getAuditLog } from '@/api/audit'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

const ACTOR_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  customer: 'default',
  admin: 'destructive',
  system: 'secondary',
  broker: 'outline',
}

function statusBadge(status: string | null) {
  if (!status) return <span className="text-muted-foreground">—</span>
  const variant: any =
    status === 'ok'
      ? 'default'
      : status === 'rejected' || status === 'failed'
        ? 'destructive'
        : 'secondary'
  return <Badge variant={variant}>{status}</Badge>
}

function compactJSON(v: any): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v.length > 80 ? v.slice(0, 77) + '…' : v
  try {
    const s = JSON.stringify(v)
    return s.length > 80 ? s.slice(0, 77) + '…' : s
  } catch {
    return String(v)
  }
}

function fmtTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch {
    return iso
  }
}

export default function AuditLog() {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actor, setActor] = useState<string>('all')
  const [actionPrefix, setActionPrefix] = useState<string>('all')

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const q: any = { limit: 500 }
      if (actor !== 'all') q.actor = actor
      if (actionPrefix !== 'all') q.action_prefix = actionPrefix
      const r = await getAuditLog(q)
      setRows(r)
    } catch (e: any) {
      setError(e?.message || 'failed to load audit log')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actor, actionPrefix])

  const exportUrl = useMemo(() => {
    const q: any = { limit: 5000 }
    if (actor !== 'all') q.actor = actor
    if (actionPrefix !== 'all') q.action_prefix = actionPrefix
    return getAuditExportUrl(q)
  }, [actor, actionPrefix])

  return (
    <div className="container max-w-7xl py-6 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <FileBarChart className="h-6 w-6" />
            Audit Log
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Every action that touched credentials, orders, sessions, or instance state. Stored in your
            container only.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
          <a href={exportUrl} target="_blank" rel="noopener noreferrer">
            <Button variant="default" size="sm">
              <Download className="h-3.5 w-3.5 mr-1.5" />
              Export CSV
            </Button>
          </a>
        </div>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <Filter className="h-4 w-4" />
            Filters
          </CardTitle>
          <CardDescription>Narrow the view. Filters apply to both the table and the CSV export.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3">
          <div className="space-y-1.5">
            <label className="text-xs text-muted-foreground">Actor</label>
            <Select value={actor} onValueChange={setActor}>
              <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">all</SelectItem>
                <SelectItem value="customer">customer</SelectItem>
                <SelectItem value="admin">admin</SelectItem>
                <SelectItem value="system">system</SelectItem>
                <SelectItem value="broker">broker</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <label className="text-xs text-muted-foreground">Action</label>
            <Select value={actionPrefix} onValueChange={setActionPrefix}>
              <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">all</SelectItem>
                <SelectItem value="order.">orders</SelectItem>
                <SelectItem value="broker.">broker creds</SelectItem>
                <SelectItem value="session.">sessions</SelectItem>
                <SelectItem value="instance.">instance ops</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-6">
          {error && <p className="text-sm text-destructive mb-3">{error}</p>}
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin" />
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-44">Time</TableHead>
                    <TableHead className="w-28">Actor</TableHead>
                    <TableHead className="w-44">Action</TableHead>
                    <TableHead className="w-32">Resource</TableHead>
                    <TableHead className="w-24">Status</TableHead>
                    <TableHead>Details</TableHead>
                    <TableHead className="w-28">From IP</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                        No audit entries match the filters.
                      </TableCell>
                    </TableRow>
                  )}
                  {rows.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="font-mono text-xs">{fmtTs(r.ts)}</TableCell>
                      <TableCell>
                        <Badge variant={ACTOR_VARIANT[r.actor] || 'outline'}>{r.actor}</Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{r.action}</TableCell>
                      <TableCell className="font-mono text-xs">{r.resource || '—'}</TableCell>
                      <TableCell>{statusBadge(r.status)}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {r.note && <div>{r.note.slice(0, 120)}</div>}
                        {r.after && (
                          <div className="font-mono text-[10px] mt-0.5 text-foreground/70">
                            after: {compactJSON(r.after)}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs">{r.src_ip || '—'}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
