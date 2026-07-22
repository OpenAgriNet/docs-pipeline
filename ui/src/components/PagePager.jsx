import React, { useEffect, useRef } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from './ui/button'
import { cn } from '../lib/utils'

/**
 * Horizontal page strip for long documents.
 * - Prev / next controls
 * - Scrollable chips (does not wrap the whole page)
 * - Active page stays in view
 */
export default function PagePager({
  pages = [],
  currentPage,
  onChange,
  getStatus, // (page) => 'active' | 'done' | 'pending' | 'accent'
  className,
  label = 'Pages',
}) {
  const stripRef = useRef(null)
  const activeRef = useRef(null)
  const total = pages.length
  const safeCurrent = Math.min(Math.max(1, currentPage || 1), Math.max(1, total))

  useEffect(() => {
    if (!activeRef.current || !stripRef.current) return
    activeRef.current.scrollIntoView({
      behavior: 'smooth',
      block: 'nearest',
      inline: 'center',
    })
  }, [safeCurrent, total])

  if (!total) return null

  function statusClass(status) {
    switch (status) {
      case 'active':
        return 'bg-primary text-primary-foreground shadow-sm ring-2 ring-primary/25'
      case 'done':
        return 'bg-success/15 text-success hover:bg-success/25'
      case 'accent':
        return 'bg-primary/10 text-primary hover:bg-primary/20'
      default:
        return 'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground'
    }
  }

  return (
    <div
      className={cn(
        'flex items-center gap-2 rounded-lg border border-border bg-card/90 px-2 py-1.5 shadow-sm backdrop-blur-sm',
        className,
      )}
    >
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="h-8 w-8 shrink-0"
        disabled={safeCurrent <= 1}
        onClick={() => onChange(Math.max(1, safeCurrent - 1))}
        aria-label="Previous page"
      >
        <ChevronLeft className="h-4 w-4" />
      </Button>

      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center justify-between px-0.5">
          <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {label}
          </span>
          <span className="text-[11px] tabular-nums text-muted-foreground">
            <span className="font-medium text-foreground">{safeCurrent}</span>
            <span className="mx-0.5">/</span>
            {total}
          </span>
        </div>
        <div
          ref={stripRef}
          className="flex max-w-full gap-1 overflow-x-auto overscroll-x-contain pb-0.5 [-ms-overflow-style:none] [scrollbar-width:thin]"
        >
          {pages.map((page) => {
            const pageNumber = page.page_number ?? page
            const isActive = pageNumber === safeCurrent
            const status = isActive
              ? 'active'
              : (typeof getStatus === 'function' ? getStatus(page) : 'pending')
            return (
              <button
                key={pageNumber}
                ref={isActive ? activeRef : null}
                type="button"
                onClick={() => onChange(pageNumber)}
                className={cn(
                  'flex h-8 min-w-8 shrink-0 items-center justify-center rounded-md px-2 text-xs font-medium transition-colors',
                  statusClass(status),
                )}
                aria-current={isActive ? 'page' : undefined}
                aria-label={`Go to page ${pageNumber}`}
              >
                {pageNumber}
              </button>
            )
          })}
        </div>
      </div>

      <Button
        type="button"
        variant="outline"
        size="icon"
        className="h-8 w-8 shrink-0"
        disabled={safeCurrent >= total}
        onClick={() => onChange(Math.min(total, safeCurrent + 1))}
        aria-label="Next page"
      >
        <ChevronRight className="h-4 w-4" />
      </Button>
    </div>
  )
}
