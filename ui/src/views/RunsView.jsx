import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Skeleton } from '../components/ui/skeleton'
import { StageBadge } from '../components/StageBadge'
import { InstanceBadge } from '../components/InstanceBadge'
import PipelineStepper from '../components/PipelineStepper'
import {
  fetchJson,
  formatCompactDateTime,
  getDocumentListLabel,
  getDocumentFileLabel,
} from '../lib/pipelineUi'
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Clock,
  Loader2,
  Play,
  RefreshCw,
  XCircle,
} from 'lucide-react'
import { cn } from '../lib/utils'

const PAGE_SIZE = 8

function formatDuration(ms) {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  const seconds = Math.floor(ms / 1000)
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  if (!minutes) return `${seconds}s`
  return `${minutes}m ${remainder}s`
}

function formatJobType(type) {
  if (!type) return '—'
  return String(type)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

function StatusBadge({ status }) {
  const s = (status || '').toLowerCase()
  if (s === 'running') {
    return (
      <Badge variant="info" className="gap-1.5 whitespace-nowrap text-[10px] font-medium">
        <span className="size-1.5 animate-pulse rounded-full bg-current" />
        Running
      </Badge>
    )
  }
  if (s === 'completed' || s === 'success') {
    return (
      <Badge variant="success" className="gap-1 whitespace-nowrap text-[10px] font-medium">
        <CheckCircle2 className="size-3" />
        Completed
      </Badge>
    )
  }
  if (s === 'queued') {
    return (
      <Badge variant="secondary" className="whitespace-nowrap text-[10px] font-medium">
        Queued
      </Badge>
    )
  }
  if (s === 'waiting_review' || s === 'waiting-review' || s === 'review') {
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2.5 py-0.5',
          'border-amber-500/40 bg-amber-500/20 text-[10px] font-semibold text-amber-200',
          'dark:border-amber-400/45 dark:bg-amber-400/15 dark:text-amber-200',
        )}
      >
        <Clock className="size-3 shrink-0 text-amber-300" />
        Waiting for review
      </span>
    )
  }
  if (s === 'failed' || s === 'error') {
    return (
      <Badge variant="destructive" className="gap-1 whitespace-nowrap text-[10px] font-medium">
        <XCircle className="size-3" />
        Failed
      </Badge>
    )
  }
  return (
    <Badge variant="secondary" className="whitespace-nowrap text-[10px] font-medium capitalize">
      {status || '—'}
    </Badge>
  )
}

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'running', label: 'Running' },
  { id: 'queued', label: 'Queued' },
  { id: 'waiting_review', label: 'Waiting for review' },
  { id: 'failed', label: 'Failed' },
  { id: 'completed', label: 'Completed' },
]

