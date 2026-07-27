import React, { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  Loader2,
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
import { Switch } from '../components/ui/switch'
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

function fieldError(detail) {
  if (!detail) return 'Request failed'
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
  }
  if (typeof detail === 'object') {
    return detail.errorMessage || detail.error || JSON.stringify(detail)
  }
  return String(detail)
}

export default function UsersAdminView() {
  const { hasPermission, isSuperAdmin } = useAuth()
  const canManage = hasPermission('manage_users') || isSuperAdmin

  const [options, setOptions] = useState(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [loadingOptions, setLoadingOptions] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!canManage) return
    let cancelled = false
    ;(async () => {
      setLoadingOptions(true)
      setError('')
      try {
        const data = await fetchJson('/admin/access-options')
        if (!cancelled) setOptions(data)
      } catch (err) {
        if (!cancelled) setError(err.message || 'Failed to load access options')
      } finally {
        if (!cancelled) setLoadingOptions(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [canManage])

  const previewShare = useMemo(() => {
    const email = form.email.trim() || '<email>'
    const access =
      form.access_type === 'super_admin'
        ? 'Super Admin (Bharat Vistaar — all states)'
        : `${(form.state || 'MH').toUpperCase()} · ${(form.role || 'contributor').replace(/^\w/, (c) => c.toUpperCase())}`
    const group =
      form.access_type === 'super_admin'
        ? '/global/super-admin'
        : `/states/${(form.state || 'MH').toUpperCase()}/${form.role || 'contributor'}`
    return [
      'You have been given access to the Docs Pipeline console.',
      '',
      'App URL: (set after create from server)',
      'Sign-in: Continue with SSO (Google)',
      `Use this Google account email: ${email}`,
      '',
      `Access level: ${access}`,
      `Keycloak group: ${group}`,
      '',
      'Steps: open app → SSO → pick this Google account.',
    ].join('\n')
  }, [form])

  function update(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }))
    setResult(null)
    setCopied(false)
  }

  async function handleSubmit(event) {
    event.preventDefault()
    if (!canManage) return
    setSubmitting(true)
    setError('')
    setResult(null)
    setCopied(false)
    try {
      const body = {
        email: form.email.trim(),
        first_name: form.first_name.trim(),
        last_name: form.last_name.trim(),
        username: form.username.trim(),
        access_type: form.access_type,
        state: form.access_type === 'state' ? form.state : '',
        role: form.access_type === 'state' ? form.role : '',
        enabled: form.enabled,
      }
      const data = await fetchJson('/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      setResult(data)
    } catch (err) {
      setError(fieldError(err.message) || 'Failed to provision user')
    } finally {
      setSubmitting(false)
    }
  }

  async function copyShare() {
    const text = result?.share_message || previewShare
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('Could not copy to clipboard')
    }
  }

  if (!canManage) {
    return (
      <div className="mx-auto max-w-lg p-8 text-center">
        <Shield className="mx-auto mb-3 h-10 w-10 text-muted-foreground" />
        <h1 className="text-lg font-semibold">Access restricted</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Only Super Admins can provision users. You need the <code>manage_users</code> permission
          (Keycloak group <code>/global/super-admin</code>).
        </p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
            Administration
          </p>
          <h1 className="mt-1 flex items-center gap-2 text-2xl font-serif font-semibold text-foreground">
            <Users className="h-6 w-6 text-primary" />
            Users & access
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Create Keycloak access for Super Admin (BV platform) or a state Contributor / Reviewer.
            Users sign in with Google SSO — enter the same email they use for Google.
          </p>
        </div>
        {options?.keycloak_admin_configured === false ? (
          <Badge variant="warning" className="text-xs">
            Keycloak admin env not set
          </Badge>
        ) : options?.keycloak_admin_configured ? (
          <Badge variant="success" className="text-xs">
            Keycloak admin ready · {options.realm}
          </Badge>
        ) : null}
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-5">
        <form
          onSubmit={handleSubmit}
          className="panel space-y-4 p-5 lg:col-span-3"
        >
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <UserPlus className="h-4 w-4 text-primary" />
            Provision user
          </div>

          {loadingOptions ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading options…
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2 space-y-1.5">
              <Label htmlFor="email">Email (Google SSO) *</Label>
              <Input
                id="email"
                type="email"
                required
                placeholder="name@gmail.com"
                value={form.email}
                onChange={(e) => update('email', e.target.value)}
              />
              <p className="text-[11px] text-muted-foreground">
                Must match the Google account they will use to sign in.
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="first_name">First name</Label>
              <Input
                id="first_name"
                value={form.first_name}
                onChange={(e) => update('first_name', e.target.value)}
                placeholder="Akshat"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="last_name">Last name</Label>
              <Input
                id="last_name"
                value={form.last_name}
                onChange={(e) => update('last_name', e.target.value)}
                placeholder="Rana"
              />
            </div>
            <div className="sm:col-span-2 space-y-1.5">
              <Label htmlFor="username">Username (optional)</Label>
              <Input
                id="username"
                value={form.username}
                onChange={(e) => update('username', e.target.value)}
                placeholder="Defaults from email local-part"
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label>Access type *</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => update('access_type', 'super_admin')}
                className={cn(
                  'rounded-xl border px-3 py-3 text-left transition-colors',
                  form.access_type === 'super_admin'
                    ? 'border-primary bg-primary/5 ring-1 ring-primary/20'
                    : 'border-border hover:bg-muted/50',
                )}
              >
                <div className="text-sm font-semibold">Super Admin (BV)</div>
                <div className="mt-1 text-[11px] text-muted-foreground">
                  All states, settings, user management. Group{' '}
                  <code className="text-[10px]">/global/super-admin</code>
                </div>
              </button>
              <button
                type="button"
                onClick={() => update('access_type', 'state')}
                className={cn(
                  'rounded-xl border px-3 py-3 text-left transition-colors',
                  form.access_type === 'state'
                    ? 'border-primary bg-primary/5 ring-1 ring-primary/20'
                    : 'border-border hover:bg-muted/50',
                )}
              >
                <div className="text-sm font-semibold">State role</div>
                <div className="mt-1 text-[11px] text-muted-foreground">
                  Limited to one state as contributor or reviewer.
                </div>
              </button>
            </div>
          </div>

          {form.access_type === 'state' ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>State *</Label>
                <Select value={form.state} onValueChange={(v) => update('state', v)}>
                  <SelectTrigger>
                    <SelectValue placeholder="State" />
                  </SelectTrigger>
                  <SelectContent>
                    {(options?.states || [{ code: 'MH', label: 'MH' }]).map((s) => (
                      <SelectItem key={s.code} value={s.code}>
                        {s.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Role *</Label>
                <Select value={form.role} onValueChange={(v) => update('role', v)}>
                  <SelectTrigger>
                    <SelectValue placeholder="Role" />
                  </SelectTrigger>
                  <SelectContent>
                    {(options?.state_roles || [
                      { id: 'contributor', label: 'Contributor' },
                      { id: 'reviewer', label: 'Reviewer' },
                    ]).map((r) => (
                      <SelectItem key={r.id} value={r.id}>
                        {r.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <p className="sm:col-span-2 text-[11px] text-muted-foreground">
                Contributor: upload + own docs. Reviewer: review/approve only (no upload).
                Group path:{' '}
                <code className="text-[10px]">
                  /states/{form.state}/{form.role}
                </code>
              </p>
            </div>
          ) : null}

          <div className="flex items-center justify-between rounded-lg border border-border px-3 py-2.5">
            <div>
              <p className="text-sm font-medium">Enabled</p>
              <p className="text-[11px] text-muted-foreground">User can sign in when on</p>
            </div>
            <Switch checked={form.enabled} onCheckedChange={(v) => update('enabled', v)} />
          </div>

          <Button type="submit" className="w-full sm:w-auto" disabled={submitting || !form.email.trim()}>
            {submitting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Creating in Keycloak…
              </>
            ) : (
              <>
                <UserPlus className="mr-2 h-4 w-4" />
                Create / update access
              </>
            )}
          </Button>

          {options?.notes?.length ? (
            <ul className="list-disc space-y-1 pl-4 text-[11px] text-muted-foreground">
              {options.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : null}
        </form>

        <div className="space-y-4 lg:col-span-2">
          <div className="panel p-5">
            <div className="mb-3 flex items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">Share with user</h2>
              <Button type="button" variant="outline" size="sm" onClick={copyShare}>
                <Copy className="mr-1.5 h-3.5 w-3.5" />
                {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>
            {result ? (
              <div className="mb-3 space-y-2">
                <div className="flex items-center gap-2 text-sm text-emerald-700 dark:text-emerald-400">
                  <CheckCircle2 className="h-4 w-4" />
                  {result.created ? 'User created' : 'User updated'} · {result.username}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  <Badge variant="outline" className="text-[10px]">
                    {result.access_type === 'super_admin'
                      ? 'Super Admin'
                      : `${result.state} · ${result.role}`}
                  </Badge>
                  <Badge variant="secondary" className="font-mono text-[10px]">
                    {result.group_path}
                  </Badge>
                </div>
              </div>
            ) : (
              <p className="mb-2 text-[11px] text-muted-foreground">
                Preview — final message includes app URLs after create.
              </p>
            )}
            <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-muted/40 p-3 text-[11px] leading-relaxed text-foreground">
              {result?.share_message || previewShare}
            </pre>
          </div>

          <div className="panel space-y-2 p-4 text-xs text-muted-foreground">
            <p className="font-semibold text-foreground">What to fill</p>
            <ul className="list-disc space-y-1 pl-4">
              <li>
                <strong>Email</strong> — Google account for SSO (required)
              </li>
              <li>
                <strong>Name</strong> — shown in the console
              </li>
              <li>
                <strong>Access type</strong> — Super Admin (all) or State
              </li>
              <li>
                <strong>State + role</strong> — only for state access (e.g. MH + contributor)
              </li>
            </ul>
            <p className="pt-1">
              After create, copy the share box and send it to the user (email / chat). They do not need a
              Keycloak password for SSO.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
