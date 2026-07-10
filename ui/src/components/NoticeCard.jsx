import React from 'react'
import { CheckCircle2, CircleAlert } from 'lucide-react'
import { cn } from '../lib/utils'
import { Card, CardContent } from './ui/card'

const toneClassMap = {
  default: 'border-border bg-card',
  warning: 'border-warning/30 bg-warning/10 text-warning',
  destructive: 'border-destructive/30 bg-destructive/5 text-destructive',
  success: 'border-success/30 bg-success/10 text-success',
}

export function NoticeCard({ title, detail, tone = 'default', icon: Icon = CircleAlert, className }) {
  const ResolvedIcon = Icon || (tone === 'success' ? CheckCircle2 : CircleAlert)

  return (
    <Card className={cn('shadow-none', toneClassMap[tone] || toneClassMap.default, className)}>
      <CardContent className="flex items-start gap-3 p-4">
        <ResolvedIcon className={cn('mt-0.5 h-4 w-4 shrink-0', tone === 'default' ? 'text-muted-foreground' : '')} />
        <div className="min-w-0">
          <p className={cn('text-sm font-medium', tone === 'default' ? 'text-foreground' : '')}>{title}</p>
          {detail ? <p className={cn('mt-1 text-xs leading-5', tone === 'default' ? 'text-muted-foreground' : 'text-current/80')}>{detail}</p> : null}
        </div>
      </CardContent>
    </Card>
  )
}
