import React from 'react'
import { Building2, ClipboardList, Database, FileCode2, FileText, LayoutDashboard, ListTodo, Play, Search, Settings, Upload } from 'lucide-react'
import { NavLink } from './NavLink'
import { useAuth } from '../auth/AuthProvider'
import { Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader, SidebarMenu, SidebarMenuButton, SidebarMenuItem, useSidebar } from './ui/sidebar'

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

const systemNav = [
  { title: 'Settings', url: '/settings', icon: Settings, permission: 'admin' },
  // Super-admin only: creating tenants and tenant admins is gated on the
  // realm-level `master_admin` role, never a per-tenant permission.
  { title: 'Tenants', url: '/tenants', icon: Building2, role: 'master_admin' },
]

export function AppSidebar() {
  const { state } = useSidebar()
  const collapsed = state === 'collapsed'
  const { hasPermission, hasRole } = useAuth()
  const canUpload = hasPermission('upload')
  const visible = items => items.filter(item =>
    (!item.permission || hasPermission(item.permission)) &&
    (!item.role || hasRole(item.role))
  )

  const renderGroup = (label, allItems) => {
    const items = visible(allItems)
    if (!items.length) return null
    return (
    <SidebarGroup>
      <SidebarGroupLabel className="text-[10px] uppercase tracking-widest text-muted-foreground/70 font-medium">
        {label}
      </SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map(item => (
            <SidebarMenuItem key={item.title}>
              <SidebarMenuButton asChild>
                <NavLink
                  to={item.url}
                  end={item.url === '/'}
                  className="flex items-center gap-3 px-3 py-2 rounded-md text-sm text-sidebar-foreground hover:bg-sidebar-accent transition-colors"
                  activeClassName="bg-sidebar-accent text-primary font-medium"
                >
                  <item.icon className="h-4 w-4 shrink-0" />
                  {!collapsed && <span>{item.title}</span>}
                </NavLink>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
    )
  }

  return (
    <Sidebar collapsible="icon" className="border-r border-sidebar-border">
      <SidebarHeader className="px-4 py-4">
        {!collapsed ? (
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-md bg-primary flex items-center justify-center">
              <FileText className="h-4 w-4 text-primary-foreground" />
            </div>
            <div>
              <h1 className="text-sm font-semibold text-foreground leading-none">DocPipeline</h1>
              <p className="text-[10px] text-muted-foreground mt-0.5">Operator Console</p>
            </div>
          </div>
        ) : (
          <div className="w-7 h-7 rounded-md bg-primary flex items-center justify-center mx-auto">
            <FileText className="h-4 w-4 text-primary-foreground" />
          </div>
        )}
      </SidebarHeader>

      <SidebarContent className="px-2">
        {renderGroup('Operations', mainNav)}
        {renderGroup('Tools', toolsNav)}
        {renderGroup('System', systemNav)}
      </SidebarContent>

      <SidebarFooter className="px-4 py-3">
        {!collapsed && canUpload && (
          <NavLink
            to="/ingest"
            className="flex items-center gap-2 px-3 py-2.5 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90 transition-colors justify-center"
            activeClassName="bg-primary/80"
          >
            <Upload className="h-4 w-4" />
            New Document
          </NavLink>
        )}
      </SidebarFooter>
    </Sidebar>
  )
}
