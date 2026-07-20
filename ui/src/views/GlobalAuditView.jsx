import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Skeleton } from '../components/ui/skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import { fetchJson, formatCompactDateTime, summarizeAuditAction } from '../lib/pipelineUi'
import { ChevronDown, ChevronUp, ChevronLeft, ChevronRight, ClipboardList, ExternalLink } from 'lucide-react'

const actionColors = {
  stage_change: 'info',
  page_edit: 'warning',
  chunk_edit: 'warning',
  approval: 'success',
  page_reset: 'secondary',
  chunk_reset: 'secondary',
  mark_reindex_required: 'warning',
  clear_reindex_required: 'success',
}

const PAGE_SIZE = 8

export default function GlobalAuditView() {
  const navigate = useNavigate()
  const [filter, setFilter] = useState('all')
  const [expanded, setExpanded] = useState(new Set())
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [logs, setLogs] = useState([])
  const [total, setTotal] = useState(0)

  useEffect(() => {
    setPage(1)
  }, [filter])

  useEffect(() => {
    fetchLogs()
  }, [filter, page])

  async function fetchLogs() {
    setLoading(true)
    try {
      const offset = (page - 1) * PAGE_SIZE
      const url = filter === 'all'
        ? `/audit?limit=${PAGE_SIZE}&offset=${offset}`
        : `/audit?action_type=${filter}&limit=${PAGE_SIZE}&offset=${offset}`
      const data = await fetchJson(url)
      setLogs(data.logs || [])
      setTotal(data.total || 0)
    } catch {
      setLogs([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const toggleExpand = (id) => {
    const next = new Set(expanded)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setExpanded(next)
  }

  if (loading && logs.length === 0) {
    return (
      <div className="p-6 max-w-5xl mx-auto space-y-4">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-4 w-48 mt-1" />
        <Skeleton className="h-[400px] rounded-lg mt-4" />
      </div>
    )
  }

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Audit Log</h1>
          <p className="text-sm text-muted-foreground mt-1">Change provenance across the system</p>
        </div>
        <Select value={filter} onValueChange={setFilter}>
          <SelectTrigger className="w-[200px]">
            <SelectValue placeholder="Filter by action" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Actions ({total})</SelectItem>
            <SelectItem value="stage_change">{summarizeAuditAction('stage_change')}</SelectItem>
            <SelectItem value="page_edit">{summarizeAuditAction('page_edit')}</SelectItem>
            <SelectItem value="chunk_edit">{summarizeAuditAction('chunk_edit')}</SelectItem>
            <SelectItem value="approval">{summarizeAuditAction('approval')}</SelectItem>
            <SelectItem value="page_reset">{summarizeAuditAction('page_reset')}</SelectItem>
            <SelectItem value="chunk_reset">{summarizeAuditAction('chunk_reset')}</SelectItem>
            <SelectItem value="mark_reindex_required">{summarizeAuditAction('mark_reindex_required')}</SelectItem>
            <SelectItem value="clear_reindex_required">{summarizeAuditAction('clear_reindex_required')}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="panel divide-y divide-border">
        {logs.length > 0 ? logs.map(entry => (
          <div key={entry.id} className="px-4 py-3">
            <div className="flex items-center gap-3">
              <Badge variant={actionColors[entry.action_type] || 'secondary'} className="text-xs capitalize whitespace-nowrap">
                {summarizeAuditAction(entry.action_type)}
              </Badge>
              <div className="flex-1 min-w-0">
                {(entry.filename || entry.document_id) && (
                  <span
                    className="text-sm text-primary hover:underline cursor-pointer"
                    onClick={() => navigate(`/documents/${entry.workflow_id}`)}
                  >
                    {entry.filename || entry.document_id}
                  </span>
                )}
              </div>
              <span className="text-xs text-muted-foreground whitespace-nowrap">{entry.actor}</span>
              <span className="text-xs text-muted-foreground whitespace-nowrap">
                {formatCompactDateTime(entry.timestamp)}
              </span>
              {entry.workflow_id && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 shrink-0"
                  onClick={() => navigate(`/documents/${entry.workflow_id}`)}
                  title="Open document"
                >
                  <ExternalLink className="h-3 w-3" />
                </Button>
              )}
              <button
                className="text-muted-foreground hover:text-foreground transition-colors shrink-0"
                onClick={() => toggleExpand(entry.id)}
              >
                {expanded.has(entry.id) ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
              </button>
            </div>
            {expanded.has(entry.id) && (
              <div className="mt-3 space-y-2">
                {entry.metadata && (
                  <div className="p-3 rounded-md bg-muted/50">
                    <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider block mb-1">Details</span>
                    <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap">
                      {typeof entry.metadata === 'string' ? entry.metadata : JSON.stringify(entry.metadata, null, 2)}
                    </pre>
                  </div>
                )}
                {(entry.old_value || entry.new_value) && (
                  <div className="grid grid-cols-2 gap-2">
                    <div className="p-3 rounded-md bg-destructive/5 border border-destructive/10">
                      <span className="text-[10px] font-medium text-destructive uppercase tracking-wider block mb-1">Before</span>
                      <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap">
                        {entry.old_value || '(empty)'}
                      </pre>
                    </div>
                    <div className="p-3 rounded-md bg-success/5 border border-success/10">
                      <span className="text-[10px] font-medium text-success uppercase tracking-wider block mb-1">After</span>
                      <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap">
                        {entry.new_value || '(empty)'}
                      </pre>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )) : (
          <div className="px-4 py-16 text-center">
            <ClipboardList className="h-8 w-8 mx-auto mb-3 text-muted-foreground/30" />
            <p className="text-sm text-muted-foreground">No audit entries match the current filter.</p>
          </div>
        )}
      </div>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total}
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
  )
}
