import React from 'react'
import { formatInstanceLabel, instanceBadgeTitle } from '../lib/instanceLabels'
import { Badge } from './ui/badge'
import { cn } from '../lib/utils'

/** Plan 2: show document state / BV portal tag from ``instance``. */
export function InstanceBadge({ instance, className }) {
  const label = formatInstanceLabel(instance)
  if (!label) return null
  const isPortal = label === 'BV'
  return (
    <Badge
      variant={isPortal ? 'default' : 'outline'}
      title={instanceBadgeTitle(instance)}
      className={cn('text-[10px] font-semibold tracking-wide', className)}
    >
      {label}
    </Badge>
  )
}

export default InstanceBadge
