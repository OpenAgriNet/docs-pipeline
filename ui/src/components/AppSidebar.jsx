import React, { useEffect, useState } from 'react'
import {
  ClipboardList,
  FileCode2,
  FileText,
  LayoutDashboard,
  ListTodo,
  Play,
  Search,
  Upload,
  Users,
} from 'lucide-react'
import { NavLink } from './NavLink'
import { useAuth } from '../auth/AuthProvider'
import { PlatformLogoIcon } from './PlatformLogoIcon'
import { APP_NAME } from '../lib/app-brand'
import { cn } from '../lib/utils'
import { fetchJson } from '../lib/pipelineUi'
import { Badge } from './ui/badge'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from './ui/sidebar'
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip'

const mainNav = [
  { title: 'Dashboard', url: '/', icon: LayoutDashboard, permission: 'search' },
  { title: 'Documents', url: '/documents', icon: FileText, permission: 'search' },
  { title: 'Queue', url: '/queue', icon: ListTodo, permission: 'search' },
  { title: 'Runs', url: '/runs', icon: Play, permission: 'search' },
]

const toolsNav = [
  { title: 'Search', url: '/search', icon: Search, permission: 'search' },
  { title: 'Chunks', url: '/chunks', icon: FileCode2, permission: 'search' },
  { title: 'Audit', url: '/audit', icon: ClipboardList, permission: 'search' },
]

const adminNav = [
  { title: 'Users', url: '/users', icon: Users, permission: 'manage_users' },
]

export function AppSidebar() {
  const { state } = useSidebar()
  const collapsed = state === 'collapsed'
  const { hasPermission } = useAuth()
  const canUpload = hasPermission('upload')
  const canSearch = hasPermission('search')
  const visible = (items) => items.filter((item) => !item.permission || hasPermission(item.permission))

  const [queueTotal, setQueueTotal] = useState(0)

  useEffect(() => {
    if (!canSearch) return
    let cancelled = false
    async function loadQueueTotal() {
      try {
        const data = await fetchJson('/operations/queue?limit=1')
        if (!cancelled) setQueueTotal(data.total || 0)
      } catch {
        // Sidebar badge is best-effort; ignore transient failures.
      }
    }
    loadQueueTotal()
    const interval = setInterval(loadQueueTotal, 30000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [canSearch])

  const renderGroup = (label, allItems) => {
    const items = visible(allItems)
    if (!items.length) return null

    return (
      <SidebarGroup className="p-0">
        {!collapsed && (
          <SidebarGroupLabel className="px-3.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            {label}
          </SidebarGroupLabel>
        )}
        <SidebarGroupContent>
          <SidebarMenu className="gap-1">
            {items.map((item) => {
              const Icon = item.icon
              const badgeCount = item.title === 'Queue' ? queueTotal : 0
              const link = (
                <NavLink
                  to={item.url}
                  end={item.url === '/'}
                  className={cn(
                    'relative flex items-center gap-3 rounded-xl text-sm font-medium',
                    'text-sidebar-foreground transition-colors',
                    collapsed
                      ? 'size-10 mx-auto justify-center hover:bg-sidebar-accent hover:text-sidebar-accent-foreground'
                      : 'px-3.5 py-2.5 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
                  )}
                  activeClassName={
                    collapsed
                      ? 'bg-primary text-primary-foreground hover:bg-primary hover:text-primary-foreground'
                      : 'bg-sidebar-accent text-sidebar-foreground font-semibold hover:bg-sidebar-accent hover:text-sidebar-foreground'
                  }
                >
                  {({ isActive }) => (
                    <>
                      {isActive && !collapsed && (
                        <span className="absolute left-0 top-1/2 h-5 w-1 -translate-y-1/2 rounded-full bg-primary" />
                      )}
                      <span className="relative inline-flex shrink-0">
                        <Icon
                          className={cn('size-[19px]', isActive && collapsed ? 'opacity-100' : 'opacity-80')}
                          strokeWidth={1.75}
                        />
                        {collapsed && badgeCount > 0 && (
                          <span className="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-warning ring-2 ring-sidebar" />
                        )}
                      </span>
                      {!collapsed && <span className="flex-1 truncate">{item.title}</span>}
                      {!collapsed && badgeCount > 0 && (
                        <Badge variant="warning" className="shrink-0 px-1.5 py-0 text-[11px]">
                          {badgeCount}
                        </Badge>
                      )}
                    </>
                  )}
                </NavLink>
              )

              return (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild className="h-auto p-0 hover:bg-transparent">
                    {collapsed ? (
                      <Tooltip delayDuration={0}>
                        <TooltipTrigger asChild>
                          <div className="w-full">{link}</div>
                        </TooltipTrigger>
                        <TooltipContent side="right">{item.title}</TooltipContent>
                      </Tooltip>
                    ) : (
                      link
                    )}
                  </SidebarMenuButton>
                </SidebarMenuItem>
              )
            })}
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    )
  }

  return (
    <Sidebar collapsible="icon" className="border-r border-sidebar-border bg-sidebar">
      <SidebarHeader className="px-5 pt-6 pb-4">
        {!collapsed ? (
          <div className="flex items-center gap-3">
            <PlatformLogoIcon className="size-10 rounded-lg" title={APP_NAME} />
            <div className="min-w-0 leading-tight">
              <div className="truncate text-base font-semibold text-sidebar-foreground">{APP_NAME}</div>
              <div className="text-xs text-muted-foreground">Operator Console</div>
            </div>
          </div>
        ) : (
          <div className="flex justify-center">
            <PlatformLogoIcon className="size-9 rounded-lg" title={APP_NAME} />
          </div>
        )}
      </SidebarHeader>

      <SidebarContent className="px-0 pt-2 gap-4">
        {renderGroup('Operations', mainNav)}
        {renderGroup('Tools', toolsNav)}
        {renderGroup('Admin', adminNav)}
      </SidebarContent>

      <SidebarFooter className="p-3">
        {canUpload &&
          (collapsed ? (
            <div className="flex justify-center">
              <Tooltip delayDuration={0}>
                <TooltipTrigger asChild>
                  <NavLink
                    to="/ingest"
                    className="flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground hover:bg-primary/90"
                  >
                    <Upload className="size-4" />
                    <span className="sr-only">New Document</span>
                  </NavLink>
                </TooltipTrigger>
                <TooltipContent side="right">New Document</TooltipContent>
              </Tooltip>
            </div>
          ) : (
            <NavLink
              to="/ingest"
              className={cn(
                'flex w-full items-center justify-center gap-2 rounded-xl px-3 py-3',
                'text-sm font-semibold text-primary-foreground shadow-sm',
                'bg-primary hover:bg-primary/90 hover:shadow-md transition-all',
              )}
            >
              <Upload className="size-4.5" strokeWidth={2} />
              New Document
            </NavLink>
          ))}
      </SidebarFooter>
    </Sidebar>
  )
}
