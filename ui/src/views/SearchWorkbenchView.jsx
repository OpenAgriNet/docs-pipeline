import React, { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Link } from 'react-router-dom'
import { API_BASE } from '../config'
import { DEFAULT_SEARCH_SETTINGS, styles } from '../styles/appStyles'

export default function SearchWorkbenchView() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [settings, setSettings] = useState(DEFAULT_SEARCH_SETTINGS)
  const [searchTime, setSearchTime] = useState(null)
  const [settingsLoading, setSettingsLoading] = useState(true)
  const [searchMeta, setSearchMeta] = useState(null)
  const [includeRawHits, setIncludeRawHits] = useState(false)

  useEffect(() => {
    fetchSettings()
  }, [])

  async function fetchSettings() {
    try {
      const response = await fetch(`${API_BASE}/settings/search`)
      if (response.ok) setSettings(await response.json())
    } catch (error) {
      console.error('Failed to fetch search settings:', error)
    } finally {
      setSettingsLoading(false)
    }
  }

  async function handleSearch(event) {
    event.preventDefault()
    if (!query.trim()) return

    setLoading(true)
    setSearchTime(null)
    setSearchMeta(null)
    const startTime = performance.now()

    try {
      const response = await fetch(`${API_BASE}/marqo/search`, {
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
          include_raw_hits: includeRawHits
        })
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Search failed')
      setResults(data.hits || [])
      setSearchMeta(data)
      setSearchTime(performance.now() - startTime)
    } catch (error) {
      console.error('Search failed:', error)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={styles.container}>
      <section style={styles.pageHero}>
        <h2 style={styles.pageHeroTitle}>Marqo search workbench</h2>
        <p style={styles.pageHeroText}>Run operational search queries against the pipeline-managed index, expose retrieval knobs, and inspect candidate versus final result behavior without leaving the application.</p>
      </section>

      <div style={styles.card}>
        <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '16px', flexWrap: 'wrap' }}>
          <div>
            <h3 style={{ margin: 0 }}>Search runtime controls</h3>
            <p style={{ margin: '6px 0 0', color: '#64748b' }}>Defaults are loaded from backend settings and can be adjusted per query here.</p>
          </div>
          <Link to="/settings" style={{ ...styles.buttonSecondary, textDecoration: 'none' }}>Configure defaults</Link>
        </div>

        <form onSubmit={handleSearch}>
          <div style={{ ...styles.flex, alignItems: 'stretch' }}>
            <input style={{ ...styles.input, flex: 1, marginBottom: 0 }} value={query} onChange={event => setQuery(event.target.value)} placeholder="Search veterinary documents..." />
            <button type="submit" style={styles.button} disabled={loading || settingsLoading}>{loading ? 'Searching...' : 'Search'}</button>
          </div>
        </form>

        <div style={{ marginTop: '16px', display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px' }}>
          <select style={styles.input} value={settings.searchMethod} onChange={event => setSettings(prev => ({ ...prev, searchMethod: event.target.value }))}>
            <option value="HYBRID">HYBRID</option>
            <option value="TENSOR">TENSOR</option>
            <option value="LEXICAL">LEXICAL</option>
          </select>
          <input style={styles.input} value={settings.indexName} onChange={event => setSettings(prev => ({ ...prev, indexName: event.target.value }))} placeholder="Index name" />
          <input style={styles.input} type="number" value={settings.limit} onChange={event => setSettings(prev => ({ ...prev, limit: parseInt(event.target.value || '12', 10) }))} placeholder="Final top-k" />
          <input style={styles.input} type="number" value={settings.candidateCap} onChange={event => setSettings(prev => ({ ...prev, candidateCap: parseInt(event.target.value || '120', 10) }))} placeholder="Candidate cap" />
          <input style={styles.input} type="number" value={settings.candidateMultiplier} onChange={event => setSettings(prev => ({ ...prev, candidateMultiplier: parseInt(event.target.value || '10', 10) }))} placeholder="Candidate multiplier" />
          <input style={styles.input} type="number" value={settings.maxChunksPerDoc} onChange={event => setSettings(prev => ({ ...prev, maxChunksPerDoc: parseInt(event.target.value || '2', 10) }))} placeholder="Max chunks/doc" />
          <input style={styles.input} type="number" step="0.05" value={settings.alpha} onChange={event => setSettings(prev => ({ ...prev, alpha: parseFloat(event.target.value || '0.6') }))} placeholder="Alpha" />
          <input style={styles.input} type="number" value={settings.hybridRrfK} onChange={event => setSettings(prev => ({ ...prev, hybridRrfK: parseInt(event.target.value || '60', 10) }))} placeholder="Hybrid RRF k" />
          <select style={styles.input} value={settings.rankingMethod} onChange={event => setSettings(prev => ({ ...prev, rankingMethod: event.target.value }))}>
            <option value="rrf">rrf</option>
            <option value="normalize_linear">normalize_linear</option>
          </select>
          <select style={styles.input} value={settings.rerankMode} onChange={event => setSettings(prev => ({ ...prev, rerankMode: event.target.value }))}>
            <option value="none">none</option>
            <option value="bm25lite">bm25lite</option>
            <option value="rrf-lite">rrf-lite</option>
            <option value="heuristic">heuristic</option>
          </select>
          <input style={styles.input} type="number" value={settings.efSearch} onChange={event => setSettings(prev => ({ ...prev, efSearch: parseInt(event.target.value || '256', 10) }))} placeholder="efSearch" />
          <input style={styles.input} value={settings.queryExpansionProfile} onChange={event => setSettings(prev => ({ ...prev, queryExpansionProfile: event.target.value }))} placeholder="Expansion profile" />
        </div>

        <div style={{ ...styles.flex, marginTop: '8px', flexWrap: 'wrap' }}>
          <label><input type="checkbox" checked={settings.useE5Prefix} onChange={event => setSettings(prev => ({ ...prev, useE5Prefix: event.target.checked }))} /> E5 prefix</label>
          <label><input type="checkbox" checked={settings.excludeReference} onChange={event => setSettings(prev => ({ ...prev, excludeReference: event.target.checked }))} /> Exclude references</label>
          <label><input type="checkbox" checked={settings.showHighlights} onChange={event => setSettings(prev => ({ ...prev, showHighlights: event.target.checked }))} /> Show highlights</label>
          <label><input type="checkbox" checked={includeRawHits} onChange={event => setIncludeRawHits(event.target.checked)} /> Include raw hits</label>
        </div>

        <div style={{ marginTop: '12px', display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ background: settings.searchMethod === 'HYBRID' ? '#dbeafe' : settings.searchMethod === 'TENSOR' ? '#e0e7ff' : '#fef3c7', color: settings.searchMethod === 'HYBRID' ? '#1d4ed8' : settings.searchMethod === 'TENSOR' ? '#3730a3' : '#92400e', padding: '4px 8px', borderRadius: '999px', fontSize: '11px' }}>
            {settings.searchMethod}{settings.searchMethod === 'HYBRID' && ` (α=${settings.alpha})`}
          </span>
          <span style={{ background: '#f1f5f9', color: '#334155', padding: '4px 8px', borderRadius: '999px', fontSize: '11px' }}>{settings.limit} results</span>
          {searchTime && <span style={{ background: '#dcfce7', color: '#166534', padding: '4px 8px', borderRadius: '999px', fontSize: '11px' }}>{searchTime.toFixed(0)}ms</span>}
          {searchMeta && <span style={{ background: '#f1f5f9', color: '#334155', padding: '4px 8px', borderRadius: '999px', fontSize: '11px' }}>candidates {searchMeta.candidate_count} → final {searchMeta.final_count}</span>}
          {searchMeta?.effective_config?.query_expansion_applied && <span style={{ background: '#fef3c7', color: '#92400e', padding: '4px 8px', borderRadius: '999px', fontSize: '11px' }}>expansion applied</span>}
          {searchMeta?.effective_config?.rerank_mode && searchMeta?.effective_config?.rerank_mode !== 'none' && <span style={{ background: '#ede9fe', color: '#6d28d9', padding: '4px 8px', borderRadius: '999px', fontSize: '11px' }}>rerank {searchMeta.effective_config.rerank_mode}</span>}
        </div>
      </div>

      {results.length > 0 && (
        <div>
          <h3 style={{ margin: '24px 0 16px' }}>Results ({results.length})</h3>
          {results.map((hit, index) => (
            <div key={index} style={styles.card}>
              <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap' }}>
                <div style={styles.flex}>
                  <h4 style={{ margin: 0 }}>{hit.name_en || hit.name || hit.filename}</h4>
                  {hit.page_start && (
                    <span style={styles.pageIndicator}>
                      {hit.page_start === hit.page_end ? `Page ${hit.page_start}` : `Pages ${hit.page_start}-${hit.page_end}`}
                    </span>
                  )}
                </div>
                <span style={{ background: '#dbeafe', color: '#1d4ed8', padding: '4px 8px', borderRadius: '999px', fontSize: '12px' }}>Score: {(hit._score || 0).toFixed(3)}</span>
              </div>
              <div className="markdown-content" style={{ fontSize: '14px', lineHeight: 1.6, maxHeight: '300px', overflow: 'auto', padding: '12px', background: '#f8fafc', borderRadius: '10px' }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{hit.text}</ReactMarkdown>
              </div>
              <div style={{ marginTop: '12px', fontSize: '12px', color: '#64748b' }}>
                Chunk #{hit.chunk_num} · {hit.token_count} tokens · Source: {hit.source} · Index: {searchMeta?.effective_config?.index_name}
              </div>
              {hit._highlights?.[0] && (
                <div style={{ marginTop: '12px', padding: '12px', background: '#fffbeb', borderRadius: '10px', fontSize: '13px' }}>
                  <strong>Highlight:</strong>{' '}
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ p: ({ children }) => <span>{children}</span> }}>
                    {hit._highlights[0].text}
                  </ReactMarkdown>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {includeRawHits && searchMeta?.raw_hits?.length > 0 && (
        <div style={styles.card}>
          <h3 style={{ marginTop: 0 }}>Raw candidate hits</h3>
          <pre style={{ background: '#f8fafc', padding: '16px', borderRadius: '10px', overflow: 'auto', maxHeight: '420px', whiteSpace: 'pre-wrap' }}>
            {JSON.stringify(searchMeta.raw_hits, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
