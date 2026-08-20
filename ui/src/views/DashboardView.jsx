import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Skeleton } from '../components/ui/skeleton'
import { StatCard } from '../components/StatCard'
import { StageBadge } from '../components/StageBadge'
import { fetchJson, formatCount, getDocumentListLabel, summarizeQueueReason } from '../lib/pipelineUi'
import { formatInstanceLabel } from '../lib/instanceLabels'
import { useAuth } from '../auth/AuthProvider'
import { AlertTriangle, CheckCircle, FileText, Globe2, ListTodo } from 'lucide-react'

const QUEUE_ROWS_SHOWN = 4

const JOB_STATUS_BADGE = {
  running: { variant: 'info', label: 'running' },
  queued: { variant: 'outline', label: 'queued' },
  retrying: { variant: 'destructive', label: 'retrying' },
  failed: { variant: 'destructive', label: 'failed' },
}

function InlineNotice({ message }) {
  return (
    <div className="rounded-md border border-destructive/20 bg-destructive/10 px-3 py-2 text-sm text-destructive">
      <div className="flex items-center gap-2">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        <span>{message}</span>
      </div>
    </div>
  )
}

export default function DashboardView() {
  const navigate = useNavigate()
  const { isSuperAdmin } = useAuth()
  const [summary, setSummary] = useState(null)
  const [queue, setQueue] = useState([])
  const [queueTotal, setQueueTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    load()
  }, [])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const [summaryData, queueData] = await Promise.all([
        fetchJson('/documents/summary'),
        fetchJson('/operations/queue?limit=8'),
      ])
      setSummary(summaryData)
      setQueue(queueData.items || [])
      setQueueTotal(queueData.total || 0)
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  const totalDocs = summary?.total_documents || 0
  const completedDocs = summary?.completed_documents || 0
  const failedDocs = summary?.failed_documents || 0
  const reindexCount = summary?.needs_reindex || 0
  const displayedQueueItems = queue.slice(0, QUEUE_ROWS_SHOWN)

  const byInstance = useMemo(() => (Array.isArray(summary?.by_instance) ? summary.by_instance : []), [summary])
  const stateRows = byInstance

  const maxStateCount = useMemo(
    () => stateRows.reduce((max, item) => Math.max(max, item.count || 0), 0),
    [stateRows],
  )

  if (loading) {
    return (
      <div className="page-shell space-y-6">
        <div>
          <Skeleton className="h-8 w-40" />
          <Skeleton className="h-4 w-56 mt-2" />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-[84px] rounded-lg" />)}
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-[84px] rounded-lg" />)}
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <Skeleton className="h-[360px] rounded-lg lg:col-span-2" />
          <Skeleton className="h-[360px] rounded-lg" />
        </div>
      </div>
    )
  }

  return (
    <div className="page-shell space-y-6">
      <div>
        <h1 className="text-2xl font-serif font-semibold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Pipeline operational overview</p>
      </div>

      {error ? <InlineNotice message={error} /> : null}

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <StatCard
          label="Total Documents"
          value={formatCount(totalDocs)}
          icon={<FileText className="h-4 w-4" />}
          onClick={() => navigate('/documents')}
        />
        <StatCard
          label="Total States"
          value={formatCount(byInstance.length)}
          icon={<Globe2 className="h-4 w-4" />}
        />
        <StatCard
          label="In Queue"
          value={formatCount(queueTotal)}
          icon={<ListTodo className="h-4 w-4" />}
          onClick={() => navigate('/queue')}
        />
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <div
          className="rounded-lg border border-success/25 bg-success/10 p-4 cursor-pointer hover:bg-success/15 transition-colors"
          onClick={() => navigate('/documents?filter=completed')}
        >
          <div className="text-sm font-medium text-success">Success docs</div>
          <div className="mt-1 text-2xl font-semibold font-serif text-success">{formatCount(completedDocs)}</div>
        </div>
        <div
          className="rounded-lg border border-destructive/25 bg-destructive/10 p-4 cursor-pointer hover:bg-destructive/15 transition-colors"
          onClick={() => navigate('/documents?filter=failed')}
        >
          <div className="text-sm font-medium text-destructive">Failed docs</div>
          <div className="mt-1 text-2xl font-semibold font-serif text-destructive">{formatCount(failedDocs)}</div>
        </div>
        <div
          className="rounded-lg border border-warning/30 bg-warning/10 p-4 cursor-pointer hover:bg-warning/15 transition-colors"
          onClick={() => navigate('/documents?filter=reindex')}
        >
          <div className="text-sm font-medium text-warning">Re-ingest marked</div>
          <div className="mt-1 text-2xl font-semibold font-serif text-warning">{formatCount(reindexCount)}</div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {isSuperAdmin && stateRows.length > 0 && (
          <div className="panel lg:col-span-2">
            <div className="panel-header flex items-center justify-between flex-wrap gap-2">
              <h2 className="text-sm font-medium text-foreground">Ingested by state</h2>
              {stateRows.length > 1 && (
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span className="flex items-center gap-1.5">
                    <span className="w-2 h-2 rounded-full bg-success" /> Success
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="w-2 h-2 rounded-full bg-destructive" /> Failed
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="w-2 h-2 rounded-full bg-warning" /> Dev approval
                  </span>
                </div>
              )}
            </div>

            <div className="p-4 space-y-4 max-h-80 overflow-y-auto">
              {stateRows.map((row) => {
                const label = row.otherStateCount
                  ? `Others (${row.otherStateCount} states)`
                  : formatInstanceLabel(row.instance) || 'No state tag'
                const pct = (value) => (maxStateCount ? (value / maxStateCount) * 100 : 0)
                return (
                  <div key={row.instance}>
                    <div className="flex items-center justify-between mb-1.5">
                      <span className="text-sm font-medium text-foreground">{label}</span>
                      <span className="text-sm font-medium text-foreground">{formatCount(row.count)}</span>
                    </div>
                    <div className="h-2 w-full rounded-full bg-muted overflow-hidden flex">
                      <div className="h-full bg-success" style={{ width: `${pct(row.success)}%` }} />
                      <div className="h-full bg-destructive" style={{ width: `${pct(row.failed)}%` }} />
                      <div className="h-full bg-warning" style={{ width: `${pct(row.dev_approval)}%` }} />
                    </div>
                    <div className="mt-1.5 flex items-center gap-3 text-xs text-muted-foreground">
                      <span className="flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-success shrink-0" />
                        {formatCount(row.success)} success
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-destructive shrink-0" />
                        {formatCount(row.failed)} failed
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-warning shrink-0" />
                        {formatCount(row.dev_approval)} dev approval
                      </span>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        <div className={isSuperAdmin && stateRows.length > 0 ? 'panel' : 'panel lg:col-span-3'}>
          <div className="panel-header flex items-center justify-between">
            <h2 className="text-sm font-medium text-foreground">Queue Preview</h2>
            <Badge variant="secondary" className="text-xs">{formatCount(queueTotal)}</Badge>
          </div>
          {displayedQueueItems.length ? (
            <div className="divide-y divide-border">
              {displayedQueueItems.map(item => {
                const statusBadge = JOB_STATUS_BADGE[item.job_status] || null
                return (
                  <div
                    key={item.workflow_id}
                    className="px-4 py-3 hover:bg-accent/50 cursor-pointer transition-colors"
                    onClick={() => navigate(`/documents/${item.workflow_id}`)}
                  >
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span className="text-sm font-medium text-foreground truncate">{getDocumentListLabel(item)}</span>
                      {statusBadge && (
                        <Badge variant={statusBadge.variant} className="text-[10px] shrink-0">{statusBadge.label}</Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <StageBadge stage={item.stage} className="text-[10px]" compact />
                      <span className="text-xs text-muted-foreground truncate">{summarizeQueueReason(item)}</span>
                    </div>
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="p-8 text-center text-muted-foreground">
              <CheckCircle className="h-8 w-8 mx-auto mb-2 opacity-30" />
              <p className="text-sm">Queue is clear</p>
            </div>
          )}
          {queueTotal > displayedQueueItems.length && (
            <div
              className="px-4 py-3 border-t border-border text-center text-xs font-medium text-primary cursor-pointer hover:bg-accent/50 transition-colors"
              onClick={() => navigate('/queue')}
            >
              View more →
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
