import React from 'react'
import { Building2 } from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select'

/**
 * Tenant instance switcher for the sidebar.
 *
 * Additive by design: renders nothing when the caller has 0 or 1 instance, so the
 * single-tenant / legacy console looks exactly as it does today. With >1 instance
 * it lets the caller pick the acting tenant; the selection flows through
 * AuthProvider → activeInstance module → fetchJson (`?instance=`) on every call.
 */
export function InstanceSwitcher({ collapsed = false }) {
  const { instances, activeInstance, setActiveInstance } = useAuth()

  // Nothing to switch between → stay invisible (non-disruptive).
  if (!Array.isArray(instances) || instances.length <= 1) return null
  // Icon-collapsed sidebar has no room for a select; the switcher reappears when expanded.
  if (collapsed) return null

  return (
    <div className="px-1 pb-2">
      <label className="mb-1 flex items-center gap-1.5 px-1 text-[11px] font-medium text-muted-foreground">
        <Building2 className="size-3.5 opacity-80" strokeWidth={1.75} />
        Tenant
      </label>
      <Select value={activeInstance || undefined} onValueChange={setActiveInstance}>
        <SelectTrigger className="h-9 w-full bg-background text-[13px]">
          <SelectValue placeholder="Select tenant" />
        </SelectTrigger>
        <SelectContent>
          {instances.map((instance) => (
            <SelectItem key={instance} value={instance} className="text-[13px]">
              {instance}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

export default InstanceSwitcher
