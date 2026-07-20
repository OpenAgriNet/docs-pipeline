import React from 'react'
import { LogOut } from 'lucide-react'
import { AppSidebar } from './AppSidebar'
import { ThemeSwitcher } from './ThemeSwitcher'
import { useAuth } from '../auth/AuthProvider'
import { SidebarInset, SidebarProvider, SidebarTrigger } from './ui/sidebar'

export default function AppShell({ children }) {
  const { authEnabled, username, logout } = useAuth()
  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full">
        <AppSidebar />
        <SidebarInset className="min-w-0">
          <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-card px-4">
            <SidebarTrigger className="mr-3" />
            <div className="flex items-center gap-3">
              {authEnabled && (
                <>
                  {username && (
                    <span className="text-xs text-muted-foreground">{username}</span>
                  )}
                  <button
                    type="button"
                    onClick={logout}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-foreground hover:bg-accent transition-colors"
                  >
                    <LogOut className="h-3.5 w-3.5" />
                    Logout
                  </button>
                </>
              )}
              <ThemeSwitcher />
            </div>
          </header>
          <main className="flex-1 min-w-0 overflow-auto">
            {children}
          </main>
        </SidebarInset>
      </div>
    </SidebarProvider>
  )
}
