import React, { useEffect, useMemo, useState } from 'react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import { fetchJson } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'
import { AlertTriangle, Check, CheckCircle, Plus, RefreshCcw, Tags, Trash2, X } from 'lucide-react'

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

// One dimension's value chips + inline add / rename / delete. Every mutation is
// re-enforced server-side (admin in the tenant), so the `canAdmin` gate is UX only.
function DimensionRow({ instance, domain, dimension, values, disabled, onChanged, onError, onNotice }) {
  const [adding, setAdding] = useState(false)
  const [addValue, setAddValue] = useState('')
  const [editing, setEditing] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [busy, setBusy] = useState(false)

  async function mutate(fn) {
    setBusy(true)
    onError('')
    try {
      await fn()
      await onChanged()
      return true
    } catch (err) {
      onError(err.message)
      return false
    } finally {
      setBusy(false)
    }
  }

  async function handleAdd() {
    const value = addValue.trim()
    if (!value) return
    const ok = await mutate(() => fetchJson(`/tenants/${encodeURIComponent(instance)}/taxonomy/nodes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain, dimension, value }),
    }))
    if (ok) { setAddValue(''); setAdding(false) }
  }

  async function handleRename(oldValue) {
    const newValue = editValue.trim()
    if (!newValue || newValue === oldValue) { setEditing(null); return }
    const ok = await mutate(() => fetchJson(`/tenants/${encodeURIComponent(instance)}/taxonomy/nodes`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain, dimension, value: oldValue, new_value: newValue }),
    }))
    if (ok) setEditing(null)
  }

  async function handleDelete(value) {
    const qs = new URLSearchParams({ domain, dimension, value }).toString()
    await mutate(() => fetchJson(`/tenants/${encodeURIComponent(instance)}/taxonomy/nodes?${qs}`, {
      method: 'DELETE',
    }))
  }

  // Deleting the last value keeps the (empty) dimension, so retiring a dimension
  // needs its own call.
  async function handleDeleteDimension() {
    const qs = new URLSearchParams({ domain, dimension }).toString()
    const ok = await mutate(() => fetchJson(`/tenants/${encodeURIComponent(instance)}/taxonomy/dimensions?${qs}`, {
      method: 'DELETE',
    }))
    if (ok && onNotice) onNotice(`Removed dimension ${domain} · ${dimension}.`)
  }

  return (
    <div className="flex flex-wrap items-center gap-2 py-2 border-b border-border/60 last:border-b-0">
      <span className="font-mono text-xs text-muted-foreground w-40 shrink-0">{dimension}</span>
      <div className="flex flex-wrap items-center gap-1.5 flex-1">
        {values.length === 0 ? (
          <span className="text-xs italic text-muted-foreground/70">empty — no vocabulary yet</span>
        ) : null}
        {values.map(value => (
          editing === value ? (
            <span key={value} className="inline-flex items-center gap-1">
              <Input
                className="h-7 w-32 font-mono text-xs"
                value={editValue}
                autoFocus
                onChange={e => setEditValue(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleRename(value); if (e.key === 'Escape') setEditing(null) }}
              />
              <Button size="sm" variant="ghost" className="h-7 w-7 p-0" disabled={busy} onClick={() => handleRename(value)}>
                <Check className="h-3.5 w-3.5 text-success" />
              </Button>
              <Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={() => setEditing(null)}>
                <X className="h-3.5 w-3.5 text-muted-foreground" />
              </Button>
            </span>
          ) : (
            <Badge key={value} variant="secondary" className="font-mono text-[11px] gap-1 pr-1">
              <button
                type="button"
                className="hover:underline disabled:no-underline"
                disabled={disabled}
                title={disabled ? undefined : 'Rename'}
                onClick={() => { if (!disabled) { setEditing(value); setEditValue(value) } }}
              >
                {value}
              </button>
              {!disabled ? (
                <button
                  type="button"
                  className="text-muted-foreground hover:text-destructive"
                  title="Delete"
                  disabled={busy}
                  onClick={() => handleDelete(value)}
                >
                  <X className="h-3 w-3" />
                </button>
              ) : null}
            </Badge>
          )
        ))}
        {!disabled ? (
          adding ? (
            <span className="inline-flex items-center gap-1">
              <Input
                className="h-7 w-32 font-mono text-xs"
                placeholder="value"
                value={addValue}
                autoFocus
                onChange={e => setAddValue(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleAdd(); if (e.key === 'Escape') { setAdding(false); setAddValue('') } }}
              />
              <Button size="sm" variant="ghost" className="h-7 w-7 p-0" disabled={busy || !addValue.trim()} onClick={handleAdd}>
                <Check className="h-3.5 w-3.5 text-success" />
              </Button>
              <Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={() => { setAdding(false); setAddValue('') }}>
                <X className="h-3.5 w-3.5 text-muted-foreground" />
              </Button>
            </span>
          ) : (
            <Button size="sm" variant="ghost" className="h-7 text-xs text-muted-foreground" onClick={() => setAdding(true)}>
              <Plus className="h-3 w-3 mr-0.5" />value
            </Button>
          )
        ) : null}
      </div>
      {!disabled ? (
        <Button
          size="sm"
          variant="ghost"
          className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
          title="Remove dimension"
          disabled={busy}
          onClick={handleDeleteDimension}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      ) : null}
    </div>
  )
}

// Per-tenant tag-taxonomy console, backed by /tenants/{instance}/taxonomy[/nodes].
function TaxonomyPanel({ instance }) {
  const [taxonomy, setTaxonomy] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // Editing is admin-IN-THIS-TENANT, not "admin anywhere": a caller who is admin
  // in tenant A but viewer in tenant B must not see editable controls for B. The
  // GET is gated by exactly that per-tenant check server-side (403 otherwise),
  // so a successful load is the scoped answer — no any-tenant permission bit.
  const [canAdmin, setCanAdmin] = useState(false)

  const [newDomain, setNewDomain] = useState('')
  const [newDimension, setNewDimension] = useState('')
  const [newValue, setNewValue] = useState('')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instance])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const data = await fetchJson(`/tenants/${encodeURIComponent(instance)}/taxonomy`)
      setTaxonomy(data && typeof data === 'object' ? data : { domains: {} })
      setCanAdmin(true)
    } catch (loadError) {
      setError(loadError.message)
      setTaxonomy({ domains: {} })
      setCanAdmin(false)
    } finally {
      setLoading(false)
    }
  }

  async function reload() {
    const data = await fetchJson(`/tenants/${encodeURIComponent(instance)}/taxonomy`)
    setTaxonomy(data && typeof data === 'object' ? data : { domains: {} })
  }

  async function handleCreateNode(event) {
    event.preventDefault()
    const domain = newDomain.trim()
    const dimension = newDimension.trim()
    if (!domain || !dimension) return
    setCreating(true)
    setError('')
    setNotice('')
    try {
      const body = { domain, dimension }
      if (newValue.trim()) body.value = newValue.trim()
      await fetchJson(`/tenants/${encodeURIComponent(instance)}/taxonomy/nodes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      setNotice(`Added ${domain} · ${dimension}${newValue.trim() ? ` : ${newValue.trim()}` : ' (empty dimension)'}.`)
      setNewValue('')
      await reload()
    } catch (createError) {
      setError(createError.message)
    } finally {
      setCreating(false)
    }
  }

  const domains = taxonomy?.domains || {}
  const domainNames = useMemo(() => Object.keys(domains).sort(), [domains])

  return (
    <div className="panel">
      <div className="panel-header flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Tags className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-sm font-medium text-foreground">Tag taxonomy</h2>
          <Badge variant="secondary" className="font-mono text-[10px]">{instance}</Badge>
        </div>
        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={load} disabled={loading}>
          <RefreshCcw className="h-3 w-3 mr-1" />Refresh
        </Button>
      </div>

      <div className="p-4 space-y-4">
        {notice ? <Notice tone="success">{notice}</Notice> : null}
        {error ? <Notice tone="error">{error}</Notice> : null}

        {canAdmin ? (
          <form className="flex flex-wrap items-end gap-3" onSubmit={handleCreateNode}>
            <div>
              <label className="text-xs font-medium text-muted-foreground">Domain</label>
              <Input
                className="mt-1 h-9 font-mono w-44"
                placeholder="animal_husbandry"
                value={newDomain}
                onChange={e => setNewDomain(e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs font-medium text-muted-foreground">Dimension</label>
              <Input
                className="mt-1 h-9 font-mono w-40"
                placeholder="species"
                value={newDimension}
                onChange={e => setNewDimension(e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs font-medium text-muted-foreground">Value (optional)</label>
              <Input
                className="mt-1 h-9 font-mono w-40"
                placeholder="cattle"
                value={newValue}
                onChange={e => setNewValue(e.target.value)}
              />
            </div>
            <Button type="submit" size="sm" className="h-9" disabled={creating || !newDomain.trim() || !newDimension.trim()}>
              <Plus className="h-3.5 w-3.5" />
              {creating ? 'Adding…' : 'Add node'}
            </Button>
          </form>
        ) : null}

        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : domainNames.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-3 py-8 text-center text-sm text-muted-foreground">
            No taxonomy defined for this tenant yet.
          </div>
        ) : (
          <div className="space-y-4">
            {domainNames.map(domain => {
              const dims = domains[domain] || {}
              const dimNames = Object.keys(dims).sort()
              return (
                <div key={domain} className="panel overflow-hidden">
                  <div className="panel-header">
                    <h3 className="text-sm font-semibold font-mono text-foreground">{domain}</h3>
                  </div>
                  <div className="px-4 py-1">
                    {dimNames.length === 0 ? (
                      <p className="py-3 text-xs italic text-muted-foreground/70">No dimensions.</p>
                    ) : dimNames.map(dimension => (
                      <DimensionRow
                        key={dimension}
                        instance={instance}
                        domain={domain}
                        dimension={dimension}
                        values={Array.isArray(dims[dimension]) ? dims[dimension] : []}
                        disabled={!canAdmin}
                        onChanged={reload}
                        onError={setError}
                        onNotice={setNotice}
                      />
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export default function TaxonomyView() {
  const { instances, isPlatformAdmin } = useAuth()
  const [selectedInstance, setSelectedInstance] = useState('')
  // A pure platform admin (master_admin) is a CONTROL-PLANE admin: its
  // `instances` claim is only the tenants it is a *member* of, which is empty —
  // yet the backend lets it manage every tenant's taxonomy. Source its tenant
  // list from the registry instead, or the console is a dead end for exactly the
  // persona it is built for.
  const [registryTenants, setRegistryTenants] = useState([])
  const [registryError, setRegistryError] = useState('')

  useEffect(() => {
    if (!isPlatformAdmin) return
    let cancelled = false
    fetchJson('/tenants')
      .then(rows => {
        if (cancelled) return
        setRegistryTenants((Array.isArray(rows) ? rows : []).map(t => t.id).filter(Boolean))
      })
      .catch(err => { if (!cancelled) setRegistryError(err.message) })
    return () => { cancelled = true }
  }, [isPlatformAdmin])

  // Union so a platform admin that *also* holds tenant memberships sees both.
  const tenantOptions = useMemo(
    () => Array.from(new Set([...instances, ...registryTenants])).sort(),
    [instances, registryTenants],
  )

  useEffect(() => {
    if (!selectedInstance && tenantOptions.length > 0) setSelectedInstance(tenantOptions[0])
  }, [tenantOptions, selectedInstance])

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Taxonomy</h1>
          <p className="text-sm text-muted-foreground mt-1">Per-tenant domain tag vocabulary used for chunk tagging and search</p>
        </div>
        {/* Rendered from ONE option up: a single-tenant caller must still see
            which tenant it is editing, and the picker is the only affordance. */}
        {tenantOptions.length >= 1 ? (
          <Select value={selectedInstance} onValueChange={setSelectedInstance}>
            <SelectTrigger className="h-9 w-56">
              <SelectValue placeholder="Select tenant" />
            </SelectTrigger>
            <SelectContent>
              {tenantOptions.map(inst => (
                <SelectItem key={inst} value={inst} className="font-mono text-xs">{inst}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
      </div>

      {registryError ? <Notice tone="error">{registryError}</Notice> : null}

      {selectedInstance ? (
        <TaxonomyPanel key={selectedInstance} instance={selectedInstance} />
      ) : (
        <div className="rounded-md border border-dashed border-border px-3 py-10 text-center text-sm text-muted-foreground">
          {isPlatformAdmin
            ? 'No tenants are registered yet. Create one in the Tenants console.'
            : 'No tenant is associated with your account.'}
        </div>
      )}
    </div>
  )
}
