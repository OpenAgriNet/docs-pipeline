import React, { useEffect, useState } from 'react'
import { AlertTriangle, Building2, Check, CheckCircle, ChevronDown, ChevronUp, Copy, KeyRound, Plus, ShieldAlert, UserPlus, Users } from 'lucide-react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { fetchJson, formatCompactDateTime } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'

const SUPER_ADMIN_ROLE = 'master_admin'

function tenantKey(tenant) {
  return tenant?.instance ?? tenant?.id ?? ''
}

function statusVariant(status) {
  const value = String(status || '').toLowerCase()
  if (value === 'active' || value === 'ready' || value === 'enabled') return 'success'
  if (value === 'pending' || value === 'provisioning') return 'warning'
  if (value === 'disabled' || value === 'suspended' || value === 'failed') return 'destructive'
  return 'secondary'
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

// Read-only field that reveals a generated secret with a one-click copy control.
function CopyableSecret({ label, value }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div>
      <p className="text-xs text-muted-foreground uppercase tracking-wider">{label}</p>
      <div className="mt-1 flex items-center gap-2">
        <code className="flex-1 rounded-md border border-input bg-muted px-3 py-2 font-mono text-sm text-foreground break-all">
          {value}
        </code>
        <Button size="sm" variant="outline" className="shrink-0" onClick={copy}>
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
    </div>
  )
}

