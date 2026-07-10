import React from 'react'
import { Card, CardContent } from './ui/card'

export function EmptyState({ icon: Icon, title, subtitle, className = '' }) {
  return (
    <Card className={className}>
      <CardContent className="flex flex-col items-center px-6 py-12 text-center">
        {Icon ? <Icon className="mb-3 h-8 w-8 text-muted-foreground/30" /> : null}
        <p className="text-sm font-medium text-foreground">{title}</p>
        {subtitle ? <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p> : null}
      </CardContent>
    </Card>
  )
}
