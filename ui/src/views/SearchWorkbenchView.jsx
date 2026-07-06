import React, { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, Code, RotateCcw, Search as SearchIcon, Sliders, AlertCircle } from 'lucide-react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import {
  DEFAULT_SEARCH_SETTINGS,
  fetchJson,
  flattenDomainTaxonomy,
  getCandidateRank,
  getCandidateHitId,
  getSearchHighlights,
  getSearchResultSnippet,
  getSearchResultTitle,
  highlightSearchSnippet,
  parseDomainTagsField,
  summarizeCandidateMethod,
} from '../lib/pipelineUi'

export default function SearchWorkbenchView() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [showTagFilters, setShowTagFilters] = useState(false)
  const [showCandidates, setShowCandidates] = useState(false)
  const [settings, setSettings] = useState(DEFAULT_SEARCH_SETTINGS)
  const [taxonomy, setTaxonomy] = useState(null)
  const [selectedTags, setSelectedTags] = useState([])
  const [searched, setSearched] = useState(false)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState(null)
  const [candidates, setCandidates] = useState([])

  const tagOptions = React.useMemo(() => flattenDomainTaxonomy(taxonomy), [taxonomy])

  useEffect(() => {
    fetchSettings()
    fetchJson('/taxonomy/domain-tags').then(setTaxonomy).catch(() => setTaxonomy({ domains: {} }))
  }, [])

  async function fetchSettings() {
    try {
      const data = await fetchJson('/settings/search')
      setSettings(data)
    } catch (settingsError) {
      setSearchError(settingsError.message)
    }
  }

  async function handleSearch() {
    if (!query.trim()) return
    try {
      setSearching(true)
      setSearchError(null)
      const data = await fetchJson('/marqo/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query,
          index_name: settings.indexName,
          search_mode: settings.searchMethod,
          top_k: settings.limit,
          candidate_cap: settings.candidateCap,
          candidate_multiplier: settings.candidateMultiplier,
          max_chunks_per_doc: settings.maxChunksPerDoc,
          use_e5_prefix: settings.useE5Prefix,
          exclude_reference: settings.excludeReference,
          hybrid_alpha: settings.alpha,
          ranking_method: settings.rankingMethod,
          ef_search: settings.efSearch,
          query_expansion_profile: settings.queryExpansionProfile,
          rerank_mode: settings.rerankMode,
          hybrid_rrf_k: settings.hybridRrfK,
          include_raw_hits: true,
          domain_tags: selectedTags,
        })
      })
      setResults(data.hits || [])
      setCandidates(data.raw_hits || data.candidates || [])
      setSearched(true)
    } catch (err) {
      setSearchError(err.message)
    } finally {
      setSearching(false)
    }
  }

  function handleReset() {
    setSettings(DEFAULT_SEARCH_SETTINGS)
  }

  function update(key, value) {
    setSettings(current => ({ ...current, [key]: value }))
  }

  function toggleTag(tag) {
    setSelectedTags(current => (
      current.includes(tag) ? current.filter(item => item !== tag) : [...current, tag]
    ))
  }

  const changedSettings = Object.entries(settings).filter(([key, value]) => DEFAULT_SEARCH_SETTINGS[key] !== value)

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-4">
      <div>
        <h1 className="text-2xl font-serif font-semibold text-foreground">Search Workbench</h1>
        <p className="text-sm text-muted-foreground mt-1">Query the pipeline-managed search index</p>
      </div>

      <div className="panel p-4 space-y-3">
        <div className="flex gap-2">
          <div className="relative flex-1">
            <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Enter your search query..."
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              className="pl-9 h-11"
            />
          </div>
          <Button onClick={handleSearch} className="h-11 px-6" disabled={searching}>
            {searching ? 'Searching...' : 'Search'}
          </Button>
        </div>

        <div className="flex items-center justify-between">
          <button
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
            onClick={() => setShowTagFilters(!showTagFilters)}
          >
            <Sliders className="h-3.5 w-3.5" />
            Domain tag filters
            {selectedTags.length > 0 && (
              <Badge variant="secondary" className="text-[10px]">{selectedTags.length}</Badge>
            )}
            {showTagFilters ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
          <button
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
            onClick={() => setShowAdvanced(!showAdvanced)}
          >
            <Sliders className="h-3.5 w-3.5" />
            Advanced settings
            {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
          {changedSettings.length > 0 && !showAdvanced && (
            <span className="text-[10px] text-muted-foreground">
              {changedSettings.length} override{changedSettings.length !== 1 ? 's' : ''} active
            </span>
          )}
        </div>

        {showTagFilters && (
          <div className="space-y-2 pt-2 border-t border-border">
            <p className="text-[11px] text-muted-foreground">
              Narrow results by Amul domain tags (all selected tags must match).
            </p>
            {selectedTags.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {selectedTags.map(tag => (
                  <Badge key={tag} variant="secondary" className="text-[10px] cursor-pointer" onClick={() => toggleTag(tag)}>
                    {tag}
                  </Badge>
                ))}
                <Button size="sm" variant="ghost" className="h-6 text-[10px]" onClick={() => setSelectedTags([])}>
                  Clear
                </Button>
              </div>
            )}
            <div className="max-h-32 overflow-y-auto flex flex-wrap gap-1">
              {tagOptions.slice(0, 48).map(opt => (
                <button
                  key={opt.tag}
                  type="button"
                  className={`text-[10px] px-2 py-0.5 rounded border ${selectedTags.includes(opt.tag) ? 'bg-primary/10 border-primary/40' : 'border-border text-muted-foreground'}`}
                  onClick={() => toggleTag(opt.tag)}
                >
                  {opt.tag}
                </button>
              ))}
            </div>
          </div>
        )}

        {showAdvanced && (
          <div className="space-y-3 pt-2 border-t border-border">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Method</label>
                <select
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                  value={settings.searchMethod}
                  onChange={e => update('searchMethod', e.target.value)}
                >
                  <option>HYBRID</option>
                  <option>TENSOR</option>
                  <option>LEXICAL</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Limit</label>
                <Input type="number" value={settings.limit} onChange={e => update('limit', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Alpha</label>
                <Input type="number" step="0.1" min="0" max="1" value={settings.alpha} onChange={e => update('alpha', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Index</label>
                <Input value={settings.indexName} onChange={e => update('indexName', e.target.value)} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">efSearch</label>
                <Input type="number" value={settings.efSearch} onChange={e => update('efSearch', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Candidate Cap</label>
                <Input type="number" value={settings.candidateCap} onChange={e => update('candidateCap', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Max Chunks/Doc</label>
                <Input type="number" value={settings.maxChunksPerDoc} onChange={e => update('maxChunksPerDoc', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Ranking</label>
                <select
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                  value={settings.rankingMethod}
                  onChange={e => update('rankingMethod', e.target.value)}
                >
                  <option value="rrf">RRF</option>
                  <option value="normalize_linear">Normalize Linear</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Rerank</label>
                <select
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                  value={settings.rerankMode}
                  onChange={e => update('rerankMode', e.target.value)}
                >
                  <option value="none">None</option>
                  <option value="cross-encoder">Cross-encoder</option>
                  <option value="colbert">ColBERT</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Query Expansion</label>
                <select
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                  value={settings.queryExpansionProfile}
                  onChange={e => update('queryExpansionProfile', e.target.value)}
                >
                  <option value="none">None</option>
                  <option value="basic">Basic</option>
                  <option value="advanced">Advanced</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Hybrid RRF K</label>
                <Input type="number" value={settings.hybridRrfK} onChange={e => update('hybridRrfK', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Candidate ×</label>
                <Input type="number" value={settings.candidateMultiplier} onChange={e => update('candidateMultiplier', Number(e.target.value))} className="h-8" />
              </div>
            </div>
            <div className="flex items-center gap-4 text-xs">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={settings.showHighlights} onChange={e => update('showHighlights', e.target.checked)} className="rounded" />
                Highlights
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={settings.useE5Prefix} onChange={e => update('useE5Prefix', e.target.checked)} className="rounded" />
                E5 Prefix
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={settings.excludeReference} onChange={e => update('excludeReference', e.target.checked)} className="rounded" />
                Exclude Reference
              </label>
              <Button variant="ghost" size="sm" className="h-6 text-xs ml-auto" onClick={handleReset}>
                <RotateCcw className="h-3 w-3 mr-1" />
                Reset to defaults
              </Button>
            </div>
          </div>
        )}
      </div>

      {searchError && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
          <AlertCircle className="h-4 w-4 text-destructive shrink-0" />
          <span className="text-destructive">{searchError}</span>
        </div>
      )}

      {searching && (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[120px] rounded-lg" />
          ))}
        </div>
      )}

      {searched && !searching && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">
              {results.length} results · {settings.searchMethod} · α={settings.alpha}
              {selectedTags.length > 0 ? ` · tags: ${selectedTags.join(', ')}` : ''}
            </span>
            <button
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
              onClick={() => setShowCandidates(!showCandidates)}
            >
              <Code className="h-3.5 w-3.5" />
              {showCandidates ? 'Hide' : 'Show'} candidate hits
            </button>
          </div>

          {results.length > 0 ? (
            results.map((result, i) => (
              <div key={i} className="panel p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-lg font-semibold font-serif text-foreground">{Number(result._score || 0).toFixed(2)}</span>
                    <span className="text-sm font-medium text-primary cursor-pointer hover:underline">{getSearchResultTitle(result)}</span>
                  </div>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span>Chunk {result.chunk_num}</span>
                    <span>·</span>
                    <span>Pages {result.page_start}–{result.page_end}</span>
                  </div>
                </div>
                <p className="text-sm text-foreground/80 leading-relaxed">
                  {highlightSearchSnippet(getSearchResultSnippet(result), getSearchHighlights(result)).map((part, index) => (
                    part.highlighted
                      ? <mark key={`${part.text}-${index}`} className="bg-warning/20 text-foreground px-0.5 rounded">{part.text}</mark>
                      : <React.Fragment key={`${part.text}-${index}`}>{part.text}</React.Fragment>
                  ))}
                </p>
                {parseDomainTagsField(result.domain_tags).length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {parseDomainTagsField(result.domain_tags).map(tag => (
                      <Badge key={tag} variant="outline" className="text-[10px]">{tag}</Badge>
                    ))}
                  </div>
                )}
                {settings.showHighlights && getSearchHighlights(result).length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {getSearchHighlights(result).map((h, j) => (
                      <Badge key={j} variant="secondary" className="text-xs">{h}</Badge>
                    ))}
                  </div>
                )}
              </div>
            ))
          ) : (
            <div className="panel p-16 text-center">
              <SearchIcon className="h-8 w-8 mx-auto mb-3 text-muted-foreground/30" />
              <p className="text-sm font-medium text-foreground">No results found</p>
              <p className="text-xs text-muted-foreground mt-1">Try adjusting your query or search settings</p>
            </div>
          )}

          {showCandidates && (
            <div className="panel">
              <div className="panel-header">
                <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Candidate Hits</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border text-left">
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Rank</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Search Score</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Method Score</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Method</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Chunk ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {candidates.map((c, idx) => (
                      <tr key={getCandidateHitId(c) || idx} className="border-b border-border">
                        <td className="px-4 py-2">{getCandidateRank(c, idx)}</td>
                        <td className="px-4 py-2 font-mono">{Number(c._score || c.score || 0).toFixed(3)}</td>
                        <td className="px-4 py-2 font-mono text-muted-foreground">{Number(c.raw_score ?? c._score ?? c.score ?? 0).toFixed(3)}</td>
                        <td className="px-4 py-2">
                          <Badge variant="secondary" className="text-[10px]">{summarizeCandidateMethod(c)}</Badge>
                        </td>
                        <td className="px-4 py-2 font-mono text-muted-foreground">{getCandidateHitId(c)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {!searched && !searching && (
        <div className="text-center py-16 text-muted-foreground">
          <SearchIcon className="h-10 w-10 mx-auto mb-3 opacity-30" />
          <p className="text-sm">Enter a query to search the pipeline index</p>
          <p className="text-xs mt-1 text-muted-foreground/70">
            Using <strong>{settings.searchMethod}</strong> on <strong>{settings.indexName}</strong>
          </p>
        </div>
      )}
    </div>
  )
}
