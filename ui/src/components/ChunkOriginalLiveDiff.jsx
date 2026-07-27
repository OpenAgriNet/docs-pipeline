import React, { useMemo } from 'react'
import { Badge } from './ui/badge'
import { Textarea } from './ui/textarea'

/** Live searchable text: draft edit → saved edit → legacy text → original. */
export function getChunkLiveText(chunk, draft) {
  if (draft !== undefined) return draft
  if (chunk?.edited_text != null && chunk.edited_text !== '') return chunk.edited_text
  if (chunk?.text != null && chunk.text !== '') return chunk.text
  return chunk?.original_text ?? ''
}

export function getChunkOriginalText(chunk) {
  return chunk?.original_text ?? ''
}

export function isChunkDivergedFromOriginal(chunk, draft) {
  return getChunkLiveText(chunk, draft) !== getChunkOriginalText(chunk)
}

/**
 * Side-by-side Original (parsed) vs Live (what will be used) when they differ.
 * Matches the OCR tab pattern already used for pages.
 */
export default function ChunkOriginalLiveDiff({
  chunk,
  draft,
  onDraftChange,
  disabled = false,
  className = '',
}) {
  const original = getChunkOriginalText(chunk)
  const live = getChunkLiveText(chunk, draft)
  const diverged = live !== original

  const summary = useMemo(() => {
    if (!diverged) return null
    const origLines = original.split('\n').length
    const liveLines = live.split('\n').length
    return {
      origChars: original.length,
      liveChars: live.length,
      origLines,
      liveLines,
    }
  }, [diverged, original, live])

  if (!diverged) {
    return (
      <div className={`space-y-2 ${className}`}>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="secondary" className="text-[10px]">Live</Badge>
          <span className="text-[10px] text-muted-foreground">Same as original parsed text</span>
        </div>
        <Textarea
          value={live}
          onChange={e => onDraftChange?.(e.target.value)}
          disabled={disabled}
          className="text-xs font-mono min-h-[80px] resize-y"
        />
      </div>
    )
  }

  return (
    <div className={`space-y-2 ${className}`}>
      <div className="flex items-center gap-2 flex-wrap">
        <Badge variant="outline" className="text-[10px]">Original</Badge>
        <Badge variant="default" className="text-[10px]">Live</Badge>
        <Badge variant="warning" className="text-[10px]">Edited</Badge>
        {summary && (
          <span className="text-[10px] text-muted-foreground">
            {summary.origChars}→{summary.liveChars} chars · {summary.origLines}→{summary.liveLines} lines
          </span>
        )}
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <label className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider">
            Original (parsed)
          </label>
          <div className="rounded-md border border-border bg-muted/30 p-3 text-xs font-mono whitespace-pre-wrap min-h-[80px] max-h-[320px] overflow-auto text-muted-foreground">
            {original || '(empty)'}
          </div>
        </div>
        <div className="space-y-1.5">
          <label className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider">
            Live (editable)
          </label>
          <Textarea
            value={live}
            onChange={e => onDraftChange?.(e.target.value)}
            disabled={disabled}
            className="text-xs font-mono min-h-[80px] max-h-[320px] resize-y"
          />
        </div>
      </div>
    </div>
  )
}
