import React from 'react'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import PipelineStepper from './PipelineStepper'
import { getDocumentFileLabel, getDocumentListLabel, getDocumentMetaLabel, summarizeAvailableAction } from '../lib/pipelineUi'

export function DocumentDetailHeader({
  doc,
  activeJob,
  reviewedPages,
  reviewedChunks,
  visibleActions,
  onNavigateBack,
  onRunAction,
}) {
  return (
    <div className="shrink-0 border-b border-border bg-card px-4 py-3">
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={onNavigateBack}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="max-w-[420px] truncate text-lg font-serif font-semibold text-foreground">
                {getDocumentListLabel(doc)}
              </h1>
              <Badge variant={doc.authoritative ? 'default' : 'secondary'} className="text-[10px]">
                {doc.authoritative ? 'Authoritative' : 'Legacy'}
              </Badge>
              {doc.stage === 'failed' ? <Badge variant="destructive" className="text-[10px]">Failed</Badge> : null}
              {activeJob?.status === 'running' ? (
                <Badge variant="info" className="text-[10px]">
                  <Loader2 className="mr-0.5 h-2.5 w-2.5 animate-spin" />
                  Processing
                </Badge>
              ) : null}
              {doc.reindex_required ? (
                <Badge variant="warning" className="text-[10px]">
                  Reindex required
                </Badge>
              ) : null}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
              <span className="font-mono">{getDocumentMetaLabel(doc)}</span>
              <span>·</span>
              <span className="max-w-[220px] truncate">{getDocumentFileLabel(doc)}</span>
              <span>·</span>
              <span>{doc.page_count} pages ({reviewedPages} reviewed)</span>
              <span>·</span>
              <span>{doc.chunk_count} chunks ({reviewedChunks} reviewed)</span>
            </div>
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 overflow-x-auto">
          <PipelineStepper currentStage={doc.stage} hasPages={doc.page_count > 0} hasChunks={doc.chunk_count > 0} />
          <div className="flex shrink-0 items-center gap-1.5">
            {visibleActions.slice(0, 4).map(action => (
              <Button
                key={action}
                size="sm"
                variant={action.includes('approve') ? 'success' : action.includes('reindex') || action.includes('reingest') ? 'warning' : 'outline'}
                className="h-7 text-xs"
                onClick={() => onRunAction(action)}
              >
                {summarizeAvailableAction(action)}
              </Button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
