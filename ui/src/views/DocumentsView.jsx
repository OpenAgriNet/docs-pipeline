import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Checkbox } from '../components/ui/checkbox'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import { StageBadge } from '../components/StageBadge'
import { Pagination } from '../components/Pagination'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../components/ui/alert-dialog'
import { fetchAllDocuments, fetchJson, formatCompactDateTime, getDocumentListLabel, getDocumentMetaLabel, getStageLabel } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'
import { Search, FileText, AlertTriangle, RefreshCw, Loader2, Tag, EyeOff } from 'lucide-react'

const PAGE_SIZE = 10
const BULK_AUTO_TAG_MAX = 25

export default function DocumentsView() {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const canUpload = hasPermission('upload')
  const canReview = hasPermission('review')
  const canAdmin = hasPermission('admin')
  const [documents, setDocuments] = useState([])
  const [query, setQuery] = useState('')
  const [stageFilter, setStageFilter] = useState('all')
  const [authFilter, setAuthFilter] = useState('all')
  const [showFailed, setShowFailed] = useState(false)
  const [showReindex, setShowReindex] = useState(false)
  const [showDisabled, setShowDisabled] = useState(false)
  const [queryFilter, setQueryFilter] = useState('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [selectedRow, setSelectedRow] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [confirmAutoTag, setConfirmAutoTag] = useState(false)
  const [autoTagging, setAutoTagging] = useState(false)
  const [bulkMessage, setBulkMessage] = useState('')

  useEffect(() => {
    load()
  }, [showDisabled])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const docs = await fetchAllDocuments({ includeDisabled: showDisabled && canAdmin })
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
      if (!showDisabled && doc.is_disabled) return false
      if (queryFilter === 'on' && doc.query_enabled === false) return false
      if (queryFilter === 'off' && doc.query_enabled !== false) return false
      return true
    })
  }, [documents, query, stageFilter, authFilter, showFailed, showReindex, showDisabled, queryFilter])

  useEffect(() => {
    setPage(1)
  }, [query, stageFilter, authFilter, showFailed, showReindex, showDisabled, queryFilter])

  // Drop selections that disappeared after reload / filter change.
  useEffect(() => {
    const visible = new Set(filtered.map(doc => doc.workflow_id))
    setSelected(prev => {
      const next = new Set([...prev].filter(id => visible.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [filtered])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const paginated = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  const pageIds = paginated.map(doc => doc.workflow_id)
  const allPageSelected = pageIds.length > 0 && pageIds.every(id => selected.has(id))
  const activeFilters = [
    stageFilter !== 'all' && `Stage: ${getStageLabel(stageFilter, { compact: true })}`,
    authFilter !== 'all' && `Type: ${authFilter}`,
    showFailed && 'Failed only',
    showReindex && 'Reindex required',
    showDisabled && 'Including deleted',
    queryFilter !== 'all' && `Queries: ${queryFilter}`,
  ].filter(Boolean)

  function toggleSelect(id) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function togglePage() {
    setSelected(prev => {
      const next = new Set(prev)
      if (allPageSelected) {
        pageIds.forEach(id => next.delete(id))
      } else {
        pageIds.forEach(id => next.add(id))
      }
      return next
    })
  }

  async function runBulkAutoTag() {
    const ids = [...selected]
    if (!ids.length) return
    if (ids.length > BULK_AUTO_TAG_MAX) {
      setBulkMessage(`Select at most ${BULK_AUTO_TAG_MAX} documents per auto-tag run.`)
      setConfirmAutoTag(false)
      return
    }
    setAutoTagging(true)
    setBulkMessage('')
    setError('')
    try {
      const result = await fetchJson('/documents/bulk/auto-tag', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workflow_ids: ids }),
      })
      const failed = (result.results || []).filter(r => !r.ok)
      setBulkMessage(
        `Auto-tag finished: ${result.succeeded || 0} succeeded, ${result.failed || 0} failed`
        + (failed.length
          ? ` — ${failed.slice(0, 3).map(f => `${f.workflow_id}: ${f.message}`).join('; ')}`
          : '')
      )
      setSelected(new Set())
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setAutoTagging(false)
      setConfirmAutoTag(false)
    }
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Documents</h1>
          <p className="text-sm text-muted-foreground mt-1">{documents.length} documents in pipeline</p>
        </div>
        {canUpload && (
          <Button onClick={() => navigate('/ingest')}>
            <FileText className="h-4 w-4 mr-2" />
            Ingest New
          </Button>
        )}
      </div>

      {error ? (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
          <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" />
          <span>{error}</span>
        </div>
      ) : null}

      {bulkMessage ? (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-muted/50 border border-border text-sm">
          <Tag className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span>{bulkMessage}</span>
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
            {stageOptions.filter(s => s !== 'all').map(stage => (
              <SelectItem key={stage} value={stage}>{getStageLabel(stage, { compact: true })}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={authFilter} onValueChange={setAuthFilter}>
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="Type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Types</SelectItem>
            <SelectItem value="authoritative">Authoritative</SelectItem>
            <SelectItem value="legacy">Legacy</SelectItem>
          </SelectContent>
        </Select>
        <Select value={queryFilter} onValueChange={setQueryFilter}>
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="Queries" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All query states</SelectItem>
            <SelectItem value="on">Queries on</SelectItem>
            <SelectItem value="off">Queries off</SelectItem>
          </SelectContent>
        </Select>
        <Button variant={showFailed ? 'destructive' : 'outline'} size="sm" onClick={() => { setShowFailed(!showFailed); setShowReindex(false) }}>
          <AlertTriangle className="h-3.5 w-3.5 mr-1.5" />
          Failed
        </Button>
        <Button
          variant={showReindex ? 'secondary' : 'outline'}
          size="sm"
          onClick={() => setShowReindex(v => !v)}
        >
          Reindex
        </Button>
        <Button variant="ghost" size="icon" onClick={load} disabled={loading} title="Refresh">
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
        </Button>
        {canAdmin && (
          <Button variant={showDisabled ? 'secondary' : 'outline'} size="sm" onClick={() => setShowDisabled(!showDisabled)}>
            <EyeOff className="h-3.5 w-3.5 mr-1.5" />
            Deleted
          </Button>
        )}
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
              setQueryFilter('all')
              setShowFailed(false)
              setShowReindex(false)
              setShowDisabled(false)
              setQuery('')
            }}
          >
            Clear all
          </button>
        </div>
      )}

      {canReview && selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-accent/30 px-3 py-2">
          <span className="text-sm font-medium text-foreground">{selected.size} selected</span>
          <Button
            size="sm"
            variant="outline"
            disabled={autoTagging || selected.size > BULK_AUTO_TAG_MAX}
            onClick={() => setConfirmAutoTag(true)}
          >
            {autoTagging ? (
              <><Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />Tagging…</>
            ) : (
              <><Tag className="h-3.5 w-3.5 mr-1.5" />Auto-tag selected</>
            )}
          </Button>
          {selected.size > BULK_AUTO_TAG_MAX && (
            <span className="text-xs text-destructive">Max {BULK_AUTO_TAG_MAX} documents per run</span>
          )}
          <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
        </div>
      )}

      <div className="rounded-lg border border-border overflow-hidden bg-card">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40">
                {canReview && (
                  <th className="px-3 py-3 w-10">
                    <Checkbox
                      checked={allPageSelected}
                      onCheckedChange={togglePage}
                      aria-label="Select all on page"
                      disabled={!pageIds.length}
                    />
                  </th>
                )}
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Document</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Stage</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Type</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Pages</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Chunks</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
                <th className="px-4 py-3 text-left font-medium text-muted-foreground">Updated</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i} className="border-b border-border">
                    <td colSpan={canReview ? 8 : 7} className="px-4 py-3">
                      <Skeleton className="h-8 w-full" />
                    </td>
                  </tr>
                ))
              ) : paginated.length > 0 ? (
                paginated.map(doc => (
                  <tr
                    key={doc.workflow_id}
                    className={`data-table-row cursor-pointer ${selectedRow === doc.workflow_id || selected.has(doc.workflow_id) ? 'bg-accent' : ''} ${doc.is_disabled || doc.query_enabled === false ? 'opacity-70' : ''}`}
                    onClick={() => {
                      setSelectedRow(doc.workflow_id)
                      navigate(`/documents/${doc.workflow_id}`)
                    }}
                  >
                    {canReview && (
                      <td
                        className="px-3 py-3"
                        onClick={e => e.stopPropagation()}
                        onKeyDown={e => e.stopPropagation()}
                      >
                        <Checkbox
                          checked={selected.has(doc.workflow_id)}
                          onCheckedChange={() => toggleSelect(doc.workflow_id)}
                          aria-label={`Select ${getDocumentListLabel(doc)}`}
                        />
                      </td>
                    )}
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
                        {doc.is_disabled && <Badge variant="destructive">Deleted</Badge>}
                        {doc.query_enabled === false && !doc.is_disabled && <Badge variant="secondary">Queries off</Badge>}
                        {(doc.failed || doc.stage === 'failed') && <Badge variant="destructive">Failed</Badge>}
                        {doc.reindex_required && <Badge variant="warning">Reindex</Badge>}
                        {(doc.stage?.includes('processing') || doc.stage === 'chunking' || doc.stage === 'ingesting') && (
                          <Badge variant="info"><Loader2 className="h-3 w-3 mr-1 animate-spin" />Processing</Badge>
                        )}
                        {!doc.is_disabled && doc.query_enabled !== false && !doc.reindex_required && !(doc.failed || doc.stage === 'failed') && !(doc.stage?.includes('processing') || doc.stage === 'chunking' || doc.stage === 'ingesting') && (
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
                  <td colSpan={canReview ? 8 : 7} className="px-4 py-16 text-center">
                    <Search className="h-8 w-8 mx-auto mb-3 text-muted-foreground/30" />
                    <p className="text-sm text-muted-foreground">
                      {query ? `No documents matching "${query}"` : 'No documents match the current filters.'}
                    </p>
                    <button
                      className="text-xs text-primary hover:underline mt-2"
                      onClick={() => {
                        setStageFilter('all')
                        setAuthFilter('all')
                        setQueryFilter('all')
                        setShowFailed(false)
                        setShowReindex(false)
                        setShowDisabled(false)
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
            <Pagination currentPage={page} totalPages={totalPages} onPageChange={setPage} />
          </div>
        )}
      </div>

      <AlertDialog open={confirmAutoTag} onOpenChange={setConfirmAutoTag}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Auto-tag selected documents?</AlertDialogTitle>
            <AlertDialogDescription>
              This re-runs domain auto-tagging on <strong>all chunks</strong> in {selected.size} document
              {selected.size === 1 ? '' : 's'}. Manual tags are kept; auto tags are replaced.
              Documents without chunks are skipped. Max {BULK_AUTO_TAG_MAX} per run.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={autoTagging}>Cancel</AlertDialogCancel>
            <AlertDialogAction disabled={autoTagging} onClick={e => { e.preventDefault(); runBulkAutoTag() }}>
              {autoTagging ? 'Tagging…' : 'Auto-tag'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
