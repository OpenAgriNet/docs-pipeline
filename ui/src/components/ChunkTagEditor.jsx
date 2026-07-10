import React, { useEffect, useMemo, useState } from 'react'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Input } from './ui/input'
import { fetchJson, flattenDomainTaxonomy } from '../lib/pipelineUi'

function flattenTaxonomy(taxonomy) {
  return flattenDomainTaxonomy(taxonomy)
}

export default function ChunkTagEditor({ workflowId, chunk, onSaved, onMessage, showAutoTagButton = false }) {
  const [taxonomy, setTaxonomy] = useState(null)
  const [draftTags, setDraftTags] = useState([])
  const [customTag, setCustomTag] = useState('')
  const [saving, setSaving] = useState(false)
  const [autoTagging, setAutoTagging] = useState(false)

  const options = useMemo(() => flattenTaxonomy(taxonomy), [taxonomy])

  useEffect(() => {
    fetchJson('/taxonomy/domain-tags').then(setTaxonomy).catch(() => setTaxonomy({ domains: {} }))
  }, [])

  useEffect(() => {
    const existing = (chunk?.domain_tags || []).map(t => `${t.dimension}:${t.value}`)
    setDraftTags(existing)
  }, [chunk?.chunk_number, chunk?.domain_tags_flat])

  function toggleTag(tag) {
    setDraftTags(current => (
      current.includes(tag) ? current.filter(item => item !== tag) : [...current, tag]
    ))
  }

  function addCustomTag() {
    const normalized = customTag.trim().toLowerCase()
    if (!normalized.includes(':')) {
      onMessage?.('Use dimension:value format, e.g. region:north')
      return
    }
    if (!draftTags.includes(normalized)) {
      setDraftTags(current => [...current, normalized])
    }
    setCustomTag('')
  }

  async function saveManualTags() {
    try {
      setSaving(true)
      await fetchJson(`/documents/${workflowId}/chunks/${chunk.chunk_number}/tags`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tags: draftTags }),
      })
      onMessage?.(`Tags saved for chunk ${chunk.chunk_number}`)
      onSaved?.()
    } catch (error) {
      onMessage?.(error.message)
    } finally {
      setSaving(false)
    }
  }

  async function runAutoTag() {
    try {
      setAutoTagging(true)
      await fetchJson(`/documents/${workflowId}/auto-tag-chunks`, { method: 'POST' })
      onMessage?.('Auto-tagging complete for document')
      onSaved?.()
    } catch (error) {
      onMessage?.(error.message)
    } finally {
      setAutoTagging(false)
    }
  }

  const autoTags = (chunk?.domain_tags || []).filter(t => t.source === 'auto')
  const manualTags = (chunk?.domain_tags || []).filter(t => t.source === 'manual')

  return (
    <div className="mt-3 space-y-3 border-t border-border pt-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Domain tags</span>
        {showAutoTagButton && (
          <Button size="sm" variant="outline" className="h-6 text-[10px]" onClick={runAutoTag} disabled={autoTagging}>
            {autoTagging ? 'Auto-tagging…' : 'Re-run auto tags'}
          </Button>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {draftTags.length ? draftTags.map(tag => (
          <Badge key={tag} variant="secondary" className="text-[10px] cursor-pointer" onClick={() => toggleTag(tag)}>
            {tag}
          </Badge>
        )) : (
          <span className="text-[11px] text-muted-foreground">No tags yet</span>
        )}
      </div>

      {(autoTags.length > 0 || manualTags.length > 0) && (
        <p className="text-[10px] text-muted-foreground">
          auto: {autoTags.length} · manual: {manualTags.length}
        </p>
      )}

      <div className="max-h-28 overflow-y-auto flex flex-wrap gap-1">
        {options.slice(0, 40).map(opt => (
          <button
            key={opt.tag}
            type="button"
            className={`text-[10px] px-2 py-0.5 rounded border ${draftTags.includes(opt.tag) ? 'bg-primary/10 border-primary/40' : 'border-border text-muted-foreground'}`}
            onClick={() => toggleTag(opt.tag)}
          >
            {opt.tag}
          </button>
        ))}
      </div>

      <div className="flex gap-2">
        <Input
          value={customTag}
          onChange={e => setCustomTag(e.target.value)}
          placeholder="dimension:value"
          className="h-7 text-xs"
        />
        <Button size="sm" variant="outline" className="h-7 text-[10px]" onClick={addCustomTag}>Add</Button>
        <Button size="sm" className="h-7 text-[10px]" onClick={saveManualTags} disabled={saving}>
          {saving ? 'Saving…' : 'Save tags'}
        </Button>
      </div>
    </div>
  )
}
