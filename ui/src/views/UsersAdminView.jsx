import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Copy,
  LayoutDashboard,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Shield,
  UserPlus,
  Users,
} from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import { Switch } from '../components/ui/switch'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from '../components/ui/sheet'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../components/ui/alert-dialog'
import { fetchJson } from '../lib/pipelineUi'
import { cn } from '../lib/utils'

const PAGE_SIZE = 6

const EMPTY_FORM = {
  email: '',
  first_name: '',
  last_name: '',
  username: '',
  access_type: 'state',
  state: 'MH',
  role: 'state_admin',
  enabled: true,
}

function errorMessage(err) {
  const raw = err?.message || err
  if (!raw) return 'Request failed'
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw)
      return parsed.errorMessage || parsed.error || raw
    } catch {
      return raw
    }
  }
  return String(raw)
}

function initials(name, email) {
  const source = (name || email || '?').trim()
  const parts = source.split(/\s+/).filter(Boolean)
  if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.toUpperCase()
  return source.slice(0, 2).toUpperCase()
}

/** Access types that represent a real product group (not baseline SSO). */
const PRODUCT_ACCESS_TYPES = new Set(['super_admin', 'bh_viewer', 'state'])

function AccessBadge({ user }) {
  if (user.access_type === 'super_admin') {
    return (
      <Badge className="gap-1 text-[10px] font-medium">
        <Shield className="size-3" />
        Super Admin
      </Badge>
    )
  }
  if (user.access_type === 'bh_viewer') {
    return (
      <Badge variant="secondary" className="gap-1 text-[10px] font-medium">
        <Shield className="size-3" />
        BH Viewer
      </Badge>
    )
  }
  if (user.access_type === 'state') {
    const states = user.states || []
    const roles = user.roles || []
    if (!states.length) {
      return (
        <Badge variant="outline" className="text-[10px] font-medium">
          {user.access_label || 'State'}
        </Badge>
      )
    }
    return (
      <div className="flex flex-wrap gap-1">
        {states.map((s, i) => {
          const role = roles[i] || roles[0] || ''
          return (
            <Badge key={`${s}-${role}-${i}`} variant="outline" className="text-[10px] font-medium capitalize">
              {s}
              {role ? ` · ${role}` : ''}
            </Badge>
          )
        })}
      </div>
    )
  }
  // No product group — baseline SSO / dashboard access
  return (
    <Badge variant="secondary" className="gap-1 text-[10px] font-medium text-foreground">
      <LayoutDashboard className="size-3 opacity-70" />
      Dashboard
    </Badge>
  )
}

