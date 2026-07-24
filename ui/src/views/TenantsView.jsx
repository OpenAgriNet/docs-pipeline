import React, { useEffect, useState } from 'react'
import {
  AlertTriangle,
  Building2,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Database,
  Plus,
  RefreshCcw,
  ShieldAlert,
  Star,
  Trash2,
  UserPlus,
  Users,
} from 'lucide-react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Skeleton } from '../components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { fetchJson, formatCompactDateTime } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'

// Valid tenant roles per the multi-tenancy API contract.
const TENANT_ROLES = [
  { value: 'state_admin', label: 'State admin' },
  { value: 'content_curator', label: 'Content curator' },
  { value: 'viewer', label: 'Viewer' },
]

// Tenants are keyed by `id` in the API contract; tolerate `instance` as a fallback.
function tenantKey(tenant) {
  return tenant?.id ?? tenant?.instance ?? ''
}

function statusVariant(status) {
  const value = String(status || '').toLowerCase()
  if (value === 'active' || value === 'ready' || value === 'enabled') return 'success'
  if (value === 'pending' || value === 'provisioning') return 'warning'
  if (value === 'disabled' || value === 'suspended' || value === 'failed') return 'destructive'
  return 'secondary'
}

function Notice({ tone = 'warning', children }) {
  const classes =
    tone === 'success'
      ? 'border-success/30 bg-success/10 text-success'
      : tone === 'error'
        ? 'border-destructive/30 bg-destructive/10 text-destructive'
        : 'border-warning/30 bg-warning/10 text-warning-foreground'
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${classes}`}>
      <div className="flex items-start gap-2">
        {tone === 'success' ? (
          <CheckCircle className="mt-0.5 h-4 w-4 shrink-0" />
        ) : (
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        )}
        <span>{children}</span>
      </div>
    </div>
  )
}

// ---- Members tab ---------------------------------------------------------

function MembersPanel({ instance }) {
  const [members, setMembers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [emailOrUserId, setEmailOrUserId] = useState('')
  const [role, setRole] = useState('viewer')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  useEffect(() => {
    loadMembers()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instance])

  async function loadMembers() {
    setLoading(true)
    setError('')
    try {
      const rows = await fetchJson(`/tenants/${encodeURIComponent(instance)}/members`)
      setMembers(Array.isArray(rows) ? rows : [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleAdd(event) {
    event.preventDefault()
    if (!emailOrUserId.trim()) return
    setSubmitting(true)
    setSubmitError('')
    try {
      await fetchJson(`/tenants/${encodeURIComponent(instance)}/members`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email_or_user_id: emailOrUserId.trim(), role }),
      })
      setEmailOrUserId('')
      setRole('viewer')
      await loadMembers()
    } catch (addError) {
      setSubmitError(addError.message)
    } finally {
      setSubmitting(false)
    }
  }

  async function handleRemove(member) {
    const id = member.user_id || member.username || member.email
    if (!id) return
    setError('')
    try {
      await fetchJson(`/tenants/${encodeURIComponent(instance)}/members/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      })
      await loadMembers()
    } catch (removeError) {
      setError(removeError.message)
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <UserPlus className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Add member</h3>
        </div>
        <form className="space-y-3" onSubmit={handleAdd}>
          <div>
            <Label className="text-xs text-muted-foreground">Email or user ID</Label>
            <Input
              className="mt-1 h-9"
              placeholder="person@example.org"
              value={emailOrUserId}
              onChange={(event) => setEmailOrUserId(event.target.value)}
            />
          </div>
          <div>
            <Label className="text-xs text-muted-foreground">Role</Label>
            <Select value={role} onValueChange={setRole}>
              <SelectTrigger className="mt-1 h-9">
                <SelectValue placeholder="Select role" />
              </SelectTrigger>
              <SelectContent>
                {TENANT_ROLES.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" size="sm" disabled={submitting || !emailOrUserId.trim()}>
            <UserPlus className="h-3.5 w-3.5" />
            {submitting ? 'Adding…' : 'Add member'}
          </Button>
          {submitError ? <Notice tone="error">{submitError}</Notice> : null}
        </form>
      </div>

      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Users className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Members</h3>
        </div>
        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : error ? (
          <Notice tone="error">{error}</Notice>
        ) : members.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
            No members yet.
          </div>
        ) : (
          <div className="panel overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead className="text-right">Remove</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {members.map((member) => (
                  <TableRow key={member.user_id || member.username || member.email}>
                    <TableCell className="font-mono text-xs font-medium text-foreground">
                      {member.username || member.user_id || '—'}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">{member.email || '—'}</TableCell>
                    <TableCell>
                      {member.role ? (
                        <Badge variant="secondary" className="font-mono text-[10px]">
                          {member.role}
                        </Badge>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 text-destructive hover:text-destructive"
                        onClick={() => handleRemove(member)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  )
}

// ---- Indexes tab ---------------------------------------------------------

function IndexesPanel({ instance }) {
  const [indexes, setIndexes] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [name, setName] = useState('')
  const [marqoIndex, setMarqoIndex] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  useEffect(() => {
    loadIndexes()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instance])

  async function loadIndexes() {
    setLoading(true)
    setError('')
    try {
      const rows = await fetchJson(`/tenants/${encodeURIComponent(instance)}/indexes`)
      setIndexes(Array.isArray(rows) ? rows : [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleAdd(event) {
    event.preventDefault()
    if (!name.trim()) return
    setSubmitting(true)
    setSubmitError('')
    try {
      const body = { name: name.trim() }
      if (marqoIndex.trim()) body.marqo_index = marqoIndex.trim()
      await fetchJson(`/tenants/${encodeURIComponent(instance)}/indexes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      setName('')
      setMarqoIndex('')
      await loadIndexes()
    } catch (addError) {
      setSubmitError(addError.message)
    } finally {
      setSubmitting(false)
    }
  }

  async function handleRemove(row) {
    const key = row.name
    if (!key) return
    setError('')
    try {
      await fetchJson(`/tenants/${encodeURIComponent(instance)}/indexes/${encodeURIComponent(key)}`, {
        method: 'DELETE',
      })
      await loadIndexes()
    } catch (removeError) {
      setError(removeError.message)
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Plus className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Add index</h3>
        </div>
        <form className="space-y-3" onSubmit={handleAdd}>
          <div>
            <Label className="text-xs text-muted-foreground">Index name</Label>
            <Input
              className="mt-1 h-9 font-mono"
              placeholder="documents-index"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div>
            <Label className="text-xs text-muted-foreground">Marqo index (optional)</Label>
            <Input
              className="mt-1 h-9 font-mono"
              placeholder="Defaults to a derived name"
              value={marqoIndex}
              onChange={(event) => setMarqoIndex(event.target.value)}
            />
          </div>
          <Button type="submit" size="sm" disabled={submitting || !name.trim()}>
            <Plus className="h-3.5 w-3.5" />
            {submitting ? 'Adding…' : 'Add index'}
          </Button>
          {submitError ? <Notice tone="error">{submitError}</Notice> : null}
        </form>
      </div>

      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Database className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Indexes</h3>
        </div>
        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : error ? (
          <Notice tone="error">{error}</Notice>
        ) : indexes.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
            No indexes yet.
          </div>
        ) : (
          <div className="panel overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Marqo index</TableHead>
                  <TableHead className="text-right">Remove</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {indexes.map((row) => (
                  <TableRow key={row.name}>
                    <TableCell className="font-mono text-xs font-medium text-foreground">
                      <span className="inline-flex items-center gap-1.5">
                        {row.is_default ? <Star className="h-3 w-3 text-warning" /> : null}
                        {row.name}
                      </span>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {row.marqo_index || '—'}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 text-destructive hover:text-destructive"
                        disabled={Boolean(row.is_default)}
                        onClick={() => handleRemove(row)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  )
}

// ---- View ----------------------------------------------------------------

export default function TenantsView() {
  const { isPlatformAdmin, username } = useAuth()

  const [tenants, setTenants] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(null)

  const [id, setId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const [createSuccess, setCreateSuccess] = useState('')

  const [reconciling, setReconciling] = useState(false)
  const [reconcileMessage, setReconcileMessage] = useState('')

  useEffect(() => {
    if (isPlatformAdmin) load()
    else setLoading(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlatformAdmin])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const rows = await fetchJson('/tenants')
      setTenants(Array.isArray(rows) ? rows : [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(event) {
    event.preventDefault()
    if (!id.trim() || !displayName.trim()) return
    setCreating(true)
    setCreateError('')
    setCreateSuccess('')
    try {
      await fetchJson('/tenants', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id.trim(), display_name: displayName.trim() }),
      })
      setCreateSuccess(`Tenant "${displayName.trim()}" created.`)
      setId('')
      setDisplayName('')
      await load()
    } catch (submitError) {
      setCreateError(submitError.message)
    } finally {
      setCreating(false)
    }
  }

  async function handleReconcile() {
    setReconciling(true)
    setReconcileMessage('')
    setError('')
    try {
      await fetchJson('/tenants/reconcile', { method: 'POST' })
      setReconcileMessage('Reconcile complete.')
      await load()
    } catch (reconcileError) {
      setError(reconcileError.message)
    } finally {
      setReconciling(false)
    }
  }

  // Hard guard: a non-platform-admin who reaches the route sees an authz notice and
  // no tenant data is ever fetched.
  if (!isPlatformAdmin) {
    return (
      <div className="mx-auto max-w-7xl space-y-4 p-6">
        <div>
          <h1 className="font-serif text-2xl font-semibold text-foreground">Tenants</h1>
          <p className="mt-1 text-sm text-muted-foreground">Tenant administration</p>
        </div>
        <div className="panel p-16 text-center">
          <ShieldAlert className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
          <p className="text-sm font-medium text-foreground">Not authorized</p>
          <p className="mt-1 text-xs text-muted-foreground">
            This area is restricted to platform administrators.
            {username ? ` Signed in as ${username}.` : ''}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="font-serif text-2xl font-semibold text-foreground">Tenants</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Create tenants, manage indexes and members, and reconcile provisioning.
          </p>
        </div>
        <Button size="sm" variant="outline" disabled={reconciling} onClick={handleReconcile}>
          <RefreshCcw className={`h-3.5 w-3.5 ${reconciling ? 'animate-spin' : ''}`} />
          {reconciling ? 'Reconciling…' : 'Reconcile'}
        </Button>
      </div>

      {reconcileMessage ? <Notice tone="success">{reconcileMessage}</Notice> : null}

      {/* Create tenant */}
      <div className="panel">
        <div className="panel-header">
          <div className="flex items-center gap-2">
            <Plus className="h-4 w-4 text-muted-foreground" />
            <h2 className="text-sm font-medium text-foreground">Create tenant</h2>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">Register a new isolated tenant instance.</p>
        </div>
        <form className="space-y-4 p-4" onSubmit={handleCreate}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <Label className="text-xs text-muted-foreground">Tenant ID</Label>
              <Input
                className="mt-1 h-9 font-mono"
                placeholder="acme-org"
                value={id}
                onChange={(event) => setId(event.target.value)}
              />
              <p className="mt-1 text-[10px] text-muted-foreground">
                Lowercase identifier, unique across the platform.
              </p>
            </div>
            <div>
              <Label className="text-xs text-muted-foreground">Display name</Label>
              <Input
                className="mt-1 h-9"
                placeholder="Acme Organization"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
              />
              <p className="mt-1 text-[10px] text-muted-foreground">
                Human-readable name shown across the console.
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={creating || !id.trim() || !displayName.trim()}>
              <Plus className="h-3.5 w-3.5" />
              {creating ? 'Creating…' : 'Create tenant'}
            </Button>
          </div>
          {createSuccess ? <Notice tone="success">{createSuccess}</Notice> : null}
          {createError ? <Notice tone="error">{createError}</Notice> : null}
        </form>
      </div>

      {/* Tenant list */}
      {error ? <Notice tone="error">{error}</Notice> : null}

      {loading ? (
        <div className="space-y-2">
          <Skeleton className="h-12 w-full rounded-lg" />
          <Skeleton className="h-12 w-full rounded-lg" />
          <Skeleton className="h-12 w-full rounded-lg" />
        </div>
      ) : tenants.length === 0 && !error ? (
        <div className="panel p-16 text-center">
          <Building2 className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
          <p className="text-sm font-medium text-foreground">No tenants yet</p>
          <p className="mt-1 text-xs text-muted-foreground">Create your first tenant using the form above.</p>
        </div>
      ) : (
        <div className="panel overflow-hidden">
          <div className="panel-header">
            <h2 className="text-sm font-medium text-foreground">All tenants</h2>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Tenant</TableHead>
                <TableHead>Display name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
                <TableHead className="text-right">Manage</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tenants.map((tenant) => {
                const key = tenantKey(tenant)
                const isOpen = expanded === key
                return (
                  <React.Fragment key={key}>
                    <TableRow>
                      <TableCell className="font-mono text-xs font-medium text-foreground">{key}</TableCell>
                      <TableCell className="text-sm text-foreground">{tenant.display_name || '—'}</TableCell>
                      <TableCell>
                        <Badge variant={statusVariant(tenant.status)}>{tenant.status || 'unknown'}</Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {formatCompactDateTime(tenant.created_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs"
                          onClick={() => setExpanded(isOpen ? null : key)}
                        >
                          {isOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                          Manage
                        </Button>
                      </TableCell>
                    </TableRow>
                    {isOpen ? (
                      <TableRow className="hover:bg-transparent">
                        <TableCell colSpan={5} className="bg-muted/30 p-4">
                          <Tabs defaultValue="members">
                            <TabsList>
                              <TabsTrigger value="members">Members</TabsTrigger>
                              <TabsTrigger value="indexes">Indexes</TabsTrigger>
                            </TabsList>
                            <TabsContent value="members" className="pt-4">
                              <MembersPanel instance={key} />
                            </TabsContent>
                            <TabsContent value="indexes" className="pt-4">
                              <IndexesPanel instance={key} />
                            </TabsContent>
                          </Tabs>
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </React.Fragment>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
