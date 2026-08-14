import React from 'react'
import { AlertTriangle, CheckCircle } from 'lucide-react'

/**
 * Compact inline status banner (Taxonomy / Tenants / Indexes).
 * Distinct from NoticeCard, which is a titled card used in review surfaces.
 */
export function Notice({ tone = 'warning', children }) {
  const classes =
    tone === 'success'
      ? 'border-success/30 bg-success/10 text-success'
      : tone === 'error'
        ? 'border-destructive/30 bg-destructive/10 text-destructive'
        : 'border-warning/30 bg-warning/10 text-warning-foreground'
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${classes}`}>
      <div className="flex items-start gap-2">
        {tone === 'success' ? (
          <CheckCircle className="mt-0.5 h-4 w-4 shrink-0" />
        ) : (
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        )}
        <span>{children}</span>
      </div>
    </div>
  )
}