export default function UsersAdminView() {
  const { hasPermission, isSuperAdmin } = useAuth()
  const canManage = hasPermission('manage_users') || isSuperAdmin

  const [options, setOptions] = useState(null)
  const [users, setUsers] = useState([])
  const [query, setQuery] = useState('')
  /** all | super_admin | bh_viewer | state | dashboard */
  const [accessFilter, setAccessFilter] = useState('all')
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [sheetOpen, setSheetOpen] = useState(false)
  const [form, setForm] = useState(EMPTY_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [shareResult, setShareResult] = useState(null)
  const [copied, setCopied] = useState(false)
  // Change-role dialog: a user holds exactly one product role at a time.
  const [roleUser, setRoleUser] = useState(null)
  const [roleForm, setRoleForm] = useState({ access_type: 'state', state: 'MH', role: 'state_admin' })
  const [roleSaving, setRoleSaving] = useState(false)
  const [roleError, setRoleError] = useState('')

  /** Seed the dialog from the user's current group so it opens on their role. */
  const openRoleEdit = useCallback((u) => {
    const path = (u.groups || []).find((g) => {
      const parts = g.split('/').filter(Boolean)
      return g === '/global/super-admin' || g === '/global/bh-viewer' || parts.length === 3
    })
    let next = { access_type: 'state', state: 'MH', role: 'state_admin' }
    if (path === '/global/super-admin') {
      next = { access_type: 'super_admin', state: '', role: '' }
    } else if (path === '/global/bh-viewer') {
      next = { access_type: 'bh_viewer', state: '', role: '' }
    } else if (path) {
      const [, code, leaf] = path.split('/').filter(Boolean)
      const byLeaf = {
        admin: 'state_admin',
        approver: 'state_approver',
        contributor: 'state_contributor',
        view: 'state_view',
      }
      next = {
        access_type: 'state',
        state: (code || 'MH').toUpperCase(),
        role: byLeaf[(leaf || '').toLowerCase()] || 'state_admin',
      }
    }
    setRoleForm(next)
    setRoleError('')
    setRoleUser(u)
  }, [])

  const loadUsers = useCallback(async (search = '') => {
    setLoading(true)
    setError('')
    try {
      const qs = search.trim() ? `?search=${encodeURIComponent(search.trim())}` : ''
      const data = await fetchJson(`/admin/users${qs}`)
      setUsers(Array.isArray(data?.users) ? data.users : [])
    } catch (err) {
      setError(errorMessage(err))
      setUsers([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!canManage) return
    let cancelled = false
    ;(async () => {
      try {
        const opts = await fetchJson('/admin/access-options')
        if (!cancelled) setOptions(opts)
      } catch {
        // options optional for form defaults
      }
      if (!cancelled) await loadUsers()
    })()
    return () => {
      cancelled = true
    }
  }, [canManage, loadUsers])

  const accessCounts = useMemo(() => {
    const counts = { all: users.length, super_admin: 0, bh_viewer: 0, state: 0, dashboard: 0 }
    for (const u of users) {
      const t = PRODUCT_ACCESS_TYPES.has(u.access_type) ? u.access_type : 'dashboard'
      counts[t] = (counts[t] || 0) + 1
    }
    return counts
  }, [users])

  const filtered = useMemo(() => {
    let list = users
    if (accessFilter === 'dashboard') {
      // Dashboard = no product group at all (baseline SSO access).
      list = list.filter((u) => !PRODUCT_ACCESS_TYPES.has(u.access_type))
    } else if (accessFilter !== 'all') {
      list = list.filter((u) => u.access_type === accessFilter)
    }
    const q = query.trim().toLowerCase()
    if (!q) return list
    return list.filter((u) =>
      [u.email, u.username, u.name, u.access_label, ...(u.groups || [])]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q)),
    )
  }, [users, query, accessFilter])

  useEffect(() => {
    setPage(1)
  }, [query, accessFilter])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const paginated = useMemo(() => {
    const safePage = Math.min(page, totalPages)
    const start = (safePage - 1) * PAGE_SIZE
    return filtered.slice(start, start + PAGE_SIZE)
  }, [filtered, page, totalPages])

  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages])

  function update(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }))
  }

  function openCreate() {
    setForm(EMPTY_FORM)
    setSheetOpen(true)
  }

  async function handleSubmit(event) {
    event.preventDefault()
    if (!canManage) return
    setSubmitting(true)
    setError('')
    try {
      const data = await fetchJson('/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: form.email.trim(),
          first_name: form.first_name.trim(),
          last_name: form.last_name.trim(),
          username: form.username.trim(),
          access_type: form.access_type,
          state: form.access_type === 'state' ? form.state : '',
          role: form.access_type === 'state' ? form.role : '',
          enabled: form.enabled,
        }),
      })
      setSheetOpen(false)
      setShareResult(data)
      setShareOpen(true)
      setCopied(false)
      await loadUsers()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function saveRole() {
    if (!roleUser) return
    setRoleSaving(true)
    setRoleError('')
    try {
      await fetchJson(`/admin/users/${encodeURIComponent(roleUser.user_id)}/access`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          access_type: roleForm.access_type,
          state: roleForm.access_type === 'state' ? roleForm.state : '',
          role: roleForm.access_type === 'state' ? roleForm.role : '',
        }),
      })
      setRoleUser(null)
      await loadUsers(query)
    } catch (err) {
      setRoleError(errorMessage(err))
    } finally {
      setRoleSaving(false)
    }
  }

  async function copyShare() {
    if (!shareResult?.share_message) return
    try {
      await navigator.clipboard.writeText(shareResult.share_message)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('Could not copy to clipboard')
    }
  }

  if (!canManage) {
    return (
      <div className="mx-auto flex max-w-md flex-col items-center px-6 py-16 text-center">
        <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-muted">
          <Shield className="size-6 text-muted-foreground" />
        </div>
        <h1 className="font-serif text-xl font-semibold text-foreground">Access restricted</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Only Super Admins can manage users.
        </p>
      </div>
    )
  }

  return (
    <div className="page-shell space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-serif text-2xl font-semibold tracking-tight text-foreground">Users</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Provision SSO access via Keycloak
            {options?.realm ? (
              <span className="text-muted-foreground/80"> · {options.realm}</span>
            ) : null}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9"
            onClick={() => loadUsers()}
            disabled={loading}
          >
            <RefreshCw className={cn('mr-1.5 size-3.5', loading && 'animate-spin')} />
            Refresh
          </Button>
          <Button type="button" size="sm" className="h-9" onClick={openCreate}>
            <Plus className="mr-1.5 size-3.5" />
            Add user
          </Button>
        </div>
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2.5 text-sm text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </div>
      ) : null}

      {options && options.keycloak_admin_configured === false ? (
        <div className="rounded-lg border border-amber-200/80 bg-amber-50 px-3 py-2.5 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-950/40 dark:text-amber-100">
          Set <code className="text-xs">KEYCLOAK_ADMIN_USERNAME</code> and{' '}
          <code className="text-xs">KEYCLOAK_ADMIN_PASSWORD</code> on the API, then restart.
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-[200px] max-w-sm flex-1">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search name or email…"
            className="h-9 pl-9"
          />
        </div>
        <Select value={accessFilter} onValueChange={setAccessFilter}>
          <SelectTrigger className="h-9 w-[200px]">
            <SelectValue placeholder="Access" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All access ({accessCounts.all})</SelectItem>
            <SelectItem value="super_admin">Super Admin ({accessCounts.super_admin})</SelectItem>
            <SelectItem value="bh_viewer">BH Viewer ({accessCounts.bh_viewer})</SelectItem>
            <SelectItem value="state">State role ({accessCounts.state})</SelectItem>
            <SelectItem value="dashboard">Dashboard ({accessCounts.dashboard})</SelectItem>
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">
          {filtered.length} user{filtered.length === 1 ? '' : 's'}
        </span>
      </div>

      <div className="panel flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="page-scroll overflow-x-auto">
          <table className="w-full min-w-[48rem] text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <th className="px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  User
                </th>
                <th className="px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Access
                </th>
                <th className="px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Groups
                </th>
                <th className="px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Role
                </th>
                <th className="px-4 py-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Status
                </th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                Array.from({ length: PAGE_SIZE }).map((_, i) => (
                  <tr key={i} className="border-b border-border">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-3">
                        <Skeleton className="size-9 rounded-full" />
                        <div className="space-y-1.5">
                          <Skeleton className="h-3.5 w-36" />
                          <Skeleton className="h-3 w-44" />
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Skeleton className="h-5 w-24 rounded-full" />
                    </td>
                    <td className="px-4 py-3">
                      <Skeleton className="h-3 w-40" />
                    </td>
                    <td className="px-4 py-3">
                      <Skeleton className="h-5 w-16 rounded-full" />
                    </td>
                  </tr>
                ))
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-16 text-center">
                    <Users className="mx-auto mb-2 size-8 text-muted-foreground/50" />
                    <p className="text-sm font-medium text-foreground">No users found</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Add a user to grant Super Admin or state access.
                    </p>
                    <Button type="button" size="sm" className="mt-4" onClick={openCreate}>
                      <UserPlus className="mr-1.5 size-3.5" />
                      Add user
                    </Button>
                  </td>
                </tr>
              ) : (
                paginated.map((u) => (
                  <tr key={u.user_id} className="border-b border-border last:border-0 hover:bg-muted/30">
                    <td className="px-4 py-3">
                      <div className="flex min-w-0 items-center gap-3">
                        <div
                          className={cn(
                            'flex size-9 shrink-0 items-center justify-center rounded-full',
                            'bg-primary/12 text-[11px] font-semibold text-primary',
                          )}
                        >
                          {initials(u.name, u.email)}
                        </div>
                        <div className="min-w-0">
                          <div className="truncate font-medium text-foreground">
                            {u.name || u.username || '—'}
                          </div>
                          <div className="truncate text-xs text-muted-foreground">{u.email || '—'}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <AccessBadge user={u} />
                    </td>
                    <td className="px-4 py-3">
                      <div className="max-w-[220px] space-y-0.5 font-mono text-[10px] text-muted-foreground">
                        {(u.groups || []).length ? (
                          (u.groups || []).slice(0, 3).map((g) => (
                            <div key={g} className="truncate">
                              {g}
                            </div>
                          ))
                        ) : (
                          <span>—</span>
                        )}
                        {(u.groups || []).length > 3 ? (
                          <span className="text-[10px]">+{u.groups.length - 3} more</span>
                        ) : null}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-7 px-2 text-[11px]"
                        onClick={() => openRoleEdit(u)}
                      >
                        <Shield className="mr-1 size-3" />
                        Change role
                      </Button>
                    </td>
                    <td className="px-4 py-3">
                      {u.enabled ? (
                        <Badge variant="success" className="text-[10px]">
                          Active
                        </Badge>
                      ) : (
                        <Badge variant="secondary" className="text-[10px]">
                          Disabled
                        </Badge>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {!loading && filtered.length > 0 ? (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3">
            <p className="text-xs text-muted-foreground">
              Showing {(Math.min(page, totalPages) - 1) * PAGE_SIZE + 1}–
              {Math.min(Math.min(page, totalPages) * PAGE_SIZE, filtered.length)} of {filtered.length}
            </p>
            <div className="flex items-center gap-1.5">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 px-2"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                aria-label="Previous page"
              >
                <ChevronLeft className="size-4" />
              </Button>
              <span className="min-w-[4.5rem] text-center text-xs font-medium text-foreground">
                {Math.min(page, totalPages)} / {totalPages}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 px-2"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                aria-label="Next page"
              >
                <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        ) : null}
      </div>

      {/* Create user sheet */}
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-md">
          <SheetHeader className="space-y-1 border-b border-border px-5 py-4 text-left">
            <SheetTitle className="font-serif text-lg">Add user</SheetTitle>
            <SheetDescription className="text-xs">
              Google SSO email + access level. No password required.
            </SheetDescription>
          </SheetHeader>

          <form onSubmit={handleSubmit} className="flex flex-1 flex-col overflow-y-auto">
            <div className="space-y-4 px-5 py-4">
              <div className="space-y-1.5">
                <Label htmlFor="email" className="text-xs font-medium">
                  Email
                </Label>
                <Input
                  id="email"
                  type="email"
                  required
                  autoComplete="off"
                  placeholder="name@gmail.com"
                  className="h-9"
                  value={form.email}
                  onChange={(e) => update('email', e.target.value)}
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="first_name" className="text-xs font-medium">
                    First name
                  </Label>
                  <Input
                    id="first_name"
                    className="h-9"
                    value={form.first_name}
                    onChange={(e) => update('first_name', e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="last_name" className="text-xs font-medium">
                    Last name
                  </Label>
                  <Input
                    id="last_name"
                    className="h-9"
                    value={form.last_name}
                    onChange={(e) => update('last_name', e.target.value)}
                  />
                </div>
              </div>

              <div className="space-y-1.5">
                <Label className="text-xs font-medium">Access</Label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => update('access_type', 'super_admin')}
                    className={cn(
                      'rounded-lg border px-3 py-2.5 text-left transition-colors',
                      form.access_type === 'super_admin'
                        ? 'border-primary/50 bg-primary/8'
                        : 'border-border hover:bg-muted/40',
                    )}
                  >
                    <div className="text-xs font-semibold text-foreground">Super Admin</div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">All states · BV</div>
                  </button>
                  <button
                    type="button"
                    onClick={() => update('access_type', 'bh_viewer')}
                    className={cn(
                      'rounded-lg border px-3 py-2.5 text-left transition-colors',
                      form.access_type === 'bh_viewer'
                        ? 'border-primary/50 bg-primary/8'
                        : 'border-border hover:bg-muted/40',
                    )}
                  >
                    <div className="text-xs font-semibold text-foreground">BH Viewer</div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">
                      All states · read only
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => update('access_type', 'state')}
                    className={cn(
                      'rounded-lg border px-3 py-2.5 text-left transition-colors',
                      form.access_type === 'state'
                        ? 'border-primary/50 bg-primary/8'
                        : 'border-border hover:bg-muted/40',
                    )}
                  >
                    <div className="text-xs font-semibold text-foreground">State role</div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">One state only</div>
                  </button>
                </div>
              </div>

              {form.access_type === 'state' ? (
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label className="text-xs font-medium">State</Label>
                    <Select value={form.state} onValueChange={(v) => update('state', v)}>
                      <SelectTrigger className="h-9">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {(options?.states || [{ code: 'MH', label: 'MH' }]).map((s) => (
                          <SelectItem key={s.code} value={s.code}>
                            {s.code}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs font-medium">Role</Label>
                    <Select value={form.role} onValueChange={(v) => update('role', v)}>
                      <SelectTrigger className="h-9">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="state_admin">Admin — full access in state</SelectItem>
                        <SelectItem value="state_approver">Approver — no delete</SelectItem>
                        <SelectItem value="state_contributor">
                          Contributor — no delete, no DEV publish
                        </SelectItem>
                        <SelectItem value="state_view">View — read only</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              ) : null}

              <div className="flex items-center justify-between rounded-lg border border-border px-3 py-2">
                <Label htmlFor="enabled" className="text-xs font-medium">
                  Active
                </Label>
                <Switch
                  id="enabled"
                  checked={form.enabled}
                  onCheckedChange={(v) => update('enabled', v)}
                />
              </div>
            </div>

            <SheetFooter className="mt-auto border-t border-border px-5 py-4 sm:justify-stretch">
              <Button
                type="submit"
                className="w-full"
                disabled={submitting || !form.email.trim()}
              >
                {submitting ? (
                  <>
                    <Loader2 className="mr-2 size-4 animate-spin" />
                    Creating…
                  </>
                ) : (
                  <>
                    <UserPlus className="mr-2 size-4" />
                    Create access
                  </>
                )}
              </Button>
            </SheetFooter>
          </form>
        </SheetContent>
      </Sheet>

      {/* Share popup after create */}
      <AlertDialog open={shareOpen} onOpenChange={setShareOpen}>
        <AlertDialogContent className="max-w-md gap-0 overflow-hidden p-0 sm:rounded-xl">
          <AlertDialogHeader className="space-y-2 border-b border-border px-5 py-4 text-left">
            <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400">
              <CheckCircle2 className="size-5" />
              <AlertDialogTitle className="font-serif text-lg text-foreground">
                {shareResult?.created ? 'User created' : 'Access updated'}
              </AlertDialogTitle>
            </div>
            <AlertDialogDescription className="text-xs text-muted-foreground">
              Share these details with{' '}
              <span className="font-medium text-foreground">{shareResult?.email}</span>
            </AlertDialogDescription>
          </AlertDialogHeader>

          <div className="space-y-3 px-5 py-4">
            <div className="flex flex-wrap gap-1.5">
              <Badge className="text-[10px]">
                {shareResult?.access_type === 'super_admin'
                  ? 'Super Admin'
                  : `${shareResult?.state || ''} · ${shareResult?.role || ''}`}
              </Badge>
              <Badge variant="outline" className="font-mono text-[10px]">
                {shareResult?.group_path}
              </Badge>
            </div>
            <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-muted/50 p-3 font-sans text-[12px] leading-relaxed text-foreground">
              {shareResult?.share_message}
            </pre>
          </div>

          <AlertDialogFooter className="gap-2 border-t border-border px-5 py-3 sm:space-x-0">
            <Button type="button" variant="outline" className="sm:flex-1" onClick={copyShare}>
              <Copy className="mr-1.5 size-3.5" />
              {copied ? 'Copied' : 'Copy message'}
            </Button>
            <AlertDialogAction className="sm:flex-1" onClick={() => setShareOpen(false)}>
              Done
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Sheet open={Boolean(roleUser)} onOpenChange={(open) => (open ? null : setRoleUser(null))}>
        <SheetContent className="flex w-full flex-col gap-0 p-0 sm:max-w-md">
          <SheetHeader className="space-y-1 border-b border-border px-5 py-4 text-left">
            <SheetTitle className="font-serif text-lg">Change role</SheetTitle>
            <SheetDescription className="text-xs text-muted-foreground">
              {roleUser?.email || roleUser?.username} — a user holds exactly one role, so this
              replaces their current access.
            </SheetDescription>
          </SheetHeader>

          <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
            <div className="rounded-lg border border-border bg-muted/30 px-3 py-2">
              <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                Current
              </div>
              <div className="mt-0.5 font-mono text-[11px] text-foreground">
                {(roleUser?.groups || []).join(', ') || 'No product group (dashboard only)'}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs font-medium">Access</Label>
              <Select
                value={roleForm.access_type}
                onValueChange={(v) => setRoleForm((f) => ({ ...f, access_type: v }))}
              >
                <SelectTrigger className="h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="super_admin">Super Admin — all states</SelectItem>
                  <SelectItem value="bh_viewer">BH Viewer — all states, read only</SelectItem>
                  <SelectItem value="state">State / Centre role</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {roleForm.access_type === 'state' ? (
              <>
                <div className="space-y-1.5">
                  <Label className="text-xs font-medium">State / Centre</Label>
                  <Select
                    value={roleForm.state}
                    onValueChange={(v) => setRoleForm((f) => ({ ...f, state: v }))}
                  >
                    <SelectTrigger className="h-9">
                      <SelectValue placeholder="Select" />
                    </SelectTrigger>
                    <SelectContent>
                      {(options?.states || []).map((s) => (
                        <SelectItem key={s.code} value={s.code}>
                          {s.label || s.code}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label className="text-xs font-medium">Role</Label>
                  <Select
                    value={roleForm.role}
                    onValueChange={(v) => setRoleForm((f) => ({ ...f, role: v }))}
                  >
                    <SelectTrigger className="h-9">
                      <SelectValue placeholder="Select" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="state_admin">Admin — full access in state</SelectItem>
                      <SelectItem value="state_approver">Approver — no delete</SelectItem>
                      <SelectItem value="state_contributor">
                        Contributor — no delete, no DEV publish
                      </SelectItem>
                      <SelectItem value="state_view">View — read only</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </>
            ) : null}

            <p className="text-[11px] leading-relaxed text-muted-foreground">
              The user must sign out and sign back in before the new role takes effect — roles
              are read from their login token.
            </p>

            {roleError ? (
              <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2">
                <AlertCircle className="mt-0.5 size-3.5 shrink-0 text-destructive" />
                <p className="text-xs text-destructive">{roleError}</p>
              </div>
            ) : null}
          </div>

          <SheetFooter className="gap-2 border-t border-border px-5 py-3 sm:space-x-0">
            <Button
              type="button"
              variant="outline"
              className="sm:flex-1"
              onClick={() => setRoleUser(null)}
              disabled={roleSaving}
            >
              Cancel
            </Button>
            <Button type="button" className="sm:flex-1" onClick={saveRole} disabled={roleSaving}>
              {roleSaving ? <Loader2 className="mr-1.5 size-3.5 animate-spin" /> : null}
              Save role
            </Button>
          </SheetFooter>
        </SheetContent>
      </Sheet>
    </div>
  )
}
