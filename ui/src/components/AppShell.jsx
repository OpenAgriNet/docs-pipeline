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
        'sticky top-0 z-30 flex h-14 shrink-0 items-center justify-between gap-3',
        'border-b border-[#d5e0db]/90 bg-white/90 px-4 backdrop-blur-md',
        'supports-[backdrop-filter]:bg-white/75',
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        <button
          type="button"
          onClick={toggleSidebar}
          className={cn(
            'inline-flex size-9 shrink-0 items-center justify-center rounded-lg',
            'border border-transparent text-[#5f7269]',
            'transition-colors hover:border-[#d5e0db] hover:bg-[#f7faf8] hover:text-[#14201b]',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#059669]/25',
          )}
          aria-label="Toggle sidebar"
        >
          <PanelLeft className="size-4" strokeWidth={1.75} />
        </button>
      </div>

      <div className="flex items-center gap-2 sm:gap-3">
        {authEnabled && (displayName || email) ? (
          <div
            className={cn(
              'hidden sm:flex items-center gap-2.5 rounded-full border border-[#d5e0db] bg-[#f7faf8]/90',
              'py-1 pl-1 pr-3 shadow-sm',
            )}
          >
            <div
              className={cn(
                'flex size-8 shrink-0 items-center justify-center rounded-full',
                'bg-[#059669]/15 text-[11px] font-semibold tracking-wide text-[#047857]',
                'ring-1 ring-[#059669]/10',
              )}
              aria-hidden
            >
              {initialsFrom(displayName, email)}
            </div>
            <div className="min-w-0 leading-tight">
              <div className="max-w-[150px] truncate text-xs font-semibold text-[#14201b]">
                {displayName || email}
              </div>
              {email && displayName && email !== displayName ? (
                <div className="max-w-[170px] truncate text-[11px] text-[#5f7269]">
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
              'inline-flex items-center gap-1.5 rounded-full border border-[#d5e0db] bg-white',
              'px-3 py-1.5 text-xs font-medium text-[#14201b]',
              'shadow-sm transition-colors',
              'hover:border-[#c8d6cf] hover:bg-[#f7faf8]',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#059669]/25',
            )}
          >
            <LogOut className="size-3.5 text-[#5f7269]" strokeWidth={1.75} />
            Logout
          </button>
        ) : null}

        <div className="flex size-9 items-center justify-center rounded-lg border border-[#d5e0db] bg-white shadow-sm">
          <ThemeSwitcher />
        </div>
      </div>
    </header>
  )
}

export default function AppShell({ children }) {
  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-[#f7faf8]">
        <AppSidebar />
        <SidebarInset className="flex min-w-0 flex-1 flex-col bg-[#f7faf8]">
          <AppHeader />
          <main className="min-h-0 min-w-0 flex-1 overflow-auto bg-white">
            {children}
          </main>
        </SidebarInset>
      </div>
    </SidebarProvider>
  )
}
