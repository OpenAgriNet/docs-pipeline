import React from 'react'
import { Badge } from './ui/badge'

export function DocumentSectionHeader({ title, description, badge }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div>
        <p className="text-sm font-medium text-foreground">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      </div>
      {badge ? <Badge variant="secondary">{badge}</Badge> : null}
    </div>
  )
}
