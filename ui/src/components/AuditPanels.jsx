import React, { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, ClipboardList, ExternalLink } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { EmptyState } from './EmptyState'
import { Pagination } from './Pagination'
import { fetchJson, formatCompactDateTime, summarizeAuditAction } from '../lib/pipelineUi'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card'
import { Skeleton } from './ui/skeleton'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from './ui/select'

function formatValue(jsonStr) {
  if (!jsonStr) return '(empty)'
  try {
    const parsed = JSON.parse(jsonStr)
    if (typeof parsed === 'string') return parsed
    if (typeof parsed === 'boolean') return parsed ? 'Yes' : 'No'
    return JSON.stringify(parsed, null, 2)
  } catch {
    return jsonStr
  }
}

function getDescription(log) {
  if (log.action_type === 'stage_change') return `${formatValue(log.old_value)} -> ${formatValue(log.new_value)}`
  if (log.action_type === 'approval') {
    const meta = log.metadata ? JSON.parse(log.metadata) : {}
    return `Approved at ${meta.stage || 'unknown stage'}`
  }
  if (log.entity_type && log.entity_id) return `${log.entity_type} #${log.entity_id}: ${log.field_name || ''}`
  return log.field_name || ''
}

const actionVariant = {
  stage_change: 'info',
  page_edit: 'warning',
  chunk_edit: 'warning',
  approval: 'success',
  page_reset: 'secondary',
  chunk_reset: 'secondary',
  mark_reindex_required: 'warning',
  clear_reindex_required: 'success'
}

