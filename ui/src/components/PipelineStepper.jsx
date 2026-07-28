import React from 'react'
import { Check, Loader2, X } from 'lucide-react'
import {
  RUNNING_BACKEND_STAGES,
  USER_PIPELINE_STAGES,
  mapStageToUserStep,
  stageMeta,
} from '../lib/pipelineUi'
import { cn } from '../lib/utils'

function resolveEffectiveIndex(currentStage, hasPages, hasChunks) {
  const isFailed = currentStage === 'failed'
  const userStepId = mapStageToUserStep(currentStage)
  let effectiveIndex = USER_PIPELINE_STAGES.findIndex(stage => stage.id === userStepId)

  if (isFailed) {
    // Approximate where failure happened so completed steps stay checked.
    if (hasChunks) {
      effectiveIndex = USER_PIPELINE_STAGES.findIndex(stage => stage.id === 'ingesting')
    } else if (hasPages) {
      effectiveIndex = USER_PIPELINE_STAGES.findIndex(stage => stage.id === 'chunk_review')
    } else {
      effectiveIndex = USER_PIPELINE_STAGES.findIndex(stage => stage.id === 'ocr_processing')
    }
  }

  if (effectiveIndex < 0) effectiveIndex = 0
  return { isFailed, effectiveIndex }
}

function stageStatus(index, effectiveIndex, isFailed) {
  if (isFailed) {
    if (index < effectiveIndex) return 'completed'
    if (index === effectiveIndex) return 'failed'
    return 'pending'
  }
  if (index < effectiveIndex) return 'completed'
  if (index === effectiveIndex) return 'active'
  return 'pending'
}

export default function PipelineStepper({ currentStage, hasPages = false, hasChunks = false, className }) {
  const { isFailed, effectiveIndex } = resolveEffectiveIndex(currentStage, hasPages, hasChunks)
  const total = USER_PIPELINE_STAGES.length
  const progressPct = total <= 1 ? 0 : Math.min(100, Math.max(0, (effectiveIndex / (total - 1)) * 100))
  const showSpinner = RUNNING_BACKEND_STAGES.has(currentStage)

  return (
    <div className={cn('w-full min-w-0', className)}>
      <div className="relative">
        <div className="absolute left-3 right-3 top-[13px] h-px rounded-full bg-border" />
        <div
          className={cn(
            'absolute left-3 top-[13px] h-px rounded-full transition-all duration-500',
            isFailed ? 'bg-destructive/50' : 'bg-primary/70',
          )}
          style={{ width: `max(0px, calc(${progressPct}% - 0.25rem))`, maxWidth: 'calc(100% - 1.5rem)' }}
        />

        <ol className="relative flex min-w-0 items-start justify-between gap-0.5 overflow-x-auto pb-0.5 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {USER_PIPELINE_STAGES.map((stage, index) => {
            const status = stageStatus(index, effectiveIndex, isFailed)
            const meta = stageMeta[stage.id] || {}
            const title = meta.description || stage.label
            const label = stage.label
            const spinning = status === 'active' && showSpinner

            return (
              <li
                key={stage.id}
                className="flex min-w-[4.1rem] flex-1 flex-col items-center gap-1 sm:min-w-0"
                title={`${stage.label}${title ? ` — ${title}` : ''}`}
              >
                <div
                  className={cn(
                    'relative z-[1] flex h-7 w-7 items-center justify-center rounded-full border text-[10px] font-semibold shadow-sm transition-all',
                    status === 'completed' && 'border-success/50 bg-success text-white',
                    status === 'active' && 'border-primary bg-primary text-primary-foreground ring-2 ring-primary/25',
                    status === 'failed' && 'border-destructive bg-destructive text-white ring-2 ring-destructive/25',
                    status === 'pending' && 'border-border bg-card text-muted-foreground',
                  )}
                >
                  {status === 'completed' ? (
                    <Check className="h-3.5 w-3.5" strokeWidth={2.5} />
                  ) : status === 'failed' ? (
                    <X className="h-3.5 w-3.5" strokeWidth={2.5} />
                  ) : spinning ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <span>{index + 1}</span>
                  )}
                </div>
                <span
                  className={cn(
                    'max-w-[5.25rem] text-center text-[9px] leading-tight sm:max-w-none sm:text-[10px]',
                    status === 'active' && 'font-semibold text-foreground',
                    status === 'completed' && 'font-medium text-success',
                    status === 'failed' && 'font-semibold text-destructive',
                    status === 'pending' && 'text-muted-foreground',
                  )}
                >
                  {label}
                </span>
              </li>
            )
          })}
        </ol>
      </div>
    </div>
  )
}