function AddAdminPanel({ tenant }) {
  const instance = tenantKey(tenant)
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [credential, setCredential] = useState(null)

  const [members, setMembers] = useState([])
  const [membersLoading, setMembersLoading] = useState(true)
  const [membersError, setMembersError] = useState('')

  useEffect(() => {
    loadMembers()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instance])

  async function loadMembers() {
    setMembersLoading(true)
    setMembersError('')
    try {
      const rows = await fetchJson(`/tenants/${encodeURIComponent(instance)}/members`)
      setMembers(Array.isArray(rows) ? rows : [])
    } catch (loadError) {
      setMembersError(loadError.message)
    } finally {
      setMembersLoading(false)
    }
  }

  async function handleAddAdmin(event) {
    event.preventDefault()
    if (!username.trim()) return
    setSubmitting(true)
    setError('')
    setCredential(null)
    try {
      const body = { username: username.trim() }
      if (email.trim()) body.email = email.trim()
      const result = await fetchJson(`/tenants/${encodeURIComponent(instance)}/admins`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
      setCredential(result)
      setUsername('')
      setEmail('')
      await loadMembers()
    } catch (submitError) {
      setError(submitError.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <UserPlus className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Add tenant admin</h3>
        </div>
        <form className="space-y-3" onSubmit={handleAddAdmin}>
          <div>
            <label className="text-xs font-medium text-muted-foreground">Username</label>
            <Input
              className="mt-1 h-9"
              placeholder="admin-username"
              value={username}
              onChange={event => setUsername(event.target.value)}
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground">Email (optional)</label>
            <Input
              className="mt-1 h-9"
              type="email"
              placeholder="admin@example.org"
              value={email}
              onChange={event => setEmail(event.target.value)}
            />
          </div>
          <Button type="submit" size="sm" disabled={submitting || !username.trim()}>
            <UserPlus className="h-3.5 w-3.5" />
            {submitting ? 'Creating…' : 'Create admin'}
          </Button>
        </form>

        {error ? <Notice tone="error">{error}</Notice> : null}

        {credential ? (
          <div className="space-y-3 rounded-md border border-success/30 bg-success/5 p-3">
            <div className="flex items-center gap-2">
              <KeyRound className="h-4 w-4 text-success" />
              <p className="text-sm font-medium text-foreground">
                Admin <span className="font-mono">{credential.username}</span> created
              </p>
            </div>
            <CopyableSecret label="Temporary password" value={credential.temporary_password} />
            <p className="text-xs text-muted-foreground">
              Copy this now — it is shown only once. The admin must change it on first login.
            </p>
          </div>
        ) : null}
      </div>

      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Users className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Members</h3>
        </div>
        {membersLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : membersError ? (
          <Notice tone="error">{membersError}</Notice>
        ) : members.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
            No members yet.
          </div>
        ) : (
          <div className="panel overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Username</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead>Roles</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {members.map(member => (
                  <TableRow key={member.username}>
                    <TableCell className="font-mono text-xs font-medium text-foreground">{member.username}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{member.email || '—'}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {(member.roles || []).length
                          ? member.roles.map(role => (
                              <Badge key={role} variant="secondary" className="font-mono text-[10px]">{role}</Badge>
                            ))
                          : <span className="text-xs text-muted-foreground">—</span>}
                      </div>
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

export default function TenantsView() {
  const { hasRole } = useAuth()
  const isSuperAdmin = hasRole(SUPER_ADMIN_ROLE)

  const [tenants, setTenants] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(null)

  const [instance, setInstance] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const [createWarning, setCreateWarning] = useState('')
  const [createSuccess, setCreateSuccess] = useState('')

  useEffect(() => {
    if (isSuperAdmin) load()
    else setLoading(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSuperAdmin])

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
    if (!instance.trim() || !displayName.trim()) return
    setCreating(true)
    setCreateError('')
    setCreateWarning('')
    setCreateSuccess('')
    try {
      const result = await fetchJson('/tenants', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instance: instance.trim(), display_name: displayName.trim() })
      })
      setCreateSuccess(`Tenant "${result?.display_name || instance.trim()}" created.`)
      if (result?.warning) setCreateWarning(result.warning)
      setInstance('')
      setDisplayName('')
      await load()
    } catch (submitError) {
      setCreateError(submitError.message)
    } finally {
      setCreating(false)
    }
  }

  // Hard guard: a non-superadmin who reaches the route sees an authz notice and
  // no tenant data is ever fetched.
  if (!isSuperAdmin) {
    return (
      <div className="p-6 max-w-7xl mx-auto space-y-4">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Tenants</h1>
          <p className="text-sm text-muted-foreground mt-1">Tenant administration</p>
        </div>
        <div className="panel p-16 text-center">
          <ShieldAlert className="h-10 w-10 mx-auto mb-3 text-muted-foreground/30" />
          <p className="text-sm font-medium text-foreground">Not authorized</p>
          <p className="text-xs text-muted-foreground mt-1">
            This area is restricted to super administrators.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div>
        <h1 className="text-2xl font-serif font-semibold text-foreground">Tenants</h1>
        <p className="text-sm text-muted-foreground mt-1">Create tenants and provision tenant administrators</p>
      </div>

      {/* Create tenant */}
      <div className="panel">
        <div className="panel-header">
          <div className="flex items-center gap-2">
            <Plus className="h-4 w-4 text-muted-foreground" />
            <h2 className="text-sm font-medium text-foreground">Create tenant</h2>
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">Register a new isolated tenant instance.</p>
        </div>
        <form className="p-4 space-y-4" onSubmit={handleCreate}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label className="text-xs font-medium text-muted-foreground">Instance ID</label>
              <Input
                className="mt-1 h-9 font-mono"
                placeholder="acme-org"
                value={instance}
                onChange={event => setInstance(event.target.value)}
              />
              <p className="text-[10px] text-muted-foreground mt-1">Lowercase identifier, unique across the platform.</p>
            </div>
            <div>
              <label className="text-xs font-medium text-muted-foreground">Display name</label>
              <Input
                className="mt-1 h-9"
                placeholder="Acme Organization"
                value={displayName}
                onChange={event => setDisplayName(event.target.value)}
              />
              <p className="text-[10px] text-muted-foreground mt-1">Human-readable name shown across the console.</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit" size="sm" disabled={creating || !instance.trim() || !displayName.trim()}>
              <Plus className="h-3.5 w-3.5" />
              {creating ? 'Creating…' : 'Create tenant'}
            </Button>
          </div>
          {createSuccess ? <Notice tone="success">{createSuccess}</Notice> : null}
          {createWarning ? <Notice tone="warning">{createWarning}</Notice> : null}
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
          <Building2 className="h-10 w-10 mx-auto mb-3 text-muted-foreground/30" />
          <p className="text-sm font-medium text-foreground">No tenants yet</p>
          <p className="text-xs text-muted-foreground mt-1">Create your first tenant using the form above.</p>
        </div>
      ) : (
        <div className="panel overflow-hidden">
          <div className="panel-header">
            <h2 className="text-sm font-medium text-foreground">All tenants</h2>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Instance</TableHead>
                <TableHead>Display name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
                <TableHead className="text-right">Admins</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tenants.map(tenant => {
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
                      <TableCell className="text-xs text-muted-foreground">{formatCompactDateTime(tenant.created_at)}</TableCell>
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
                          <AddAdminPanel tenant={tenant} />
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
