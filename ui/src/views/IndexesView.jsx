import React, { useEffect, useState } from 'react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent } from '../components/ui/card'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../components/ui/alert-dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { fetchAllDocuments, fetchJson, formatCompactDateTime, formatCount } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'
import { Activity, AlertTriangle, Check, CheckCircle, ChevronDown, ChevronUp, Clock, Database, HardDrive, Layers, Pencil, Plus, RefreshCcw, Star, Trash2, X } from 'lucide-react'

function formatMetric(value, suffix = '') {
  if (value === null || value === undefined || value === '') return '—'
  return `${value}${suffix}`
}

function Notice({ tone = 'warning', children }) {
  const classes = tone === 'success'
    ? 'border-success/30 bg-success/10 text-success'
    : tone === 'error'
      ? 'border-destructive/30 bg-destructive/10 text-destructive'
      : 'border-warning/30 bg-warning/10 text-warning-foreground'
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${classes}`}>
      <div className="flex items-start gap-2">
        {tone === 'success' ? <CheckCircle className="mt-0.5 h-4 w-4 shrink-0" /> : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />}
        <span>{children}</span>
      </div>
    </div>
  )
}

// Self-service management of a tenant's LOGICAL index registry (create / rename /
// set-default / delete). Backed by the /tenants/{instance}/indexes routes; every
// mutation is re-enforced server-side (admin/pipeline in the tenant), so the
// role gates here are UX only.
function TenantIndexPanel({ instance, canPipeline, canAdmin }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')

  const [newName, setNewName] = useState('')
  const [newModel, setNewModel] = useState('')
  const [creating, setCreating] = useState(false)

  const [editing, setEditing] = useState(null)
  const [editValue, setEditValue] = useState('')

  const [deleteTarget, setDeleteTarget] = useState(null)
  const [forceDelete, setForceDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instance])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const data = await fetchJson(`/tenants/${encodeURIComponent(instance)}/indexes`)
      setRows(Array.isArray(data) ? data : [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(event) {
    event.preventDefault()
    if (!newName.trim()) return
    setCreating(true)
    setError('')
    setNotice('')
    try {
      const body = { name: newName.trim() }
      if (newModel.trim()) body.embedding_model = newModel.trim()
      const created = await fetchJson(`/tenants/${encodeURIComponent(instance)}/indexes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      setNotice(`Index "${created.name}" created${created.is_default ? ' (tenant default)' : ''}.`)
      setNewName('')
      setNewModel('')
      await load()
    } catch (createError) {
      setError(createError.message)
    } finally {
      setCreating(false)
    }
  }

  async function patchIndex(name, body, label) {
    setBusy(name)
    setError('')
    setNotice('')
    try {
      await fetchJson(`/tenants/${encodeURIComponent(instance)}/indexes/${encodeURIComponent(name)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (label) setNotice(label)
      await load()
      return true
    } catch (patchError) {
      setError(patchError.message)
      return false
    } finally {
      setBusy('')
    }
  }

  async function handleRename(name) {
    const ok = await patchIndex(name, { display_name: editValue.trim() }, `Renamed "${name}".`)
    if (ok) setEditing(null)
  }

  async function handleSetDefault(name) {
    await patchIndex(name, { is_default: true }, `"${name}" is now the tenant default.`)
  }

  async function handleDelete() {
    if (!deleteTarget) return
    setDeleting(true)
    setError('')
    setNotice('')
    try {
      const qs = forceDelete ? '?force=true' : ''
      const result = await fetchJson(
        `/tenants/${encodeURIComponent(instance)}/indexes/${encodeURIComponent(deleteTarget.name)}${qs}`,
        { method: 'DELETE' },
      )
      const reassigned = result?.documents_reassigned || 0
      setNotice(
        `Index "${deleteTarget.name}" deleted${reassigned ? ` — ${formatCount(reassigned)} document(s) reassigned to the tenant default` : ''}.`,
      )
      setDeleteTarget(null)
      setForceDelete(false)
      await load()
    } catch (deleteError) {
      // Surface the 409 in-use / default-index guard so the operator can retry
      // with force (the dialog stays open with its force toggle).
      setError(deleteError.message)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="panel">
      <div className="panel-header flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Layers className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-sm font-medium text-foreground">Tenant indexes</h2>
          <Badge variant="secondary" className="font-mono text-[10px]">{instance}</Badge>
        </div>
        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={load} disabled={loading}>
          <RefreshCcw className="h-3 w-3 mr-1" />Refresh
        </Button>
      </div>

      <div className="p-4 space-y-4">
        {notice ? <Notice tone="success">{notice}</Notice> : null}
        {error ? <Notice tone="error">{error}</Notice> : null}

        {/* Create index */}
        {canPipeline ? (
          <form className="flex flex-wrap items-end gap-3" onSubmit={handleCreate}>
            <div>
              <label className="text-xs font-medium text-muted-foreground">Index name</label>
              <Input
                className="mt-1 h-9 font-mono w-44"
                placeholder="schemes"
                value={newName}
                onChange={event => setNewName(event.target.value)}
              />
              <p className="text-[10px] text-muted-foreground mt-1">Lowercase, letters/digits/_ only.</p>
            </div>
            <div>
              <label className="text-xs font-medium text-muted-foreground">Embedding model (optional)</label>
              <Input
                className="mt-1 h-9 w-56"
                placeholder="default model"
                value={newModel}
                onChange={event => setNewModel(event.target.value)}
              />
            </div>
            <Button type="submit" size="sm" className="h-9" disabled={creating || !newName.trim()}>
              <Plus className="h-3.5 w-3.5" />
              {creating ? 'Creating…' : 'Create index'}
            </Button>
          </form>
        ) : null}

        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : rows.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-3 py-8 text-center text-sm text-muted-foreground">
            No indexes registered for this tenant yet.
          </div>
        ) : (
          <div className="panel overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Display name</TableHead>
                  <TableHead>Physical index</TableHead>
                  <TableHead>Default</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map(idx => {
                  const rowBusy = busy === idx.name
                  const isEditing = editing === idx.name
                  return (
                    <TableRow key={idx.name}>
                      <TableCell className="font-mono text-xs font-medium text-foreground">{idx.name}</TableCell>
                      <TableCell className="text-sm text-foreground">
                        {isEditing ? (
                          <div className="flex items-center gap-1">
                            <Input
                              className="h-8 w-40"
                              value={editValue}
                              autoFocus
                              placeholder="Display name"
                              onChange={event => setEditValue(event.target.value)}
                              onKeyDown={event => { if (event.key === 'Enter') handleRename(idx.name) }}
                            />
                            <Button size="sm" variant="ghost" className="h-8 w-8 p-0" disabled={rowBusy} onClick={() => handleRename(idx.name)}>
                              <Check className="h-3.5 w-3.5 text-success" />
                            </Button>
                            <Button size="sm" variant="ghost" className="h-8 w-8 p-0" onClick={() => setEditing(null)}>
                              <X className="h-3.5 w-3.5 text-muted-foreground" />
                            </Button>
                          </div>
                        ) : (
                          <span>{idx.display_name || <span className="text-muted-foreground">—</span>}</span>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-[11px] text-muted-foreground">{idx.marqo_index}</TableCell>
                      <TableCell>
                        {idx.is_default ? (
                          <Badge variant="success"><Star className="h-3 w-3 mr-1" />Default</Badge>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{formatCompactDateTime(idx.created_at)}</TableCell>
                      <TableCell className="text-right">
                        {canAdmin ? (
                          <div className="flex items-center justify-end gap-1">
                            {!isEditing ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-7 text-xs"
                                disabled={rowBusy}
                                onClick={() => { setEditing(idx.name); setEditValue(idx.display_name || '') }}
                              >
                                <Pencil className="h-3 w-3 mr-1" />Rename
                              </Button>
                            ) : null}
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 text-xs"
                              disabled={rowBusy || idx.is_default}
                              onClick={() => handleSetDefault(idx.name)}
                            >
                              <Star className="h-3 w-3 mr-1" />Set default
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-7 text-xs text-destructive hover:text-destructive"
                              disabled={rowBusy}
                              onClick={() => { setDeleteTarget(idx); setForceDelete(false); setError('') }}
                            >
                              <Trash2 className="h-3 w-3 mr-1" />Delete
                            </Button>
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* Delete confirm — surfaces the in-use / default-index force-reassign guard. */}
      <AlertDialog open={!!deleteTarget} onOpenChange={open => { if (!open) { setDeleteTarget(null); setForceDelete(false) } }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete index “{deleteTarget?.name}”?</AlertDialogTitle>
            <AlertDialogDescription>
              This drops the physical Marqo index <span className="font-mono">{deleteTarget?.marqo_index}</span> and its
              registry row. An index that still has documents — or that is the tenant default while other indexes exist —
              is refused unless you force it below.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <label className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={forceDelete}
              onChange={event => setForceDelete(event.target.checked)}
            />
            <span>
              <span className="font-medium">Force delete.</span> Reassign any documents in this index back to the tenant
              default (their chunks resolve to the default index) and drop it even if it is the current default.
            </span>
          </label>
          {error ? <Notice tone="error">{error}</Notice> : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={event => { event.preventDefault(); handleDelete() }}
            >
              {deleting ? 'Deleting…' : forceDelete ? 'Force delete' : 'Delete'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

export default function IndexesView() {
  const [loading, setLoading] = useState(true)
  const [expandedIndex, setExpandedIndex] = useState(null)
  const [indexRows, setIndexRows] = useState([])
  const [error, setError] = useState('')
  const [actionMessage, setActionMessage] = useState('')
  const [busyIndex, setBusyIndex] = useState('')
  const { hasPermission, instances } = useAuth()
  const canPipeline = hasPermission('pipeline')
  const canAdmin = hasPermission('admin')

  const [selectedInstance, setSelectedInstance] = useState('')

  useEffect(() => {
    load()
  }, [])

  useEffect(() => {
    if (!selectedInstance && instances.length > 0) setSelectedInstance(instances[0])
  }, [instances, selectedInstance])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const rows = await fetchJson('/marqo/indexes/summary')
      setIndexRows(Array.isArray(rows) ? rows : [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleReindex(indexName, mode = 'stale') {
    setBusyIndex(indexName)
    setActionMessage('')
    setError('')
    try {
      const docs = await fetchAllDocuments()
      const workflowIds = docs
        .filter(doc => {
          if (mode === 'stale') return doc.reindex_required
          return doc.reindex_required || ['completed', 'ready_for_ingestion', 'chunk_review'].includes(doc.stage)
        })
        .map(doc => doc.workflow_id)

      if (!workflowIds.length) {
        setActionMessage(`No eligible documents found for ${mode === 'stale' ? 'stale reindex' : 'full reindex'}.`)
        return
      }

      const result = await fetchJson('/documents/bulk/reindex', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workflow_ids: workflowIds })
      })

      setActionMessage(
        `${result.succeeded} workflow${result.succeeded === 1 ? '' : 's'} queued for ${mode === 'stale' ? 'stale' : 'full'} reindex from ${indexName}.`
      )
      await load()
    } catch (actionError) {
      setError(actionError.message)
    } finally {
      setBusyIndex('')
    }
  }

  if (loading) {
    return (
      <div className="p-6 max-w-7xl mx-auto space-y-4">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-4 w-48 mt-1" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[180px] rounded-lg" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Indexes</h1>
          <p className="text-sm text-muted-foreground mt-1">Search index health, status, and per-tenant management</p>
        </div>
        {instances.length > 1 ? (
          <Select value={selectedInstance} onValueChange={setSelectedInstance}>
            <SelectTrigger className="h-9 w-56">
              <SelectValue placeholder="Select tenant" />
            </SelectTrigger>
            <SelectContent>
              {instances.map(inst => (
                <SelectItem key={inst} value={inst} className="font-mono text-xs">{inst}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
      </div>

      {error ? (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
          <AlertTriangle className="h-4 w-4 text-destructive shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {actionMessage ? (
        <Card className="shadow-none">
          <CardContent className="flex items-start gap-3 px-4 py-3 text-sm text-foreground">
            <CheckCircle className="mt-0.5 h-4 w-4 text-success" />
            <span>{actionMessage}</span>
          </CardContent>
        </Card>
      ) : null}

      {/* Per-tenant logical index management (create / rename / set-default / delete). */}
      {selectedInstance ? (
        <TenantIndexPanel
          key={selectedInstance}
          instance={selectedInstance}
          canPipeline={canPipeline}
          canAdmin={canAdmin}
        />
      ) : null}

      {/* Physical index health */}
      {indexRows.length === 0 ? (
        <div className="panel p-16 text-center">
          <Database className="h-10 w-10 mx-auto mb-3 text-muted-foreground/30" />
          <p className="text-sm font-medium text-foreground">No index health data</p>
          <p className="text-xs text-muted-foreground mt-1">Physical index metrics appear here once documents complete ingestion</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {indexRows.map(idx => {
            const health = idx.live_error ? 'degraded' : idx.stale_documents > 0 ? 'warning' : 'healthy'
            return (
              <div key={idx.index_name} className={`panel ${health === 'degraded' ? 'border-destructive/30' : health === 'warning' ? 'border-warning/30' : ''}`}>
                <div className="panel-header flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Database className="h-4 w-4 text-muted-foreground" />
                    <h3 className="text-sm font-semibold font-mono text-foreground">{idx.index_name}</h3>
                  </div>
                  <div className="flex items-center gap-2">
                    {health === 'healthy' ? <CheckCircle className="h-3.5 w-3.5 text-success" /> : <AlertTriangle className={`h-3.5 w-3.5 ${health === 'warning' ? 'text-warning' : 'text-destructive'}`} />}
                    {idx.stale_documents > 0 ? (
                      <Badge variant="warning"><AlertTriangle className="h-3 w-3 mr-1" />Stale</Badge>
                    ) : (
                      <Badge variant="success"><CheckCircle className="h-3 w-3 mr-1" />Synced</Badge>
                    )}
                    {idx.has_domain_tags_field === false ? (
                      <Badge variant="outline">No tag field</Badge>
                    ) : idx.has_domain_tags_field ? (
                      <Badge variant="secondary">Tag filters</Badge>
                    ) : null}
                  </div>
                </div>
                <div className="p-4 space-y-4">
                  <div className="grid grid-cols-3 gap-4">
                    <div>
                      <p className="text-xs text-muted-foreground uppercase tracking-wider">Documents</p>
                      <p className="text-xl font-semibold font-serif mt-1">{formatCount(idx.documents)}</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground uppercase tracking-wider">Chunks</p>
                      <p className="text-xl font-semibold font-serif mt-1">{formatCount(idx.indexed_chunks)}</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground uppercase tracking-wider">Stale</p>
                      <p className={`text-xl font-semibold font-serif mt-1 ${idx.stale_documents > 0 ? 'text-warning' : ''}`}>
                        {formatCount(idx.stale_documents)}
                      </p>
                    </div>
                  </div>

                  {idx.stale_documents > 0 ? (
                    <Card className="border-warning/30 bg-warning/10 shadow-none">
                      <CardContent className="flex items-center gap-3 p-3">
                        <RefreshCcw className="h-4 w-4 shrink-0 text-warning" />
                        <span className="text-sm text-foreground">{formatCount(idx.stale_documents)} document(s) need reindexing</span>
                        <Button
                          size="sm"
                          variant="warning"
                          className="ml-auto"
                          disabled={busyIndex === idx.index_name || !canPipeline}
                          onClick={() => handleReindex(idx.index_name, 'stale')}
                        >
                          {busyIndex === idx.index_name ? 'Queueing...' : 'Reindex Stale'}
                        </Button>
                      </CardContent>
                    </Card>
                  ) : null}

                  <button
                    className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors w-full"
                    onClick={() => setExpandedIndex(expandedIndex === idx.index_name ? null : idx.index_name)}
                  >
                    {expandedIndex === idx.index_name ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                    Index stats
                  </button>

                  {expandedIndex === idx.index_name ? (
                    <div className="grid grid-cols-3 gap-3 pt-2 border-t border-border">
                      <div className="flex items-center gap-2">
                        <Activity className="h-3.5 w-3.5 text-muted-foreground" />
                        <div>
                          <p className="text-[10px] text-muted-foreground">Avg Query</p>
                          <p className="text-xs font-medium">{formatMetric(idx.avg_query_ms ?? idx.live_stats?.avgQueryMs, 'ms')}</p>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <HardDrive className="h-3.5 w-3.5 text-muted-foreground" />
                        <div>
                          <p className="text-[10px] text-muted-foreground">Storage</p>
                          <p className="text-xs font-medium">{formatMetric(idx.storage_mb ?? idx.live_stats?.storageMb, ' MB')}</p>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                        <div>
                          <p className="text-[10px] text-muted-foreground">Updated</p>
                          <p className="text-xs font-medium">{formatCompactDateTime(idx.last_indexed_at)}</p>
                        </div>
                      </div>
                    </div>
                  ) : null}

                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      className="text-xs h-7"
                      disabled={busyIndex === idx.index_name || !canPipeline}
                      onClick={() => handleReindex(idx.index_name, 'all')}
                    >
                      <RefreshCcw className="h-3 w-3 mr-1" />
                      {busyIndex === idx.index_name ? 'Queueing...' : 'Full Reindex'}
                    </Button>
                  </div>

                  <div className="text-xs text-muted-foreground">
                    Last updated: {formatCompactDateTime(idx.last_indexed_at)}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
