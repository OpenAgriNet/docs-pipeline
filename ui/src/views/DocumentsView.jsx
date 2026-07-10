import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import { StageBadge } from '../components/StageBadge'
import { fetchAllDocuments, formatCompactDateTime, getDocumentListLabel, getDocumentMetaLabel, getStageLabel } from '../lib/pipelineUi'
import { Search, FileText, AlertTriangle, RefreshCw, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react'

const PAGE_SIZE = 10

export default function DocumentsView() {
  const navigate = useNavigate()
  const [documents, setDocuments] = useState([])
  const [query, setQuery] = useState('')
  const [stageFilter, setStageFilter] = useState('all')
  const [authFilter, setAuthFilter] = useState('all')
  const [showFailed, setShowFailed] = useState(false)
  const [showReindex, setShowReindex] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [selectedRow, setSelectedRow] = useState(null)

  useEffect(() => {
    load()
  }, [])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const docs = await fetchAllDocuments()
      setDocuments(docs)
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  const stageOptions = useMemo(() => ['all', ...new Set(documents.map(doc => doc.stage))], [documents])

  const filtered = useMemo(() => {
    return documents.filter(doc => {
      const q = query.trim().toLowerCase()
      if (q && ![doc.filename, getDocumentListLabel(doc), doc.workflow_id].some(v => `${v || ''}`.toLowerCase().includes(q))) return false
      if (stageFilter !== 'all' && doc.stage !== stageFilter) return false
      if (authFilter === 'authoritative' && !doc.authoritative) return false
      if (authFilter === 'legacy' && doc.authoritative) return false
      if (showFailed && !doc.failed && doc.stage !== 'failed') return false
      if (showReindex && !doc.reindex_required) return false
      return true
    })
  }, [documents, query, stageFilter, authFilter, showFailed, showReindex])

  useEffect(() => {
    setPage(1)
  }, [query, stageFilter, authFilter, showFailed, showReindex])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const paginated = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  const activeFilters = [
    stageFilter !== 'all' && `Stage: ${getStageLabel(stageFilter, { compact: true })}`,
    authFilter !== 'all' && `Type: ${authFilter}`,
    showFailed && 'Failed only',
    showReindex && 'Reindex required',
  ].filter(Boolean)

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Documents</h1>
          <p className="text-sm text-muted-foreground mt-1">{documents.length} documents in pipeline</p>
        </div>
        <Button onClick={() => navigate('/ingest')}>
          <FileText className="h-4 w-4 mr-2" />
          Ingest New
        </Button>
      </div>

      {error ? (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
          <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" />
          <span>{error}</span>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search filename, name, or workflow ID..."
            value={query}
            onChange={e => setQuery(e.target.value)}
            className="pl-9"
          />
        </div>
        <Select value={stageFilter} onValueChange={setStageFilter}>
          <SelectTrigger className="w-[160px]">
            <SelectValue placeholder="Stage" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Stages</SelectItem>
                    {stageOptions.filter(s => s !== 'all').map(s => <SelectItem key={s} value={s}>{getStageLabel(s, { compact: true })}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={authFilter} onValueChange={setAuthFilter}>
          <SelectTrigger className="w-[160px]">
            <SelectValue placeholder="Authority" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All</SelectItem>
            <SelectItem value="authoritative">Authoritative</SelectItem>
            <SelectItem value="legacy">Legacy</SelectItem>
          </SelectContent>
        </Select>
        <Button variant={showFailed ? 'destructive' : 'outline'} size="sm" onClick={() => { setShowFailed(!showFailed); setShowReindex(false) }}>
          <AlertTriangle className="h-3.5 w-3.5 mr-1.5" />
          Failed
        </Button>
        <Button variant={showReindex ? 'warning' : 'outline'} size="sm" onClick={() => { setShowReindex(!showReindex); setShowFailed(false) }}>
          <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
          Reindex
        </Button>
      </div>

      {activeFilters.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs text-muted-foreground">Active filters:</span>
          {activeFilters.map((f, i) => <Badge key={i} variant="secondary" className="text-xs">{f}</Badge>)}
          <button
            className="text-xs text-primary hover:underline"
            onClick={() => {
              setStageFilter('all')
              setAuthFilter('all')
              setShowFailed(false)
              setShowReindex(false)
              setQuery('')
            }}
          >
            Clear all
          </button>
        </div>
      )}

      <div className="panel">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Document</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Stage</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Type</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Pages</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Chunks</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Status</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Updated</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                Array.from({ length: 6 }).map((_, i) => (
                  <tr key={i} className="border-b border-border">
                    <td className="px-4 py-3"><Skeleton className="h-4 w-48" /><Skeleton className="h-3 w-24 mt-1.5" /></td>
                    <td className="px-4 py-3"><Skeleton className="h-5 w-20 rounded-full" /></td>
                    <td className="px-4 py-3"><Skeleton className="h-5 w-16 rounded-full" /></td>
                    <td className="px-4 py-3"><Skeleton className="h-4 w-8" /></td>
                    <td className="px-4 py-3"><Skeleton className="h-4 w-8" /></td>
                    <td className="px-4 py-3"><Skeleton className="h-5 w-16 rounded-full" /></td>
                    <td className="px-4 py-3"><Skeleton className="h-4 w-20" /></td>
                  </tr>
                ))
              ) : paginated.length > 0 ? (
                paginated.map(doc => (
                  <tr
                    key={doc.workflow_id}
                    className={`data-table-row cursor-pointer ${selectedRow === doc.workflow_id ? 'bg-accent' : ''}`}
                    onClick={() => {
                      setSelectedRow(doc.workflow_id)
                      navigate(`/documents/${doc.workflow_id}`)
                    }}
                  >
                    <td className="px-4 py-3 max-w-[280px]">
                      <div className="font-medium text-foreground truncate">{getDocumentListLabel(doc)}</div>
                      <div className="text-xs text-muted-foreground font-mono truncate">{getDocumentMetaLabel(doc)}</div>
                    </td>
                    <td className="px-4 py-3"><StageBadge stage={doc.stage} compact /></td>
                    <td className="px-4 py-3">
                      <Badge variant={doc.authoritative ? 'default' : 'secondary'}>
                        {doc.authoritative ? 'Auth' : 'Legacy'}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">{doc.page_count}</td>
                    <td className="px-4 py-3 text-muted-foreground">{doc.chunk_count}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {(doc.failed || doc.stage === 'failed') && <Badge variant="destructive">Failed</Badge>}
                        {doc.reindex_required && <Badge variant="warning">Reindex</Badge>}
                        {(doc.stage?.includes('processing') || doc.stage === 'chunking' || doc.stage === 'ingesting') && (
                          <Badge variant="info"><Loader2 className="h-3 w-3 mr-1 animate-spin" />Processing</Badge>
                        )}
                        {!doc.reindex_required && !(doc.failed || doc.stage === 'failed') && !(doc.stage?.includes('processing') || doc.stage === 'chunking' || doc.stage === 'ingesting') && (
                          <span className="text-xs text-muted-foreground">OK</span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                      {formatCompactDateTime(doc.updated_at || doc.created_at)}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={7} className="px-4 py-16 text-center">
                    <Search className="h-8 w-8 mx-auto mb-3 text-muted-foreground/30" />
                    <p className="text-sm text-muted-foreground">
                      {query ? `No documents matching "${query}"` : 'No documents match the current filters.'}
                    </p>
                    <button
                      className="text-xs text-primary hover:underline mt-2"
                      onClick={() => {
                        setStageFilter('all')
                        setAuthFilter('all')
                        setShowFailed(false)
                        setShowReindex(false)
                        setQuery('')
                      }}
                    >
                      Clear filters
                    </button>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {!loading && filtered.length > PAGE_SIZE && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-border">
            <span className="text-xs text-muted-foreground">
              Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, filtered.length)} of {filtered.length}
            </span>
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="icon" className="h-7 w-7" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>
                <ChevronLeft className="h-3.5 w-3.5" />
              </Button>
              {Array.from({ length: totalPages }).map((_, i) => (
                <button
                  key={i}
                  className={`h-7 w-7 rounded text-xs font-medium transition-colors ${
                    page === i + 1 ? 'bg-primary text-primary-foreground' : 'hover:bg-accent text-muted-foreground'
                  }`}
                  onClick={() => setPage(i + 1)}
                >
                  {i + 1}
                </button>
              ))}
              <Button variant="ghost" size="icon" className="h-7 w-7" disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
