import React from 'react'
import { cn } from '../lib/utils'

const variantStyles = {
  default: '',
  warning: 'border-warning/30 bg-warning/5',
  danger: 'border-destructive/30 bg-destructive/5',
  success: 'border-success/30 bg-success/5',
}

export function StatCard({ label, value, subtitle, icon, variant = 'default', className, onClick }) {
  return (
    <div
      className={cn('stat-card', variantStyles[variant], onClick && 'cursor-pointer', className)}
      onClick={onClick}
    >
      <div className="flex items-start justify-between mb-2">
        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">{label}</span>
        {icon && <span className="text-muted-foreground">{icon}</span>}
      </div>
      <div className="text-2xl font-semibold font-serif text-foreground">{value}</div>
      {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
    </div>
  )
}
