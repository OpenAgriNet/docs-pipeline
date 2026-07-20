import React from 'react'
import { PIPELINE_STAGES } from '../lib/pipelineUi'
import { cn } from '../lib/utils'

export default function PipelineStepper({ currentStage, hasPages = false, hasChunks = false }) {
  const isFailed = currentStage === 'failed'
  let effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === currentStage)

  if (isFailed) {
    if (hasChunks) effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === 'ingesting')
    else if (hasPages) effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === 'chunking')
    else effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === 'ocr_processing')
  }

  return (
    <div className="flex items-center gap-1 overflow-x-auto">
        {PIPELINE_STAGES.map((stage, index) => {
          let status = 'pending'
          if (isFailed) {
            if (index < effectiveIndex) status = 'completed'
            else if (index === effectiveIndex) status = 'failed'
          } else {
            if (index < effectiveIndex) status = 'completed'
            else if (index === effectiveIndex) status = 'active'
          }

          return (
            <React.Fragment key={stage.id}>
              <div className="flex flex-col items-center gap-1">
                <div
                  className={cn(
                    'flex h-7 w-7 items-center justify-center rounded-full text-[10px] font-medium transition-colors',
                    status === 'completed' && 'bg-success/20 text-success',
                    status === 'active' && 'bg-primary/20 text-primary ring-2 ring-primary/30',
                    status === 'failed' && 'bg-destructive/20 text-destructive ring-2 ring-destructive/30',
                    status === 'pending' && 'bg-muted text-muted-foreground'
                  )}
                >
                  {status === 'completed' ? '✓' : index + 1}
                </div>
                <span
                  className={cn(
                    'whitespace-nowrap text-center text-[10px] leading-tight',
                    status === 'active' ? 'font-medium text-foreground' : 'text-muted-foreground'
                  )}
                >
                  {stage.shortLabel || stage.label}
                </span>
              </div>
              {index < PIPELINE_STAGES.length - 1 ? (
                <div className={cn('mt-[-14px] h-px w-4', index < effectiveIndex ? 'bg-success/40' : 'bg-border')} />
              ) : null}
            </React.Fragment>
          )
        })}
    </div>
  )
}
