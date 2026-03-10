import React from 'react'
import { PIPELINE_STAGES, styles } from '../styles/appStyles'

export default function PipelineStepper({ currentStage, hasPages = false, hasChunks = false }) {
  const isFailed = currentStage === 'failed'
  let effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === currentStage)

  if (isFailed) {
    if (hasChunks) {
      effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === 'ingesting')
    } else if (hasPages) {
      effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === 'chunking')
    } else {
      effectiveIndex = PIPELINE_STAGES.findIndex(stage => stage.id === 'ocr_processing')
    }
  }

  return (
    <div style={styles.stepper}>
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
          <div key={stage.id} style={styles.stepperStep(status)}>
            {index < PIPELINE_STAGES.length - 1 && (
              <div style={styles.stepperLine(index < effectiveIndex ? 'completed' : 'pending')} />
            )}
            <div style={styles.stepperCircle(status)}>
              {status === 'completed' ? '✓' : status === 'failed' ? '✕' : index + 1}
            </div>
            <span style={styles.stepperLabel(status)}>{stage.label}</span>
          </div>
        )
      })}
    </div>
  )
}
