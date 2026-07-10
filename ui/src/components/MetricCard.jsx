import React from 'react'
import { cn } from '../lib/utils'
import { Card, CardContent } from './ui/card'

const toneClassMap = {
  default: 'border-border bg-background/80',
  warning: 'border-warning/20 bg-warning/10 text-warning',
  success: 'border-success/20 bg-success/10 text-success',
}

export function MetricCard({ label, value, tone = 'default', className }) {
  return (
    <Card className={cn('rounded-2xl shadow-none', toneClassMap[tone] || toneClassMap.default, className)}>
      <CardContent className="px-3 py-2">
        <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">{label}</div>
        <div className={cn('mt-0.5 text-sm font-medium', tone === 'default' ? 'text-foreground' : '')}>{value}</div>
      </CardContent>
    </Card>
  )
}
