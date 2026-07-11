import React from 'react'
import { Badge } from './ui/badge'
import { getChunkTagLabels } from '../lib/pipelineUi'

export default function DomainTagBadges({
  tags,
  chunk,
  limit,
  className = '',
  onTagClick,
}) {
  const labels = tags || getChunkTagLabels(chunk)
  const visible = limit ? labels.slice(0, limit) : labels
  const hiddenCount = limit && labels.length > limit ? labels.length - limit : 0

  if (!labels.length) {
    return null
  }

  return (
    <div className={`flex flex-wrap items-center gap-1 ${className}`}>
      {visible.map(tag => (
        <Badge
          key={tag}
          variant="outline"
          className={`text-[10px] font-normal ${onTagClick ? 'cursor-pointer hover:bg-muted' : ''}`}
          onClick={onTagClick ? () => onTagClick(tag) : undefined}
        >
          {tag}
        </Badge>
      ))}
      {hiddenCount > 0 && (
        <Badge variant="secondary" className="text-[10px] font-normal">
          +{hiddenCount}
        </Badge>
      )}
    </div>
  )
}