export default function RunsView() {
  const navigate = useNavigate()
  const [filter, setFilter] = useState('all')
  const [runs, setRuns] = useState([])
  const [expanded, setExpanded] = useState(new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const pageSize = 100
      let offset = 0
      let allRuns = []
      while (true) {
        const query = new URLSearchParams({
          limit: String(pageSize),
          offset: String(offset),
        })
        const data = await fetchJson(`/runs?${query.toString()}`)
        const batch = Array.isArray(data) ? data : []
        allRuns = allRuns.concat(batch)
        if (batch.length < pageSize) break
        offset += pageSize
      }
      setRuns(allRuns)
    } catch (err) {
      setError(err.message || 'Failed to load runs')
      setRuns([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const normalizeStatus = (status) => {
    const s = String(status || '').toLowerCase().replace(/-/g, '_')
    if (s === 'success') return 'completed'
    if (s === 'review') return 'waiting_review'
    return s
  }

  const counts = useMemo(() => {
    const c = {
      all: runs.length,
      running: 0,
      queued: 0,
      waiting_review: 0,
      failed: 0,
      completed: 0,
    }
    for (const run of runs) {
      const s = normalizeStatus(run.status)
      if (s in c && s !== 'all') c[s] += 1
    }
    return c
  }, [runs])

  const filtered = useMemo(() => {
    if (filter === 'all') return runs
    return runs.filter((r) => normalizeStatus(r.status) === filter)
  }, [runs, filter])

  useEffect(() => {
    setPage(1)
  }, [filter])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const paginated = useMemo(() => {
    const safe = Math.min(page, totalPages)
    const start = (safe - 1) * PAGE_SIZE
    return filtered.slice(start, start + PAGE_SIZE)
  }, [filtered, page, totalPages])

  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages])

  function toggleExpand(id) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  if (loading) {
    return (
      <div className="flex h-full w-full flex-col gap-4 p-4 sm:p-5">
        <div>
          <Skeleton className="h-7 w-28" />
          <Skeleton className="mt-2 h-4 w-52" />
        </div>
        <Skeleton className="h-9 w-full max-w-lg rounded-lg" />
        <Skeleton className="min-h-[320px] flex-1 rounded-xl" />
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col gap-4 p-3 sm:p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-serif text-xl font-semibold tracking-tight text-foreground sm:text-2xl">
            Runs
          </h1>
          <p className="mt-0.5 text-xs text-muted-foreground sm:text-sm">
            Pipeline job history and status
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-9"
          onClick={load}
          disabled={loading}
        >
          <RefreshCw className={cn('mr-1.5 size-3.5', loading && 'animate-spin')} />
          Refresh
        </Button>
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2.5 text-sm text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {/* Equal-height chips; wrap to next line when they overflow */}
      <div
        className="flex w-full flex-wrap gap-1.5 rounded-xl border border-border bg-muted/50 p-1.5"
        role="tablist"
        aria-label="Filter runs by status"
      >
        {FILTERS.map((f) => {
          const count = counts[f.id] ?? 0
          const active = filter === f.id
          return (
            <button
              key={f.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setFilter(f.id)}
              className={cn(
                // Same size for every chip
                'inline-flex h-9 min-w-[7.5rem] flex-1 items-center justify-center gap-1.5',
                'rounded-lg px-3 text-xs font-medium whitespace-nowrap transition-all sm:flex-none sm:min-w-[8.5rem]',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
                active
                  ? 'bg-card text-foreground shadow-sm ring-1 ring-border'
                  : 'bg-transparent text-muted-foreground hover:bg-card/70 hover:text-foreground',
              )}
            >
              <span className="truncate">{f.label}</span>
              <span
                className={cn(
                  'inline-flex h-5 min-w-[1.25rem] shrink-0 items-center justify-center rounded-md px-1.5 text-[10px] font-semibold tabular-nums',
                  active
                    ? 'bg-primary/12 text-primary'
                    : 'bg-background/80 text-muted-foreground',
                )}
              >
                {count}
              </span>
            </button>
          )
        })}
      </div>

      <div className="panel flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10 bg-card">
              <tr className="border-b border-border text-left">
                <th className="w-10 px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground" />
                <th className="px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Document
                </th>
                <th className="px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Type
                </th>
                <th className="px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Stage
                </th>
                <th className="px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Status
                </th>
                <th className="px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Duration
                </th>
                <th className="px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Started
                </th>
                <th className="px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Error
                </th>
              </tr>
            </thead>
            <tbody>
              {paginated.map((run) => {
                const runId = run.id || run.job_id
                const isExpanded = expanded.has(runId)
                const title = getDocumentListLabel(run)
                const fileLabel = getDocumentFileLabel(run)
                const err = (run.error_message || run.error || '').split('\n')[0]

                return (
                  <React.Fragment key={runId}>
                    <tr
                      className={cn(
                        'border-b border-border transition-colors hover:bg-muted/25',
                        isExpanded && 'bg-muted/30',
                      )}
                    >
                      <td className="px-2 py-2.5">
                        <button
                          type="button"
                          className="flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                          onClick={() => toggleExpand(runId)}
                          aria-label={isExpanded ? 'Collapse' : 'Expand'}
                        >
                          {isExpanded ? (
                            <ChevronUp className="size-4" />
                          ) : (
                            <ChevronDown className="size-4" />
                          )}
                        </button>
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex min-w-0 max-w-[280px] flex-col gap-0.5">
                          <button
                            type="button"
                            className="truncate text-left text-sm font-medium text-primary hover:underline"
                            onClick={() => navigate(`/documents/${run.workflow_id}`)}
                            title={title}
                          >
                            {title}
                          </button>
                          <div className="flex flex-wrap items-center gap-1.5">
                            <InstanceBadge instance={run.instance} />
                            <span className="truncate font-mono text-[10px] text-muted-foreground">
                              #{runId}
                              {fileLabel && fileLabel !== title ? ` · ${fileLabel}` : ''}
                            </span>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="text-xs text-foreground/90">
                          {formatJobType(run.job_type)}
                        </span>
                      </td>
                      <td className="px-3 py-2.5">
                        <StageBadge
                          stage={run.current_stage || run.document_stage || run.stage}
                          compact
                        />
                      </td>
                      <td className="px-3 py-2.5">
                        <StatusBadge status={run.status} />
                      </td>
                      <td className="px-3 py-2.5 text-xs text-muted-foreground">
                        {run.duration_ms ? (
                          formatDuration(run.duration_ms)
                        ) : run.status === 'running' ? (
                          <span className="inline-flex items-center gap-1 text-info">
                            <Clock className="size-3 animate-pulse" />
                            In progress
                          </span>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2.5 text-xs text-muted-foreground">
                        {formatCompactDateTime(run.started_at)}
                      </td>
                      <td
                        className="max-w-[180px] truncate px-3 py-2.5 text-xs text-destructive"
                        title={err || ''}
                      >
                        {err || (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                    </tr>
                    {isExpanded ? (
                      <tr className="border-b border-border bg-muted/20">
                        <td colSpan={8} className="px-4 py-3">
                          <div className="space-y-3 rounded-lg border border-border bg-card p-4">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                                Job details
                              </span>
                              <Button
                                variant="outline"
                                size="sm"
                                className="h-7 text-xs"
                                onClick={() => navigate(`/documents/${run.workflow_id}`)}
                              >
                                Open document
                              </Button>
                            </div>
                            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                              <div>
                                <span className="block text-[10px] text-muted-foreground">Job ID</span>
                                <span className="font-mono text-xs">{runId}</span>
                              </div>
                              <div className="min-w-0">
                                <span className="block text-[10px] text-muted-foreground">Workflow</span>
                                <span className="block truncate font-mono text-[11px]">
                                  {run.workflow_id}
                                </span>
                              </div>
                              <div>
                                <span className="block text-[10px] text-muted-foreground">Attempt</span>
                                <span className="text-xs">{run.attempt || 1}</span>
                              </div>
                              <div>
                                <span className="block text-[10px] text-muted-foreground">Completed</span>
                                <span className="text-xs">
                                  {formatCompactDateTime(run.completed_at)}
                                </span>
                              </div>
                            </div>
                            <div>
                              <span className="mb-2 block text-[10px] text-muted-foreground">
                                Stage progression
                              </span>
                              <PipelineStepper
                                currentStage={
                                  run.current_stage || run.document_stage || run.stage
                                }
                                hasPages
                                hasChunks={
                                  run.job_type?.includes('chunk') ||
                                  run.stage === 'completed' ||
                                  run.document_stage === 'completed'
                                }
                              />
                            </div>
                            {run.error_message || run.error ? (
                              <div className="flex items-start gap-2 rounded-md border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive">
                                <AlertCircle className="mt-0.5 size-4 shrink-0" />
                                <span className="break-words">
                                  {run.error_message || run.error}
                                </span>
                              </div>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                )
              })}

              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-16 text-center">
                    <Play className="mx-auto mb-3 size-8 text-muted-foreground/30" />
                    <p className="text-sm font-medium text-foreground">No runs found</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      No jobs match the current filter.
                    </p>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        {filtered.length > 0 ? (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-2.5">
            <p className="text-xs text-muted-foreground">
              Showing {(Math.min(page, totalPages) - 1) * PAGE_SIZE + 1}–
              {Math.min(Math.min(page, totalPages) * PAGE_SIZE, filtered.length)} of{' '}
              {filtered.length}
            </p>
            <div className="flex items-center gap-1.5">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 px-2"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                <ChevronLeft className="size-4" />
              </Button>
              <span className="min-w-[4.5rem] text-center text-xs font-medium">
                {Math.min(page, totalPages)} / {totalPages}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 px-2"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
