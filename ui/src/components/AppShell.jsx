import React, { useState } from 'react'
import { PanelLeft } from 'lucide-react'
import { AppSidebar } from './AppSidebar'
import { ThemeSwitcher } from './ThemeSwitcher'
import { UserAccessSheet } from './UserAccessSheet'
import { useAuth } from '../auth/AuthProvider'
import { primaryProductRole, roleLabel } from '../lib/roleCapabilities'
import { useSidebar, SidebarInset, SidebarProvider } from './ui/sidebar'
import { cn } from '../lib/utils'

function initialsFrom(name, email) {
  const source = (name || email || '?').trim()
  const parts = source.split(/\s+/).filter(Boolean)
  if (parts.length >= 2) {
    return `${parts[0][0] || ''}${parts[1][0] || ''}`.toUpperCase()
  }
  return source.slice(0, 2).toUpperCase()
}

function AppHeader() {
  const { toggleSidebar } = useSidebar()
  const {
    authEnabled,
    displayName,
    email,
    roles,
    stateRoles,
    isSuperAdmin,
  } = useAuth()
  const [accessOpen, setAccessOpen] = useState(false)

  const primary = primaryProductRole({ isSuperAdmin, roles, stateRoles })
  const roleText = roleLabel(primary)

  return (
    <>
      <header
        className={cn(
          'sticky top-0 z-30 flex h-14 shrink-0 items-center justify-between gap-3 px-4',
          'border-b border-border/90 bg-card/90 backdrop-blur-md',
          'supports-[backdrop-filter]:bg-card/75',
        )}
      >
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            onClick={toggleSidebar}
            className={cn(
              'inline-flex size-9 shrink-0 items-center justify-center rounded-lg',
              'border border-transparent text-muted-foreground',
              'transition-colors hover:border-border hover:bg-muted hover:text-foreground',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
            )}
            aria-label="Toggle sidebar"
          >
            <PanelLeft className="size-4" strokeWidth={1.75} />
          </button>
        </div>

        <div className="flex items-center gap-2 sm:gap-2.5">
          {authEnabled && (displayName || email) ? (
            <button
              type="button"
              onClick={() => setAccessOpen(true)}
              title="View access & roles"
              aria-label="Open access and role details"
              className={cn(
                'flex items-center gap-2.5 rounded-full border border-border bg-muted/60',
                'py-1 pl-1 pr-3 shadow-sm transition-colors',
                'hover:border-primary/30 hover:bg-primary/5',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
              )}
            >
              <div
                className={cn(
                  'flex size-8 shrink-0 items-center justify-center rounded-full',
                  'bg-primary/15 text-[11px] font-semibold tracking-wide text-primary',
                  'ring-1 ring-primary/10',
                )}
                aria-hidden
              >
                {initialsFrom(displayName, email)}
              </div>
              <div className="hidden min-w-0 leading-tight sm:block text-left">
                <div className="max-w-[150px] truncate text-xs font-semibold text-foreground">
                  {displayName || email}
                </div>
                <div className="max-w-[170px] truncate text-[11px] text-muted-foreground">
                  {roleText}
                  {email && displayName && email !== displayName ? ` · ${email}` : ''}
                </div>
              </div>
            </button>
          ) : null}

          <ThemeSwitcher />
        </div>
      </header>

      {authEnabled ? (
        <UserAccessSheet open={accessOpen} onOpenChange={setAccessOpen} />
      ) : null}
    </>
  )
}

export default function AppShell({ children }) {
  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-background">
        <AppSidebar />
        <SidebarInset className="flex min-w-0 flex-1 flex-col bg-background">
          <AppHeader />
          <main className="min-h-0 min-w-0 flex-1 overflow-auto bg-card">
            {children}
          </main>
        </SidebarInset>
      </div>
    </SidebarProvider>
  )
}
