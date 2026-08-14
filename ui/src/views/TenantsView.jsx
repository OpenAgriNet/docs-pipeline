import React, { useEffect, useState } from 'react'
import { Building2, Check, ChevronDown, ChevronUp, Copy, KeyRound, Plus, RotateCcw, ShieldAlert, Trash2, UserPlus, Users } from 'lucide-react'
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
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { Notice } from '../components/Notice'
import { fetchJson, formatCompactDateTime } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'

const SUPER_ADMIN_ROLE = 'master_admin'
const MANAGE_USERS_PERMISSION = 'manage_users'

// Per-tenant membership roles. Platform roles (master_admin / superadmin) are
// deliberately absent — they can never be assigned as a tenant membership.
const ROLE_OPTIONS = [
  { value: 'admin', label: 'Admin' },
  { value: 'content_curator', label: 'Content curator' },
  { value: 'viewer', label: 'Viewer' },
]

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

function MemberManagementPanel({ tenant }) {
  const instance = tenantKey(tenant)
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('viewer')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [credential, setCredential] = useState(null)

  const [members, setMembers] = useState([])
  const [membersLoading, setMembersLoading] = useState(true)
  const [membersError, setMembersError] = useState('')

  // Per-row mutation state: the user_id currently in-flight, a row-scoped error,
  // and the temporary password returned by a reset (shown once).
  const [rowBusy, setRowBusy] = useState('')
  const [rowError, setRowError] = useState('')
  const [resetCredential, setResetCredential] = useState(null)
  // Pending destructive action awaiting confirmation: {kind: 'remove'|'reset', member}.
  // Both are unrecoverable (a colleague's password cannot be un-reset), so neither
  // fires on a single click.
  const [confirmAction, setConfirmAction] = useState(null)

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

  async function handleAddMember(event) {
    event.preventDefault()
    if (!username.trim()) return
    setSubmitting(true)
    setError('')
    setCredential(null)
    try {
      const body = { username: username.trim(), role }
      if (email.trim()) body.email = email.trim()
      const result = await fetchJson(`/tenants/${encodeURIComponent(instance)}/members`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
      setCredential(result)
      setUsername('')
      setEmail('')
      setRole('viewer')
      await loadMembers()
    } catch (submitError) {
      setError(submitError.message)
    } finally {
      setSubmitting(false)
    }
  }

  function memberPath(member, suffix = '') {
    return `/tenants/${encodeURIComponent(instance)}/members/${encodeURIComponent(member.user_id)}${suffix}`
  }

  async function handleChangeRole(member, nextRole) {
    const current = (member.roles || [])[0]
    if (!member.user_id || nextRole === current) return
    setRowBusy(member.user_id)
    setRowError('')
    setResetCredential(null)
    try {
      await fetchJson(memberPath(member), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: nextRole })
      })
      await loadMembers()
    } catch (mutError) {
      setRowError(mutError.message)
    } finally {
      setRowBusy('')
    }
  }

  async function runConfirmedAction() {
    const pending = confirmAction
    setConfirmAction(null)
    if (!pending) return
    if (pending.kind === 'remove') await handleRemove(pending.member)
    else await handleResetPassword(pending.member)
  }

  async function handleRemove(member) {
    if (!member.user_id) return
    setRowBusy(member.user_id)
    setRowError('')
    setResetCredential(null)
    try {
      await fetchJson(memberPath(member), { method: 'DELETE' })
      await loadMembers()
    } catch (mutError) {
      setRowError(mutError.message)
    } finally {
      setRowBusy('')
    }
  }

  async function handleResetPassword(member) {
    if (!member.user_id) return
    setRowBusy(member.user_id)
    setRowError('')
    setResetCredential(null)
    try {
      const result = await fetchJson(memberPath(member, '/reset-password'), { method: 'POST' })
      setResetCredential({ username: member.username, temporary_password: result?.temporary_password })
    } catch (mutError) {
      setRowError(mutError.message)
    } finally {
      setRowBusy('')
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <UserPlus className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Add member</h3>
        </div>
        <form className="space-y-3" onSubmit={handleAddMember}>
          <div>
            <label className="text-xs font-medium text-muted-foreground">Username</label>
            <Input
              className="mt-1 h-9"
              placeholder="member-username"
              value={username}
              onChange={event => setUsername(event.target.value)}
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground">Email (optional)</label>
            <Input
              className="mt-1 h-9"
              type="email"
              placeholder="member@example.org"
              value={email}
              onChange={event => setEmail(event.target.value)}
            />
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground">Role</label>
            <Select value={role} onValueChange={setRole}>
              <SelectTrigger className="mt-1 h-9">
                <SelectValue placeholder="Select role" />
              </SelectTrigger>
              <SelectContent>
                {ROLE_OPTIONS.map(option => (
                  <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" size="sm" disabled={submitting || !username.trim()}>
            <UserPlus className="h-3.5 w-3.5" />
            {submitting ? 'Adding…' : 'Add member'}
          </Button>
        </form>

        {error ? <Notice tone="error">{error}</Notice> : null}

        {credential ? (
          credential.temporary_password ? (
            <div className="space-y-3 rounded-md border border-success/30 bg-success/5 p-3">
              <div className="flex items-center gap-2">
                <KeyRound className="h-4 w-4 text-success" />
                <p className="text-sm font-medium text-foreground">
                  Member <span className="font-mono">{credential.username}</span> created
                  {credential.role ? <> as <span className="font-mono">{credential.role}</span></> : null}
                </p>
              </div>
              <CopyableSecret label="Temporary password" value={credential.temporary_password} />
              <p className="text-xs text-muted-foreground">
                Copy this now — it is shown only once. The member must change it on first login.
              </p>
            </div>
          ) : (
            <Notice tone="success">
              Existing user <span className="font-mono">{credential.username}</span> was added to
              this tenant{credential.role ? <> as <span className="font-mono">{credential.role}</span></> : null}.
              Their password was left unchanged.
            </Notice>
          )
        ) : null}
      </div>

      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Users className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium text-foreground">Members</h3>
        </div>

        {rowError ? <Notice tone="error">{rowError}</Notice> : null}

        {resetCredential ? (
          <div className="space-y-3 rounded-md border border-success/30 bg-success/5 p-3">
            <div className="flex items-center gap-2">
              <KeyRound className="h-4 w-4 text-success" />
              <p className="text-sm font-medium text-foreground">
                Password reset for <span className="font-mono">{resetCredential.username}</span>
              </p>
            </div>
            <CopyableSecret label="Temporary password" value={resetCredential.temporary_password} />
            <p className="text-xs text-muted-foreground">
              Copy this now — it is shown only once. The member must change it on first login.
            </p>
          </div>
        ) : null}

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
          <div className="panel overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Username</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {members.map(member => {
                  const busy = rowBusy === member.user_id
                  const primaryRole = (member.roles || [])[0] || ''
                  const canAct = Boolean(member.user_id)
                  return (
                    <TableRow key={member.user_id || member.username}>
                      <TableCell className="font-mono text-xs font-medium text-foreground">{member.username}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{member.email || '—'}</TableCell>
                      <TableCell>
                        <Select
                          value={primaryRole}
                          onValueChange={next => handleChangeRole(member, next)}
                          disabled={!canAct || busy}
                        >
                          <SelectTrigger className="h-8 w-40 text-xs">
                            <SelectValue placeholder="—" />
                          </SelectTrigger>
                          <SelectContent>
                            {ROLE_OPTIONS.map(option => (
                              <SelectItem key={option.value} value={option.value} className="text-xs">{option.label}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 text-xs"
                            disabled={!canAct || busy}
                            onClick={() => setConfirmAction({ kind: 'reset', member })}
                          >
                            <RotateCcw className="h-3 w-3" />
                            Reset password
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 text-xs text-destructive hover:text-destructive"
                            disabled={!canAct || busy}
                            onClick={() => setConfirmAction({ kind: 'remove', member })}
                          >
                            <Trash2 className="h-3 w-3" />
                            Remove
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* Confirm destructive member actions — removal detaches every role in this
          tenant; a password reset invalidates the member's current credential. */}
      <AlertDialog open={!!confirmAction} onOpenChange={open => { if (!open) setConfirmAction(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmAction?.kind === 'remove'
                ? `Remove “${confirmAction?.member?.username}” from ${instance}?`
                : `Reset the password for “${confirmAction?.member?.username}”?`}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmAction?.kind === 'remove'
                ? 'This detaches the member from every role group in this tenant. Their Keycloak account is kept, but they lose access to this tenant until re-added.'
                : 'Their current password stops working immediately and is replaced by a temporary one shown once here. This cannot be undone.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className={confirmAction?.kind === 'remove' ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : undefined}
              onClick={event => { event.preventDefault(); runConfirmedAction() }}
            >
              {confirmAction?.kind === 'remove' ? 'Remove member' : 'Reset password'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

export default function TenantsView() {
  const { hasRole, hasPermission, instances: myInstances } = useAuth()
  const isSuperAdmin = hasRole(SUPER_ADMIN_ROLE)
  // A tenant admin (holds manage_users in one or more tenants) self-manages its
  // own tenants' members without the platform-wide create/list surfaces. The
  // backend is the source of truth (404/403 per tenant); this is only the gate
  // that decides which surface to render.
  const canManageUsers = hasPermission(MANAGE_USERS_PERMISSION)
  const managedInstances = !isSuperAdmin && canManageUsers ? (myInstances || []) : []
  const isTenantManager = managedInstances.length > 0

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
      // Response shape is { tenant, default_index, keycloak } — read the created
      // tenant's display name (falling back to its instance id) from result.tenant.
      const createdTenant = result?.tenant
      const createdName = createdTenant?.display_name || createdTenant?.id || instance.trim()
      setCreateSuccess(`Tenant "${createdName}" created.`)
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

  // Tenant-admin surface: not a platform super-admin, but manages users in one or
  // more tenants. Show only those tenants' member panels — no platform-wide
  // create-tenant / list-all surfaces (which the backend gates to master_admin).
  if (!isSuperAdmin && isTenantManager) {
    return (
      <div className="p-6 max-w-7xl mx-auto space-y-4">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Your tenants</h1>
          <p className="text-sm text-muted-foreground mt-1">Manage members for the tenants you administer</p>
        </div>
        <div className="panel overflow-hidden">
          <div className="panel-header">
            <h2 className="text-sm font-medium text-foreground">Tenants</h2>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Instance</TableHead>
                <TableHead className="text-right">Members</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {managedInstances.map(inst => {
                const isOpen = expanded === inst
                return (
                  <React.Fragment key={inst}>
                    <TableRow>
                      <TableCell className="font-mono text-xs font-medium text-foreground">{inst}</TableCell>
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs"
                          onClick={() => setExpanded(isOpen ? null : inst)}
                        >
                          {isOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                          Manage
                        </Button>
                      </TableCell>
                    </TableRow>
                    {isOpen ? (
                      <TableRow className="hover:bg-transparent">
                        <TableCell colSpan={2} className="bg-muted/30 p-4">
                          <MemberManagementPanel tenant={{ instance: inst }} />
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </React.Fragment>
                )
              })}
            </TableBody>
          </Table>
        </div>
      </div>
    )
  }

  // Hard guard: a caller who is neither a platform super-admin nor a tenant
  // manager sees an authz notice and no tenant data is ever fetched.
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
                          <MemberManagementPanel tenant={tenant} />
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
