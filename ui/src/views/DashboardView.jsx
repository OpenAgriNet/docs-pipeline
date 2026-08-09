import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Skeleton } from '../components/ui/skeleton'
import { StatCard } from '../components/StatCard'
import { StageBadge } from '../components/StageBadge'
import { fetchJson, formatCount, getDocumentListLabel, summarizeQueueReason } from '../lib/pipelineUi'
import { formatInstanceLabel } from '../lib/instanceLabels'
import { useAuth } from '../auth/AuthProvider'
import { AlertTriangle, CheckCircle, FileText, Play, RefreshCw } from 'lucide-react'

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
  const displayedQueueItems = queue

  const byInstance = useMemo(() => (Array.isArray(summary?.by_instance) ? summary.by_instance : []), [summary])
  const maxInstanceCount = useMemo(
    () => byInstance.reduce((max, item) => Math.max(max, item.count || 0), 0),
    [byInstance],
  )

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
        <Skeleton className="h-[220px] rounded-lg" />
        <Skeleton className="h-[200px] rounded-lg" />
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

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {byInstance.length > 0 && (
          <div className="panel lg:col-span-2">
            <div className="panel-header flex items-center justify-between">
              <h2 className="text-sm font-medium text-foreground">
                {isSuperAdmin ? 'Documents by State' : 'Your State'}
              </h2>
              <Badge variant="secondary" className="text-xs">{formatCount(totalDocs)} total</Badge>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 p-4">
              {byInstance.map(({ instance, count }) => (
                <div key={instance} className="rounded-md border border-border bg-muted/30 p-3">
                  <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                    {formatInstanceLabel(instance) || 'No state tag'}
                  </div>
                  <div className="mt-1 text-xl font-semibold font-serif text-foreground">{formatCount(count)}</div>
                </div>
              ))}
            </div>

            {isSuperAdmin && byInstance.length > 1 && (
              <div className="border-t border-border p-4 space-y-2.5">
                {byInstance.map(({ instance, count }) => (
                  <div key={instance} className="flex items-center gap-3">
                    <span className="w-14 shrink-0 text-xs font-medium text-muted-foreground">
                      {formatInstanceLabel(instance) || '—'}
                    </span>
                    <div className="h-2 flex-1 rounded-full bg-muted overflow-hidden">
                      <div
                        className="h-full rounded-full bg-primary"
                        style={{ width: `${maxInstanceCount ? (count / maxInstanceCount) * 100 : 0}%` }}
                      />
                    </div>
                    <span className="w-10 shrink-0 text-right text-xs font-medium text-foreground">{formatCount(count)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className={byInstance.length > 0 ? 'panel' : 'panel lg:col-span-3'}>
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
    </div>
  )
}
