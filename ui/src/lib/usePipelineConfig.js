import { useEffect, useState } from 'react'
import { fetchJson } from './pipelineUi'

/**
 * Runtime pipeline shape, read once from the backend.
 *
 * `/pipeline/stages` omits the PROD stages when DISABLE_PROD_SETTING is on, so
 * their presence is what tells the UI whether promotion exists at all. Cached
 * at module level: the flag is deployment-wide and cannot change while the tab
 * is open, so every stepper on the page shares one request.
 */

const DEFAULT_CONFIG = { prodEnabled: true, loaded: false }

let cache = null
let inflight = null

export function fetchPipelineConfig() {
  if (cache) return Promise.resolve(cache)
  if (!inflight) {
    inflight = fetchJson('/pipeline/stages')
      .then(stages => {
        const ids = new Set((Array.isArray(stages) ? stages : []).map(s => s.id))
        cache = { prodEnabled: ids.has('approval_for_prod'), loaded: true }
        return cache
      })
      .catch(() => {
        // Assume PROD exists on failure — showing a stage that never activates
        // is a cosmetic problem; hiding a real approval gate is a functional one.
        inflight = null
        return DEFAULT_CONFIG
      })
  }
  return inflight
}

export function usePipelineConfig() {
  const [config, setConfig] = useState(cache || DEFAULT_CONFIG)

  useEffect(() => {
    if (cache) return undefined
    let alive = true
    fetchPipelineConfig().then(next => {
      if (alive) setConfig(next)
    })
    return () => {
      alive = false
    }
  }, [])

  return config
}
