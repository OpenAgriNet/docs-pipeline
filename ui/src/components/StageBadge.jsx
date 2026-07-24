import React from 'react'
import { cn } from '../lib/utils'
import { getStageLabel } from '../lib/pipelineUi'

const stageColorMap = {
  registered: 'bg-stage-registered/15 text-stage-registered',
  normalization: 'bg-stage-normalization/15 text-stage-normalization',
  ocr_processing: 'bg-stage-ocr/15 text-stage-ocr',
  ocr_review: 'bg-stage-ocr/15 text-stage-ocr',
  translation_processing: 'bg-stage-translation/15 text-stage-translation',
  translation_review: 'bg-stage-translation/15 text-stage-translation',
  chunking: 'bg-stage-chunking/15 text-stage-chunking',
  chunk_review: 'bg-stage-chunking/15 text-stage-chunking',
  ready_for_ingestion: 'bg-stage-ingestion/15 text-stage-ingestion',
  ingesting: 'bg-stage-ingestion/15 text-stage-ingestion',
  approval_for_prod: 'bg-stage-ingestion/15 text-stage-ingestion',
  completed: 'bg-success/15 text-success',
  failed: 'bg-destructive/15 text-destructive'
}

export function StageBadge({ stage, className, compact = false }) {
  return (
    <span className={cn('stage-badge', stageColorMap[stage] || 'bg-muted text-muted-foreground', className)}>
      <span className={cn(
        'w-1.5 h-1.5 rounded-full',
        stage === 'failed' ? 'bg-destructive' :
        stage === 'completed' || stage === 'ready_for_ingestion' || stage === 'ingesting' || stage === 'approval_for_prod' ? 'bg-stage-ingestion' :
        stage?.includes('translation') ? 'bg-stage-translation' :
        stage?.includes('chunk') ? 'bg-stage-chunking' :
        stage?.includes('ocr') ? 'bg-stage-ocr' :
        stage === 'normalization' ? 'bg-stage-normalization' :
        'bg-stage-registered'
      )} />
      {getStageLabel(stage, { compact })}
    </span>
  )
}
