import React from 'react'
import { Building2, Check, LogOut, Shield, X as XIcon } from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'
import {
  formatStateList,
  primaryProductRole,
  roleLabel,
  rolesToDisplay,
} from '../lib/roleCapabilities'
import { cn } from '../lib/utils'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from './ui/sheet'

function initialsFrom(name, email) {
  const source = (name || email || '?').trim()
  const parts = source.split(/\s+/).filter(Boolean)
  if (parts.length >= 2) {
    return `${parts[0][0] || ''}${parts[1][0] || ''}`.toUpperCase()
  }
  return source.slice(0, 2).toUpperCase()
}

function RoleCard({ role, highlight }) {
  return (
    <section
      className={cn(
        'rounded-xl border bg-card p-4 shadow-sm',
        highlight ? 'border-primary/40 ring-1 ring-primary/15' : 'border-border',
      )}
    >
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-foreground">{role.label}</h3>
          <p className="text-[11px] text-muted-foreground">{role.shortLabel}</p>
        </div>
        <Badge variant={highlight ? 'default' : 'secondary'} className="text-[10px] font-medium">
          {role.scope}
        </Badge>
      </div>
      <p className="mb-3 text-xs leading-relaxed text-muted-foreground">{role.summary}</p>
      <ul className="space-y-1.5">
        {role.capabilities.map((cap) => (
          <li key={cap.id} className="flex items-start gap-2 text-xs">
            <span
              className={cn(
                'mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full',
                cap.allowed
                  ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400'
                  : 'bg-muted text-muted-foreground/70',
              )}
              aria-hidden
            >
              {cap.allowed ? (
                <Check className="size-2.5" strokeWidth={3} />
              ) : (
                <XIcon className="size-2.5" strokeWidth={2.5} />
              )}
            </span>
            <span
              className={cn(
                'leading-snug',
                cap.allowed ? 'text-foreground' : 'text-muted-foreground line-through decoration-muted-foreground/40',
              )}
            >
              {cap.label}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * Right-side overlay: identity + role access matrix.
 * Super admin sees all three roles; others only the roles they hold.
 */
export function UserAccessSheet({ open, onOpenChange }) {
  const {
    displayName,
    email,
    roles,
    permissions,
    instances,
    groups,
    stateRoles,
    isSuperAdmin,
    logout,
  } = useAuth()

  const catalog = rolesToDisplay({ isSuperAdmin, roles, stateRoles })
  const primary = primaryProductRole({ isSuperAdmin, roles, stateRoles })
  const states = formatStateList(instances, stateRoles)
  const titleName = displayName || email || 'Signed-in user'

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 overflow-y-auto p-0 sm:max-w-md"
      >
        <SheetHeader className="space-y-3 border-b border-border px-5 pb-4 pt-5 text-left">
          <div className="flex items-start gap-3 pr-8">
            <div
              className={cn(
                'flex size-11 shrink-0 items-center justify-center rounded-full',
                'bg-primary/15 text-sm font-semibold tracking-wide text-primary',
                'ring-1 ring-primary/15',
              )}
            >
              {initialsFrom(displayName, email)}
            </div>
            <div className="min-w-0 flex-1">
              <SheetTitle className="truncate text-base">{titleName}</SheetTitle>
              {email ? (
                <SheetDescription className="truncate text-xs">{email}</SheetDescription>
              ) : (
                <SheetDescription className="sr-only">Account access details</SheetDescription>
              )}
              <div className="mt-2 flex flex-wrap gap-1.5">
                <Badge className="gap-1 text-[10px]">
                  <Shield className="size-3" />
                  {roleLabel(primary)}
                </Badge>
                {isSuperAdmin ? (
                  <Badge variant="outline" className="text-[10px]">
                    All states
                  </Badge>
                ) : states.length > 0 ? (
                  <Badge variant="outline" className="gap-1 text-[10px]">
                    <Building2 className="size-3" />
                    {states.join(', ')}
                  </Badge>
                ) : null}
              </div>
            </div>
          </div>
        </SheetHeader>

        <div className="flex flex-1 flex-col gap-4 px-5 py-4">
          <div>
            <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {isSuperAdmin ? 'Platform roles (full catalog)' : 'Your access'}
            </h2>
            <p className="mb-3 text-xs text-muted-foreground">
              {isSuperAdmin
                ? 'As Super Admin you have full access. Below is what each role includes on the platform.'
                : 'Capabilities for the role(s) assigned to your account in Keycloak.'}
            </p>
            <div className="space-y-3">
              {catalog.length === 0 ? (
                <p className="rounded-lg border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground">
                  No product role mapped yet. Ask an admin to assign a Keycloak group
                  (e.g. /states/MH/contributor).
                </p>
              ) : (
                catalog.map((role) => (
                  <RoleCard
                    key={role.id}
                    role={role}
                    highlight={role.id === primary || (isSuperAdmin && role.id === 'super_admin')}
                  />
                ))
              )}
            </div>
          </div>

          {(states.length > 0 || (groups && groups.length > 0) || (permissions && permissions.length > 0)) && (
            <div className="space-y-3 rounded-xl border border-border bg-muted/30 p-3">
              <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Session details
              </h2>
              {states.length > 0 && !isSuperAdmin ? (
                <div>
                  <div className="text-[11px] font-medium text-muted-foreground">States</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {states.map((code) => (
                      <Badge key={code} variant="secondary" className="text-[10px]">
                        {code}
                        {stateRoles?.[code.toLowerCase()]
                          ? ` · ${roleLabel(stateRoles[code.toLowerCase()])}`
                          : ''}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}
              {groups?.length ? (
                <div>
                  <div className="text-[11px] font-medium text-muted-foreground">Keycloak groups</div>
                  <ul className="mt-1 space-y-0.5 font-mono text-[10px] text-foreground/80">
                    {groups.map((g) => (
                      <li key={g} className="truncate">
                        {g}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {permissions?.length ? (
                <div>
                  <div className="text-[11px] font-medium text-muted-foreground">API permissions</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {[...permissions].sort().map((p) => (
                      <Badge key={p} variant="outline" className="font-mono text-[10px]">
                        {p}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>

        <div className="mt-auto border-t border-border px-5 py-4">
          <Button
            type="button"
            variant="outline"
            className="w-full gap-2"
            onClick={() => {
              onOpenChange(false)
              void logout()
            }}
          >
            <LogOut className="size-3.5" />
            Sign out
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

export default UserAccessSheet
