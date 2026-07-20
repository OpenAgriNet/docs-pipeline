import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Skeleton } from '../components/ui/skeleton'
import { StatCard } from '../components/StatCard'
import { StageBadge } from '../components/StageBadge'
import { fetchJson, formatCompactDateTime, formatCount, getDocumentListLabel, getStageLabel, summarizeQueueReason } from '../lib/pipelineUi'
import { AlertTriangle, CheckCircle, FileText, ListTodo, Play, RefreshCw, XCircle } from 'lucide-react'

function formatDuration(ms) {
  if (!ms && ms !== 0) return '—'
  if (ms < 1000) return `${ms}ms`
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
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
  const [summary, setSummary] = useState(null)
  const [queue, setQueue] = useState([])
  const [runs, setRuns] = useState([])
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
      const [summaryData, queueData, runData] = await Promise.all([
        fetchJson('/documents/summary'),
        fetchJson('/operations/queue?limit=8'),
        fetchJson('/runs?status=running&limit=8')
      ])
      setSummary(summaryData)
      setQueue(queueData.items || [])
      setQueueTotal(queueData.total || 0)
      setRuns(Array.isArray(runData) ? runData : [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  const totalDocs = summary?.total_documents || 0
  const authDocs = summary?.authoritative_documents || 0
  const legacyDocs = Math.max(0, totalDocs - authDocs)
  const failedDocs = summary?.failed_documents || 0
  const reindexCount = summary?.needs_reindex || 0
  const runningJobs = summary?.running_jobs || 0
  const queuedItems = summary?.review_queue || 0
  const reviewBacklog = queuedItems
  const reindexBacklog = reindexCount
  const displayedQueueItems = queue

  const stageCounts = useMemo(() => summary?.by_stage || {}, [summary])

  if (loading) {
    return (
      <div className="p-6 max-w-7xl mx-auto space-y-6">
        <div>
          <Skeleton className="h-8 w-40" />
          <Skeleton className="h-4 w-56 mt-2" />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[100px] rounded-lg" />)}
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <Skeleton className="h-[200px] rounded-lg lg:col-span-2" />
          <Skeleton className="h-[200px] rounded-lg" />
        </div>
        <Skeleton className="h-[250px] rounded-lg" />
      </div>
    )
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-serif font-semibold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Pipeline operational overview</p>
      </div>

      {error ? <InlineNotice message={error} /> : null}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Total Documents"
          value={formatCount(totalDocs)}
          subtitle={`${formatCount(authDocs)} authoritative · ${formatCount(legacyDocs)} legacy`}
          icon={<FileText className="h-4 w-4" />}
          onClick={() => navigate('/documents')}
        />
        <StatCard
          label="Failed"
          value={formatCount(failedDocs)}
          variant={failedDocs > 0 ? 'danger' : 'default'}
          subtitle="Require attention"
          icon={<AlertTriangle className="h-4 w-4" />}
          onClick={() => navigate('/documents?filter=failed')}
        />
        <StatCard
          label="Reindex Required"
          value={formatCount(reindexCount)}
          variant={reindexCount > 0 ? 'warning' : 'default'}
          subtitle="Search may be stale"
          icon={<RefreshCw className="h-4 w-4" />}
          onClick={() => navigate('/documents?filter=reindex')}
        />
        <StatCard
          label="Running Jobs"
          value={formatCount(runningJobs)}
          variant={runningJobs > 0 ? 'success' : 'default'}
          subtitle={queuedItems ? `${formatCount(queuedItems)} queued` : undefined}
          icon={<Play className="h-4 w-4" />}
          onClick={() => navigate('/runs')}
        />
      </div>

      <div className="panel">
        <div className="panel-header flex items-center justify-between">
          <h2 className="text-sm font-medium text-foreground">Needs Attention</h2>
          <Badge variant="secondary" className="text-xs">{formatCount(failedDocs + reindexCount + queuedItems)} items</Badge>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 divide-x divide-border">
          <div className="p-4 text-center cursor-pointer hover:bg-accent/50 transition-colors" onClick={() => navigate('/runs')}>
            <div className="flex items-center justify-center gap-1.5 text-destructive mb-1">
              <XCircle className="h-3.5 w-3.5" />
              <span className="text-xs font-medium uppercase tracking-wider">Failed Docs</span>
            </div>
            <span className="font-serif text-xl font-semibold">{formatCount(failedDocs)}</span>
          </div>
          <div className="p-4 text-center cursor-pointer hover:bg-accent/50 transition-colors" onClick={() => navigate('/queue')}>
            <div className="flex items-center justify-center gap-1.5 text-info mb-1">
              <ListTodo className="h-3.5 w-3.5" />
              <span className="text-xs font-medium uppercase tracking-wider">Review Backlog</span>
            </div>
            <span className="text-xl font-semibold font-serif">{formatCount(reviewBacklog)}</span>
          </div>
          <div className="p-4 text-center cursor-pointer hover:bg-accent/50 transition-colors" onClick={() => navigate('/documents?filter=reindex')}>
            <div className="flex items-center justify-center gap-1.5 text-warning mb-1">
              <RefreshCw className="h-3.5 w-3.5" />
              <span className="text-xs font-medium uppercase tracking-wider">Reindex Backlog</span>
            </div>
            <span className="text-xl font-semibold font-serif">{formatCount(reindexBacklog)}</span>
          </div>
          <div className="p-4 text-center cursor-pointer hover:bg-accent/50 transition-colors" onClick={() => navigate('/runs')}>
            <div className="flex items-center justify-center gap-1.5 text-success mb-1">
              <Play className="h-3.5 w-3.5" />
              <span className="text-xs font-medium uppercase tracking-wider">Active Jobs</span>
            </div>
            <span className="text-xl font-semibold font-serif">{formatCount(runningJobs)}</span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="panel lg:col-span-2">
          <div className="panel-header">
            <h2 className="text-sm font-medium text-foreground">Stage Distribution</h2>
          </div>
          <div className="p-4">
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {['registered', 'ocr_processing', 'ocr_review', 'translation_processing', 'translation_review', 'chunking', 'chunk_review', 'ready_for_ingestion', 'ingesting', 'completed', 'failed']
                .filter(stage => stageCounts[stage] !== undefined)
                .map(stage => (
                <div
                  key={stage}
                  className="flex items-center justify-between p-3 rounded-md bg-muted/50 cursor-pointer hover:bg-accent/50 transition-colors"
                  onClick={() => navigate(`/documents?stage=${stage}`)}
                >
                  <StageBadge stage={stage} compact />
                  <span className="text-lg font-semibold font-serif">{formatCount(stageCounts[stage])}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header flex items-center justify-between">
            <h2 className="text-sm font-medium text-foreground">Queue Preview</h2>
            <Badge variant="secondary" className="text-xs">{formatCount(displayedQueueItems.length)}</Badge>
          </div>
          {displayedQueueItems.length ? (
            <div className="divide-y divide-border">
              {displayedQueueItems.map(item => (
                <div
                  key={item.workflow_id}
                  className="px-4 py-3 hover:bg-accent/50 cursor-pointer transition-colors"
                  onClick={() => navigate(`/documents/${item.workflow_id}`)}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-info shrink-0" />
                    <span className="text-sm font-medium text-foreground truncate">{getDocumentListLabel(item)}</span>
                  </div>
                  <div className="flex items-center gap-2 ml-3.5">
                    <StageBadge stage={item.stage} className="text-[10px]" compact />
                    <span className="text-xs text-muted-foreground truncate">{summarizeQueueReason(item)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="p-8 text-center text-muted-foreground">
              <CheckCircle className="h-8 w-8 mx-auto mb-2 opacity-30" />
              <p className="text-sm">Queue is clear</p>
            </div>
          )}
          {queuedItems > queue.length && (
            <div
              className="px-4 py-3 border-t border-border text-center text-xs font-medium text-primary cursor-pointer hover:bg-accent/50 transition-colors"
              onClick={() => navigate('/queue')}
            >
              View all {formatCount(queuedItems)} queue items →
            </div>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-header flex items-center justify-between">
          <div>
            <h2 className="text-sm font-medium text-foreground">Recent Running Jobs</h2>
            <p className="mt-1 text-xs text-muted-foreground">Previewing up to 8 active runs. Open Runs for the full ledger.</p>
          </div>
          <span className="text-xs text-primary cursor-pointer hover:underline" onClick={() => navigate('/runs')}>
            View all →
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <th className="px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">Job</th>
                <th className="px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">Document</th>
                <th className="px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">Type</th>
                <th className="px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">Stage</th>
                <th className="px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">Status</th>
                <th className="px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">Duration</th>
                <th className="px-4 py-2.5 text-xs font-medium text-muted-foreground uppercase tracking-wider">Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.slice(0, 7).map(run => (
                <tr key={run.id || run.job_id} className="data-table-row">
                  <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground">{run.id || run.job_id}</td>
                  <td className="px-4 py-2.5">
                    <span
                      className="text-primary hover:underline cursor-pointer truncate block max-w-[200px]"
                      onClick={() => navigate(`/documents/${run.workflow_id}`)}
                    >
                      {run.filename || run.workflow_id}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 capitalize">{run.job_type}</td>
                      <td className="px-4 py-2.5"><StageBadge stage={run.current_stage || run.stage} compact /></td>
                  <td className="px-4 py-2.5">
                    <Badge variant={run.status === 'running' ? 'info' : run.status === 'completed' ? 'success' : run.status === 'queued' ? 'secondary' : 'destructive'}>
                      {run.status === 'running' && <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse-warm mr-1" />}
                      {run.status}
                    </Badge>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-muted-foreground">
                    {formatDuration(run.duration_ms)}
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground text-xs">
                    {formatCompactDateTime(run.started_at)}
                  </td>
                </tr>
              ))}
              {!runs.length && (
                <tr>
                  <td colSpan={7} className="px-4 py-12 text-center text-muted-foreground">No active runs.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
