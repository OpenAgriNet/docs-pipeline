import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Copy,
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

const EMPTY_FORM = {
  email: '',
  first_name: '',
  last_name: '',
  username: '',
  access_type: 'state',
  state: 'MH',
  role: 'contributor',
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

function AccessBadge({ user }) {
  if (user.access_type === 'super_admin') {
    return (
      <Badge className="text-[10px] font-medium">
        <Shield className="mr-1 h-3 w-3" />
        Super Admin
      </Badge>
    )
  }
  if (user.access_type === 'state') {
    return (
      <div className="flex flex-wrap gap-1">
        {(user.states || []).map((s, i) => (
          <Badge key={`${s}-${i}`} variant="outline" className="text-[10px] font-medium">
            {s}
            {user.roles?.[i] ? ` · ${user.roles[i]}` : user.roles?.[0] ? ` · ${user.roles[0]}` : ''}
          </Badge>
        ))}
        {!user.states?.length ? (
          <Badge variant="secondary" className="text-[10px]">
            {user.access_label || 'State'}
          </Badge>
        ) : null}
      </div>
    )
  }
  return (
    <Badge variant="secondary" className="text-[10px] font-normal text-muted-foreground">
      {user.access_label || '—'}
    </Badge>
  )
}

export default function UsersAdminView() {
  const { hasPermission, isSuperAdmin } = useAuth()
  const canManage = hasPermission('manage_users') || isSuperAdmin

  const [options, setOptions] = useState(null)
  const [users, setUsers] = useState([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [sheetOpen, setSheetOpen] = useState(false)
  const [form, setForm] = useState(EMPTY_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [shareResult, setShareResult] = useState(null)
  const [copied, setCopied] = useState(false)

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

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return users
    return users.filter((u) =>
      [u.email, u.username, u.name, u.access_label, ...(u.groups || [])]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q)),
    )
  }, [users, query])

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
    <div className="mx-auto max-w-7xl space-y-5 p-6">
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
        <div className="relative min-w-[220px] max-w-sm flex-1">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name, email, access…"
            className="h-9 pl-9"
          />
        </div>
        <span className="text-xs text-muted-foreground">
          {filtered.length} user{filtered.length === 1 ? '' : 's'}
        </span>
      </div>

      <div className="panel overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
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
                  Status
                </th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                Array.from({ length: 6 }).map((_, i) => (
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
                  <td colSpan={4} className="px-4 py-16 text-center">
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
                filtered.map((u) => (
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
                        <SelectItem value="contributor">Contributor</SelectItem>
                        <SelectItem value="reviewer">Reviewer</SelectItem>
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
    </div>
  )
}