function AuditEntry({ log, global = false, onNavigate }) {
  const [expanded, setExpanded] = useState(false)
  const hasDetails = log.old_value || log.new_value || log.metadata
  const docDisplay = log.filename || log.document_id || log.workflow_id
  const description = getDescription(log)

  return (
    <div className="px-4 py-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={actionVariant[log.action_type] || 'secondary'} className="text-xs capitalize whitespace-nowrap">
              {summarizeAuditAction(log.action_type)}
            </Badge>
            {global && docDisplay ? (
              <Button variant="link" size="sm" className="h-auto p-0 text-sm" onClick={() => onNavigate(log.workflow_id)}>
                {docDisplay}
              </Button>
            ) : null}
            {log.actor ? <span className="text-xs text-muted-foreground">{log.actor}</span> : null}
            <span className="text-xs text-muted-foreground">{formatCompactDateTime(log.timestamp)}</span>
          </div>
          <div className="mt-2 text-sm leading-relaxed text-foreground">{description}</div>
          {log.field_name ? <div className="mt-1 text-xs text-muted-foreground">Field: {log.field_name}</div> : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {global && log.workflow_id ? (
            <Button variant="ghost" size="icon" className="size-7" onClick={() => onNavigate(log.workflow_id)} title="Open document">
              <ExternalLink className="h-3.5 w-3.5" />
            </Button>
          ) : null}
          {hasDetails ? (
            <Button variant="ghost" size="icon" className="size-7" onClick={() => setExpanded(value => !value)} title="Toggle details">
              {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </Button>
          ) : null}
        </div>
      </div>

      {expanded ? (
        <div className="mt-3 flex flex-col gap-2">
          {log.metadata ? (
            <Card className="border-border/80 bg-muted/40 shadow-none">
              <CardContent className="p-3">
                <span className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-muted-foreground">Details</span>
                <pre className="whitespace-pre-wrap text-xs text-muted-foreground">{formatValue(log.metadata)}</pre>
              </CardContent>
            </Card>
          ) : null}
          {log.old_value || log.new_value ? (
            <div className="grid gap-2 md:grid-cols-2">
              <Card className="border-destructive/20 bg-destructive/5 shadow-none">
                <CardContent className="p-3">
                  <span className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-destructive">Before</span>
                  <pre className="whitespace-pre-wrap text-xs text-muted-foreground">{formatValue(log.old_value)}</pre>
                </CardContent>
              </Card>
              <Card className="border-success/20 bg-success/5 shadow-none">
                <CardContent className="p-3">
                  <span className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-success">After</span>
                  <pre className="whitespace-pre-wrap text-xs text-muted-foreground">{formatValue(log.new_value)}</pre>
                </CardContent>
              </Card>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

export function DocumentAuditLog({ workflowId }) {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [total, setTotal] = useState(0)

  useEffect(() => {
    fetchLogs()
  }, [workflowId, filter])

  async function fetchLogs() {
    setLoading(true)
    try {
      const url = filter === 'all'
        ? `/documents/${workflowId}/audit?limit=100`
        : `/documents/${workflowId}/audit?action_type=${filter}&limit=100`
      const data = await fetchJson(url)
      setLogs(data.logs || [])
      setTotal(data.total || 0)
    } catch (error) {
      setLogs([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-4 border-b border-border/80 pb-4">
        <div>
          <CardTitle className="font-sans text-base font-medium">Audit Trail</CardTitle>
          <CardDescription>{total} entries for this document</CardDescription>
        </div>
        <Select value={filter} onValueChange={setFilter}>
          <SelectTrigger className="w-[210px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="all">All Actions</SelectItem>
              <SelectItem value="stage_change">Stage Changes</SelectItem>
              <SelectItem value="page_edit">Page Edits</SelectItem>
              <SelectItem value="chunk_edit">Chunk Edits</SelectItem>
              <SelectItem value="approval">Approvals</SelectItem>
              <SelectItem value="page_reset">Page Resets</SelectItem>
              <SelectItem value="chunk_reset">Chunk Resets</SelectItem>
              <SelectItem value="mark_reindex_required">Mark Reindex Required</SelectItem>
              <SelectItem value="clear_reindex_required">Clear Reindex Required</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent className="p-0">
        {loading ? (
          <div className="flex flex-col gap-2 p-4">
            {Array.from({ length: 4 }).map((_, index) => <Skeleton key={index} className="h-16 rounded-lg" />)}
          </div>
        ) : logs.length === 0 ? (
          <EmptyState icon={ClipboardList} title="No audit entries found for this document." />
        ) : (
          <div className="divide-y divide-border">
            {logs.map(log => <AuditEntry key={log.id} log={log} />)}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function GlobalAuditLogPanel() {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const limit = 8
  const navigate = useNavigate()

  useEffect(() => {
    fetchLogs()
  }, [filter, offset])

  async function fetchLogs() {
    setLoading(true)
    try {
      const url = filter === 'all'
        ? `/audit?limit=${limit}&offset=${offset}`
        : `/audit?action_type=${filter}&limit=${limit}&offset=${offset}`
      const data = await fetchJson(url)
      setLogs(data.logs || [])
      setTotal(data.total || 0)
    } catch (error) {
      setLogs([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / limit))
  const currentPage = Math.floor(offset / limit) + 1

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-medium text-foreground">Global Audit Log</div>
          <div className="mt-1 text-xs text-muted-foreground">Track edits, resets, approvals, and stage changes across the whole pipeline.</div>
        </div>
        <Select value={filter} onValueChange={value => { setFilter(value); setOffset(0) }}>
          <SelectTrigger className="w-[210px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="all">All Actions</SelectItem>
              <SelectItem value="stage_change">Stage Changes</SelectItem>
              <SelectItem value="page_edit">Page Edits</SelectItem>
              <SelectItem value="chunk_edit">Chunk Edits</SelectItem>
              <SelectItem value="approval">Approvals</SelectItem>
              <SelectItem value="page_reset">Page Resets</SelectItem>
              <SelectItem value="chunk_reset">Chunk Resets</SelectItem>
              <SelectItem value="mark_reindex_required">Mark Reindex Required</SelectItem>
              <SelectItem value="clear_reindex_required">Clear Reindex Required</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </div>

      <Card>
        <CardContent className="p-0">
          {loading ? (
            <div className="flex flex-col gap-2 p-4">
              {Array.from({ length: 5 }).map((_, index) => <Skeleton key={index} className="h-16 rounded-lg" />)}
            </div>
          ) : logs.length === 0 ? (
            <EmptyState icon={ClipboardList} title="No audit entries match the current filter." />
          ) : (
            <div className="divide-y divide-border">
              {logs.map(log => (
                <AuditEntry key={log.id} log={log} global onNavigate={workflowId => navigate(`/documents/${workflowId}`)} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {total > limit ? (
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            Showing {offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <Pagination
            currentPage={currentPage}
            totalPages={totalPages}
            onPageChange={pageNumber => setOffset((pageNumber - 1) * limit)}
          />
        </div>
      ) : null}
    </div>
  )
}
