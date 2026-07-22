import React from 'react'
import { LogOut, PanelLeft } from 'lucide-react'
import { AppSidebar } from './AppSidebar'
import { ThemeSwitcher } from './ThemeSwitcher'
import { useAuth } from '../auth/AuthProvider'
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
  const { authEnabled, displayName, email, logout } = useAuth()

  return (
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
          <div
            className={cn(
              'hidden sm:flex items-center gap-2.5 rounded-full border border-border bg-muted/60',
              'py-1 pl-1 pr-3 shadow-sm',
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
            <div className="min-w-0 leading-tight">
              <div className="max-w-[150px] truncate text-xs font-semibold text-foreground">
                {displayName || email}
              </div>
              {email && displayName && email !== displayName ? (
                <div className="max-w-[170px] truncate text-[11px] text-muted-foreground">
                  {email}
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        {authEnabled ? (
          <button
            type="button"
            onClick={() => void logout()}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border border-border bg-card',
              'px-3 py-1.5 text-xs font-medium text-foreground',
              'shadow-sm transition-colors',
              'hover:bg-muted',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
            )}
          >
            <LogOut className="size-3.5 text-muted-foreground" strokeWidth={1.75} />
            Logout
          </button>
        ) : null}

        <ThemeSwitcher />
      </div>
    </header>
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
