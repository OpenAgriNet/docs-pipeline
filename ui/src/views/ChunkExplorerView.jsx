import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, ChevronRight, Search, X } from 'lucide-react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Checkbox } from '../components/ui/checkbox'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import {
  buildDocumentChunkUrl,
  fetchJson,
  flattenDomainTaxonomy,
  getStageLabel,
  PIPELINE_STAGES,
} from '../lib/pipelineUi'

const PAGE_SIZE = 50
const ANY = '__any__'
const PRESET_STORAGE_KEY = 'chunk-explorer-presets-v1'

export default function ChunkExplorerView() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  const [chunks, setChunks] = useState([])
  const [total, setTotal] = useState(0)
  const [taxonomy, setTaxonomy] = useState(null)

  const [query, setQuery] = useState('')
  const [stage, setStage] = useState('all')
  const [includeExcluded, setIncludeExcluded] = useState(false)
  const [selectedTags, setSelectedTags] = useState([])
  const [offset, setOffset] = useState(0)
  const [selectedDomain, setSelectedDomain] = useState(ANY)
  const [selectedDimension, setSelectedDimension] = useState(ANY)
  const [selectedValue, setSelectedValue] = useState(ANY)
  const [viewMode, setViewMode] = useState('detailed')
  const [groupMode, setGroupMode] = useState('none')
  const [presetName, setPresetName] = useState('')
  const [presets, setPresets] = useState([])
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const tagOptions = useMemo(() => flattenDomainTaxonomy(taxonomy), [taxonomy])
  const stageOptions = useMemo(() => ['all', ...PIPELINE_STAGES.map(s => s.id), 'failed'], [])
  const canPrev = offset > 0
  const canNext = offset + chunks.length < total

  function openChunkDocument(chunk) {
    if (!chunk?.workflow_id) return
    navigate(buildDocumentChunkUrl(chunk.workflow_id, chunk.chunk_number))
  }

  function openDocumentChunks(workflowId) {
    if (!workflowId) return
    navigate(buildDocumentChunkUrl(workflowId))
  }
  const domains = taxonomy?.domains || {}

  const domainOptions = useMemo(
    () => Object.keys(domains).sort((a, b) => a.localeCompare(b)),
    [domains],
  )

  const dimensionOptions = useMemo(() => {
    const source = selectedDomain === ANY
      ? Object.values(domains)
      : [domains[selectedDomain] || {}]
    const seen = new Set()
    const values = []
    source.forEach(domain => {
      Object.keys(domain || {}).forEach(dimension => {
        if (!seen.has(dimension)) {
          seen.add(dimension)
          values.push(dimension)
        }
      })
    })
    return values.sort((a, b) => a.localeCompare(b))
  }, [domains, selectedDomain])

  const directionalValueOptions = useMemo(() => {
    if (selectedDimension === ANY) return []
    const source = selectedDomain === ANY
      ? Object.values(domains)
      : [domains[selectedDomain] || {}]
    const seen = new Set()
    const values = []
    source.forEach(domain => {
      ;(domain?.[selectedDimension] || []).forEach(value => {
        if (!seen.has(value)) {
          seen.add(value)
          values.push(value)
        }
      })
    })
    return values.sort((a, b) => a.localeCompare(b))
  }, [domains, selectedDomain, selectedDimension])

  useEffect(() => {
    fetchJson('/taxonomy/domain-tags')
      .then(setTaxonomy)
      .catch(() => setTaxonomy({ domains: {} }))
  }, [])

  useEffect(() => {
    runSearch({ isInitial: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    try {
      const raw = localStorage.getItem(PRESET_STORAGE_KEY)
      if (!raw) return
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) setPresets(parsed)
    } catch (_) {
      setPresets([])
    }
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(presets))
    } catch (_) {
      // Ignore storage errors.
    }
  }, [presets])

  function toggleTag(tag) {
    setSelectedTags(current => (
      current.includes(tag) ? current.filter(item => item !== tag) : [...current, tag]
    ))
  }

  function addDirectionalTag() {
    if (selectedDimension === ANY || selectedValue === ANY) return
    const tag = `${selectedDimension}:${selectedValue}`
    setSelectedTags(current => (current.includes(tag) ? current : [...current, tag]))
  }

  async function runSearch({ isInitial = false, nextOffset = offset, filters } = {}) {
    try {
      if (isInitial) setLoading(true)
      setSearching(true)
      setError('')
      const active = filters || {
        query,
        stage,
        includeExcluded,
        selectedTags,
      }
      const params = new URLSearchParams()
      if ((active.query || '').trim()) params.set('q', (active.query || '').trim())
      if (active.stage && active.stage !== 'all') params.set('stage', active.stage)
      if (active.includeExcluded) params.set('include_excluded', 'true')
      ;(active.selectedTags || []).forEach(tag => params.append('tags', tag))
      params.set('limit', String(PAGE_SIZE))
      params.set('offset', String(nextOffset))

      const response = await fetchJson(`/chunks/search?${params.toString()}`)
      setChunks(Array.isArray(response.items) ? response.items : [])
      setTotal(Number(response.total || 0))
      setOffset(nextOffset)
    } catch (loadError) {
      setError(loadError.message)
      setChunks([])
      setTotal(0)
    } finally {
      setLoading(false)
      setSearching(false)
    }
  }

  function clearFilters() {
    const resetFilters = {
      query: '',
      stage: 'all',
      includeExcluded: false,
      selectedTags: [],
    }
    setQuery(resetFilters.query)
    setStage(resetFilters.stage)
    setIncludeExcluded(resetFilters.includeExcluded)
    setSelectedTags(resetFilters.selectedTags)
    setSelectedDomain(ANY)
    setSelectedDimension(ANY)
    setSelectedValue(ANY)
    setOffset(0)
    runSearch({ nextOffset: 0, filters: resetFilters })
  }

  function buildPresetPayload() {
    return {
      query,
      stage,
      includeExcluded,
      selectedTags,
      selectedDomain,
      selectedDimension,
      selectedValue,
      viewMode,
      groupMode,
    }
  }

  function applyPreset(preset) {
    const appliedFilters = {
      query: preset.query || '',
      stage: preset.stage || 'all',
      includeExcluded: Boolean(preset.includeExcluded),
      selectedTags: Array.isArray(preset.selectedTags) ? preset.selectedTags : [],
    }
    setQuery(appliedFilters.query)
    setStage(appliedFilters.stage)
    setIncludeExcluded(appliedFilters.includeExcluded)
    setSelectedTags(appliedFilters.selectedTags)
    setSelectedDomain(preset.selectedDomain || ANY)
    setSelectedDimension(preset.selectedDimension || ANY)
    setSelectedValue(preset.selectedValue || ANY)
    setViewMode(preset.viewMode || 'detailed')
    setGroupMode(preset.groupMode || 'none')
    runSearch({ nextOffset: 0, filters: appliedFilters })
  }

  function savePreset() {
    const name = presetName.trim()
    if (!name) return
    const payload = buildPresetPayload()
    setPresets(current => {
      const existingIndex = current.findIndex(item => item.name === name)
      const nextItem = { name, ...payload }
      if (existingIndex >= 0) {
        const next = [...current]
        next[existingIndex] = nextItem
        return next
      }
      return [nextItem, ...current].slice(0, 20)
    })
    setPresetName('')
  }

  function removePreset(name) {
    setPresets(current => current.filter(item => item.name !== name))
  }

  const groupedByDocument = useMemo(() => {
    const groups = new Map()
    chunks.forEach(chunk => {
      const key = chunk.workflow_id || '__unknown__'
      const label = chunk.display_name || chunk.filename || key
      const existing = groups.get(key) || { key, label, chunks: [] }
      existing.chunks.push(chunk)
      groups.set(key, existing)
    })
    return Array.from(groups.values())
      .map(group => ({
        ...group,
        chunks: group.chunks.sort((a, b) => Number(a.chunk_number || 0) - Number(b.chunk_number || 0)),
      }))
      .sort((a, b) => b.chunks.length - a.chunks.length || a.label.localeCompare(b.label))
  }, [chunks])

  const groupedByTag = useMemo(() => {
    const groups = new Map()
    chunks.forEach(chunk => {
      const tags = (chunk.domain_tags || []).map(tag => tag.tag).filter(Boolean)
      tags.forEach(tag => {
        const existing = groups.get(tag) || { key: tag, chunks: [] }
        existing.chunks.push(chunk)
        groups.set(tag, existing)
      })
    })
    return Array.from(groups.values())
      .map(group => ({
        ...group,
        chunks: group.chunks.sort((a, b) => {
          if ((a.workflow_id || '') !== (b.workflow_id || '')) {
            return (a.workflow_id || '').localeCompare(b.workflow_id || '')
          }
          return Number(a.chunk_number || 0) - Number(b.chunk_number || 0)
        }),
      }))
      .sort((a, b) => b.chunks.length - a.chunks.length || a.key.localeCompare(b.key))
  }, [chunks])

  if (loading) {
    return (
      <div className="p-6 space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Chunk Explorer</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Search by keyword, domain tag (<span className="font-mono text-xs">dimension:value</span>), or both.
          </p>
        </div>
        <Badge variant="secondary" className="text-xs">
          {total} matching chunks
        </Badge>
      </div>

      <div className="panel p-4 space-y-3">
        <div className="flex flex-col md:flex-row gap-2">
          <div className="relative flex-1">
            <Search className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="Keyword or tag (e.g. claim:eligibility, milking machine)..."
              className="pl-9"
            />
          </div>
          <Select value={stage} onValueChange={setStage}>
            <SelectTrigger className="w-full md:w-56">
              <SelectValue placeholder="Stage" />
            </SelectTrigger>
            <SelectContent>
              {stageOptions.map(value => (
                <SelectItem key={value} value={value}>
                  {value === 'all' ? 'All stages' : getStageLabel(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={() => runSearch({ nextOffset: 0 })} disabled={searching}>
            {searching ? 'Searching…' : 'Search'}
          </Button>
          <Button variant="outline" onClick={clearFilters} disabled={searching}>
            Clear
          </Button>
        </div>

        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Checkbox checked={includeExcluded} onCheckedChange={value => setIncludeExcluded(Boolean(value))} />
          <span>Include excluded chunks</span>
          <Button
            size="sm"
            variant="ghost"
            className="h-6 text-[10px] ml-auto"
            onClick={() => setAdvancedOpen(current => !current)}
          >
            {advancedOpen ? <ChevronDown className="h-3 w-3 mr-1" /> : <ChevronRight className="h-3 w-3 mr-1" />}
            Advanced filters
          </Button>
        </div>

        {advancedOpen && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="font-medium uppercase tracking-wider">Display</span>
            <Button
              size="sm"
              variant={viewMode === 'detailed' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setViewMode('detailed')}
            >
              Detailed
            </Button>
            <Button
              size="sm"
              variant={viewMode === 'compact' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setViewMode('compact')}
            >
              Compact
            </Button>
            <span className="mx-1">·</span>
            <Button
              size="sm"
              variant={groupMode === 'none' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setGroupMode('none')}
            >
              No groups
            </Button>
            <Button
              size="sm"
              variant={groupMode === 'document' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setGroupMode('document')}
            >
              Group by document
            </Button>
            <Button
              size="sm"
              variant={groupMode === 'tag' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setGroupMode('tag')}
            >
              Group by tag
            </Button>
          </div>
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Tag filters (all selected tags must match)
          </p>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-2">
            <Select
              value={selectedDomain}
              onValueChange={value => {
                setSelectedDomain(value)
                setSelectedDimension(ANY)
                setSelectedValue(ANY)
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="Domain" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>Any domain</SelectItem>
                {domainOptions.map(domain => (
                  <SelectItem key={domain} value={domain}>{domain}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={selectedDimension}
              onValueChange={value => {
                setSelectedDimension(value)
                setSelectedValue(ANY)
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder="Dimension" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>Any dimension</SelectItem>
                {dimensionOptions.map(dimension => (
                  <SelectItem key={dimension} value={dimension}>{dimension}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={selectedValue} onValueChange={setSelectedValue} disabled={selectedDimension === ANY}>
              <SelectTrigger>
                <SelectValue placeholder="Value" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>Select value</SelectItem>
                {directionalValueOptions.map(value => (
                  <SelectItem key={value} value={value}>{value}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button onClick={addDirectionalTag} disabled={selectedDimension === ANY || selectedValue === ANY}>
              Add directional tag
            </Button>
          </div>
          <div className="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto">
            {tagOptions.slice(0, 80).map(option => {
              const active = selectedTags.includes(option.tag)
              return (
                <button
                  key={option.tag}
                  type="button"
                  onClick={() => toggleTag(option.tag)}
                  className={`text-[10px] px-2 py-0.5 rounded border ${
                    active ? 'bg-primary/10 border-primary/40 text-foreground' : 'border-border text-muted-foreground'
                  }`}
                >
                  {option.tag}
                </button>
              )
            })}
          </div>
          {selectedTags.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {selectedTags.map(tag => (
                <Badge key={tag} variant="secondary" className="text-[10px]">
                  {tag}
                  <button
                    type="button"
                    className="ml-1 inline-flex"
                    onClick={() => toggleTag(tag)}
                    aria-label={`Remove ${tag}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))}
            </div>
          )}
        </div>
        )}

        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Saved presets
          </p>
          <div className="flex flex-col md:flex-row gap-2">
            <Input
              value={presetName}
              onChange={event => setPresetName(event.target.value)}
              placeholder="Preset name (e.g. Cattle disease + treatment)"
              className="md:max-w-sm"
            />
            <Button variant="outline" onClick={savePreset}>Save current filters</Button>
          </div>
          {presets.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {presets.map(preset => (
                <Badge key={preset.name} variant="secondary" className="text-[10px] gap-1">
                  <button type="button" onClick={() => applyPreset(preset)} className="hover:underline">
                    {preset.name}
                  </button>
                  <button
                    type="button"
                    onClick={() => removePreset(preset.name)}
                    aria-label={`Delete ${preset.name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))}
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="panel p-4 border-destructive/30 bg-destructive/5 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="space-y-2">
        {chunks.length === 0 ? (
          <div className="panel p-10 text-center">
            <p className="text-sm font-medium">No chunks found</p>
            <p className="text-xs text-muted-foreground mt-1">Try fewer filters or a broader keyword.</p>
          </div>
        ) : groupMode === 'document' ? (
          groupedByDocument.map(group => (
            <div key={group.key} className="panel p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  className="text-sm font-medium text-primary hover:underline truncate"
                  onClick={() => openDocumentChunks(group.key)}
                >
                  {group.label}
                </button>
                <Badge variant="secondary" className="text-[10px]">{group.chunks.length} chunks</Badge>
              </div>
              <div className="space-y-2">
                {group.chunks.map(chunk => (
                  <div
                    key={`${chunk.workflow_id}-${chunk.chunk_number}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => openChunkDocument(chunk)}
                    onKeyDown={event => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        openChunkDocument(chunk)
                      }
                    }}
                    className="rounded border border-border/80 p-2 space-y-1.5 cursor-pointer hover:border-primary/50 hover:bg-muted/30 transition-colors"
                  >
                    <p className="text-xs text-muted-foreground">
                      Chunk {chunk.chunk_number} · Pages {chunk.page_start}–{chunk.page_end} · {getStageLabel(chunk.stage)}
                    </p>
                    <p className={viewMode === 'detailed' ? 'text-sm text-foreground/90' : 'text-xs text-muted-foreground'}>
                      {viewMode === 'detailed'
                        ? String(chunk.edited_text || chunk.original_text || '').slice(0, 280)
                        : `${(chunk.workflow_id || '').slice(0, 20)}... · #${chunk.chunk_number} · ${chunk.token_count || 0} tokens`}
                    </p>
                    {(chunk.domain_tags || []).length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {chunk.domain_tags.map(tag => (
                          <Badge key={`${chunk.workflow_id}-${chunk.chunk_number}-${tag.tag}`} variant="outline" className="text-[10px]">
                            {tag.tag}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))
        ) : groupMode === 'tag' ? (
          groupedByTag.map(group => (
            <div key={group.key} className="panel p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <Badge variant="outline" className="text-xs">{group.key}</Badge>
                <Badge variant="secondary" className="text-[10px]">{group.chunks.length} chunks</Badge>
              </div>
              <div className="space-y-2">
                {group.chunks.map(chunk => (
                  <div
                    key={`${group.key}-${chunk.workflow_id}-${chunk.chunk_number}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => openChunkDocument(chunk)}
                    onKeyDown={event => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        openChunkDocument(chunk)
                      }
                    }}
                    className="rounded border border-border/80 p-2 space-y-1.5 cursor-pointer hover:border-primary/50 hover:bg-muted/30 transition-colors"
                  >
                    <button
                      type="button"
                      className="text-xs font-medium text-primary hover:underline"
                      onClick={event => {
                        event.stopPropagation()
                        openDocumentChunks(chunk.workflow_id)
                      }}
                    >
                      {chunk.display_name || chunk.filename || chunk.workflow_id}
                    </button>
                    <p className="text-xs text-muted-foreground">
                      Chunk {chunk.chunk_number} · Pages {chunk.page_start}–{chunk.page_end} · {getStageLabel(chunk.stage)}
                    </p>
                    <p className={viewMode === 'detailed' ? 'text-sm text-foreground/90' : 'text-xs text-muted-foreground'}>
                      {viewMode === 'detailed'
                        ? String(chunk.edited_text || chunk.original_text || '').slice(0, 280)
                        : `${(chunk.workflow_id || '').slice(0, 20)}... · #${chunk.chunk_number} · ${chunk.token_count || 0} tokens`}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          ))
        ) : (
          chunks.map(chunk => (
            <div
              key={`${chunk.workflow_id}-${chunk.chunk_number}`}
              role="button"
              tabIndex={0}
              onClick={() => openChunkDocument(chunk)}
              onKeyDown={event => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  openChunkDocument(chunk)
                }
              }}
              className="panel p-4 space-y-2 cursor-pointer hover:border-primary/40 hover:bg-muted/20 transition-colors"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <button
                    type="button"
                    className="text-sm font-medium text-primary hover:underline truncate"
                    onClick={event => {
                      event.stopPropagation()
                      openDocumentChunks(chunk.workflow_id)
                    }}
                  >
                    {chunk.display_name || chunk.filename || chunk.workflow_id}
                  </button>
                  <p className="text-xs text-muted-foreground">
                    Chunk {chunk.chunk_number} · Pages {chunk.page_start}–{chunk.page_end} · {getStageLabel(chunk.stage)}
                  </p>
                </div>
                {chunk.is_excluded ? <Badge variant="destructive">Excluded</Badge> : null}
              </div>

              {viewMode === 'detailed' ? (
                <p className="text-sm leading-relaxed text-foreground/90">
                  {String(chunk.edited_text || chunk.original_text || '').slice(0, 520)}
                </p>
              ) : (
                <p className="text-xs leading-relaxed text-muted-foreground">
                  {(chunk.workflow_id || '').slice(0, 20)}... · #{chunk.chunk_number} · {chunk.token_count || 0} tokens
                </p>
              )}

              {(chunk.domain_tags || []).length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {chunk.domain_tags.map(tag => (
                    <Badge key={`${chunk.workflow_id}-${chunk.chunk_number}-${tag.tag}`} variant="outline" className="text-[10px]">
                      {tag.tag}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      <div className="flex items-center justify-end gap-2">
        <Button variant="outline" onClick={() => runSearch({ nextOffset: Math.max(0, offset - PAGE_SIZE) })} disabled={!canPrev || searching}>
          Previous
        </Button>
        <Button onClick={() => runSearch({ nextOffset: offset + PAGE_SIZE })} disabled={!canNext || searching}>
          Next
        </Button>
      </div>
    </div>
  )
}
