import React from 'react'
import {
  Building2,
  ClipboardList,
  Database,
  FileCode2,
  FileText,
  LayoutDashboard,
  ListTodo,
  Play,
  Search,
  Upload,
} from 'lucide-react'
import { NavLink } from './NavLink'
import { useAuth } from '../auth/AuthProvider'
import { InstanceSwitcher } from './InstanceSwitcher'
import { PlatformLogoIcon } from './PlatformLogoIcon'
import { APP_NAME } from '../lib/app-brand'
import { cn } from '../lib/utils'
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
  { title: 'Indexes', url: '/indexes', icon: Database, permission: 'search' },
  { title: 'Search', url: '/search', icon: Search, permission: 'search' },
  { title: 'Chunks', url: '/chunks', icon: FileCode2, permission: 'search' },
  { title: 'Audit', url: '/audit', icon: ClipboardList, permission: 'search' },
]

// Control-plane. Visible only to platform admins; never gated on a data permission.
const adminNav = [
  { title: 'Tenants', url: '/tenants', icon: Building2, platformAdmin: true },
]

export function AppSidebar() {
  const { state } = useSidebar()
  const collapsed = state === 'collapsed'
  const { hasPermission, isPlatformAdmin } = useAuth()
  const canUpload = hasPermission('upload')
  const visible = (items) =>
    items.filter(
      (item) =>
        (!item.permission || hasPermission(item.permission)) &&
        (!item.platformAdmin || isPlatformAdmin),
    )

  const renderGroup = (label, allItems) => {
    const items = visible(allItems)
    if (!items.length) return null

    return (
      <SidebarGroup>
        {!collapsed && (
          <SidebarGroupLabel className="px-3 text-[11px] font-medium text-muted-foreground">
            {label}
          </SidebarGroupLabel>
        )}
        <SidebarGroupContent>
          <SidebarMenu className="gap-0.5">
            {items.map((item) => {
              const Icon = item.icon
              const link = (
                <NavLink
                  to={item.url}
                  end={item.url === '/'}
                  className={cn(
                    'flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium',
                    'text-sidebar-foreground transition-colors',
                    'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
                    collapsed && 'justify-center px-2',
                  )}
                  activeClassName="bg-sidebar-accent text-primary hover:bg-sidebar-accent hover:text-primary"
                >
                  <Icon className="size-[18px] shrink-0 opacity-80" strokeWidth={1.75} />
                  {!collapsed && <span>{item.title}</span>}
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
      <SidebarHeader className="px-4 pt-5 pb-3">
        {!collapsed ? (
          <div className="flex items-center gap-2.5">
            <PlatformLogoIcon className="size-9 rounded-lg" title={APP_NAME} />
            <div className="min-w-0 leading-tight">
              <div className="truncate text-sm font-semibold text-sidebar-foreground">{APP_NAME}</div>
              <div className="text-[11px] text-muted-foreground">Operator Console</div>
            </div>
          </div>
        ) : (
          <div className="flex justify-center">
            <PlatformLogoIcon className="size-8 rounded-lg" title={APP_NAME} />
          </div>
        )}
      </SidebarHeader>

      <SidebarContent className="px-2 pt-1">
        {!collapsed && <InstanceSwitcher collapsed={collapsed} />}
        {renderGroup('Operations', mainNav)}
        {renderGroup('Tools', toolsNav)}
        {renderGroup('Administration', adminNav)}
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
                'flex w-full items-center justify-center gap-2 rounded-lg px-3 py-2.5',
                'text-sm font-medium text-primary-foreground',
                'bg-primary hover:bg-primary/90 transition-colors',
              )}
            >
              <Upload className="size-4" />
              New Document
            </NavLink>
          ))}
      </SidebarFooter>
    </Sidebar>
  )
}
