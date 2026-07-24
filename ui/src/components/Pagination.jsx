import { ChevronLeft, ChevronRight, MoreHorizontal } from 'lucide-react'

import { Button } from './ui/button'
import { cn } from '../lib/utils'

/**
 * Build a windowed list of page items for a paginator.
 *
 * Always includes the first and last page, a window of `siblingCount` pages on
 * either side of the current page, and `'ellipsis'` markers for any gaps. The
 * number of rendered items is bounded by a small constant regardless of how
 * many pages exist, so the control can never overflow horizontally.
 *
 * @param {number} currentPage 1-based current page
 * @param {number} totalPages total number of pages (>= 1)
 * @param {number} siblingCount pages to show on each side of the current page
 * @returns {Array<number|string>} e.g. [1, 'ellipsis-left', 99, 100, 101, 'ellipsis-right', 200]
 */
export function getPaginationRange(currentPage, totalPages, siblingCount = 1) {
  // first + last + current + 2 ellipsis slots + siblings on both sides
  const totalNumbers = siblingCount * 2 + 5

  if (totalPages <= totalNumbers) {
    return Array.from({ length: totalPages }, (_, i) => i + 1)
  }

  const leftSibling = Math.max(currentPage - siblingCount, 1)
  const rightSibling = Math.min(currentPage + siblingCount, totalPages)

  const showLeftEllipsis = leftSibling > 2
  const showRightEllipsis = rightSibling < totalPages - 1

  const items = [1]

  if (showLeftEllipsis) {
    items.push('ellipsis-left')
  } else {
    for (let page = 2; page < leftSibling; page += 1) items.push(page)
  }

  for (let page = leftSibling; page <= rightSibling; page += 1) {
    if (page !== 1 && page !== totalPages) items.push(page)
  }

  if (showRightEllipsis) {
    items.push('ellipsis-right')
  } else {
    for (let page = rightSibling + 1; page < totalPages; page += 1) items.push(page)
  }

  items.push(totalPages)

  return items
}

/**
 * Windowed paginator with prev/next controls.
 *
 * Renders a bounded number of page buttons (first, last, current +/- siblings,
 * with ellipses) so long page counts never overflow their container. The
 * container also wraps as a belt-and-suspenders guard against overflow.
 */
export function Pagination({ currentPage, totalPages, onPageChange, siblingCount = 1, className }) {
  if (!totalPages || totalPages <= 1) return null

  const items = getPaginationRange(currentPage, totalPages, siblingCount)

  return (
    <div className={cn('flex flex-wrap items-center gap-1', className)}>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        disabled={currentPage <= 1}
        onClick={() => onPageChange(currentPage - 1)}
        aria-label="Previous page"
      >
        <ChevronLeft className="h-3.5 w-3.5" />
      </Button>

      {items.map(item => {
        if (typeof item === 'string') {
          return (
            <span key={item} className="flex h-7 w-7 items-center justify-center text-muted-foreground" aria-hidden="true">
              <MoreHorizontal className="h-3.5 w-3.5" />
            </span>
          )
        }

        const isActive = item === currentPage
        return (
          <Button
            key={item}
            variant={isActive ? 'default' : 'ghost'}
            size="icon"
            className="h-7 w-7 text-xs"
            aria-current={isActive ? 'page' : undefined}
            onClick={() => onPageChange(item)}
          >
            {item}
          </Button>
        )
      })}

      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        disabled={currentPage >= totalPages}
        onClick={() => onPageChange(currentPage + 1)}
        aria-label="Next page"
      >
        <ChevronRight className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}

export default Pagination
