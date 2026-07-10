import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Skeleton } from '../components/ui/skeleton'
import { StageBadge } from '../components/StageBadge'
import PipelineStepper from '../components/PipelineStepper'
import { fetchJson, formatCompactDateTime, getDocumentListLabel } from '../lib/pipelineUi'
import { Tabs, TabsList, TabsTrigger } from '../components/ui/tabs'
import { AlertCircle, ChevronDown, ChevronUp, Clock, Play } from 'lucide-react'

function formatDuration(ms) {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  const seconds = Math.floor(ms / 1000)
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  if (!minutes) return `${seconds}s`
  return `${minutes}m ${remainder}s`
}

export default function RunsView() {
  const navigate = useNavigate()
  const [tab, setTab] = useState('all')
  const [runs, setRuns] = useState([])
  const [expanded, setExpanded] = useState(new Set())
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    load()
  }, [tab])

  async function load() {
    setLoading(true)
    try {
      const pageSize = 100
      let offset = 0
      let allRuns = []
      while (true) {
        const query = new URLSearchParams({ limit: String(pageSize), offset: String(offset) })
        if (tab !== 'all') query.set('status', tab)
        const data = await fetchJson(`/runs?${query.toString()}`)
        const batch = Array.isArray(data) ? data : []
        allRuns = allRuns.concat(batch)
        if (batch.length < pageSize) break
        offset += pageSize
      }
      setRuns(allRuns)
    } finally {
      setLoading(false)
    }
  }

  const counts = useMemo(() => ({
    running: runs.filter(run => run.status === 'running').length,
    queued: runs.filter(run => run.status === 'queued').length,
    failed: runs.filter(run => run.status === 'failed').length,
    completed: runs.filter(run => run.status === 'completed').length,
  }), [runs])

  function toggleExpand(id) {
    const next = new Set(expanded)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setExpanded(next)
  }

  if (loading) {
    return (
      <div className="p-6 max-w-7xl mx-auto space-y-4">
        <Skeleton className="h-8 w-24" />
        <Skeleton className="h-4 w-48 mt-1" />
        <Skeleton className="h-10 w-80 mt-4" />
        <Skeleton className="h-[300px] rounded-lg mt-4" />
      </div>
    )
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div>
        <h1 className="text-2xl font-serif font-semibold text-foreground">Runs</h1>
        <p className="text-sm text-muted-foreground mt-1">Pipeline job history and status</p>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="all">All ({runs.length})</TabsTrigger>
          <TabsTrigger value="running">Running ({counts.running})</TabsTrigger>
          <TabsTrigger value="queued">Queued ({counts.queued})</TabsTrigger>
          <TabsTrigger value="failed">Failed ({counts.failed})</TabsTrigger>
          <TabsTrigger value="completed">Completed ({counts.completed})</TabsTrigger>
        </TabsList>
      </Tabs>

      <div className="panel">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Job ID</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Document</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Type</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Stage</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Status</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Duration</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Started</th>
                <th className="px-4 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">Error</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(run => {
                const runId = run.id || run.job_id
                const isExpanded = expanded.has(runId)
                return (
                  <React.Fragment key={runId}>
                    <tr className={`data-table-row ${isExpanded ? 'bg-accent/30' : ''}`}>
                      <td className="px-4 py-3">
                        <button
                          className="flex items-center gap-2 text-left text-muted-foreground hover:text-foreground transition-colors"
                          onClick={() => toggleExpand(runId)}
                        >
                          {isExpanded ? <ChevronUp className="h-4 w-4 shrink-0" /> : <ChevronDown className="h-4 w-4 shrink-0" />}
                          <span className="font-mono text-xs">{runId}</span>
                        </button>
                      </td>
                      <td className="px-4 py-3">
                        <span className="text-primary hover:underline cursor-pointer truncate block max-w-[200px]" onClick={() => navigate(`/documents/${run.workflow_id}`)}>
                          {getDocumentListLabel(run)}
                        </span>
                      </td>
                      <td className="px-4 py-3 capitalize">{run.job_type}</td>
                      <td className="px-4 py-3"><StageBadge stage={run.current_stage || run.stage} compact /></td>
                      <td className="px-4 py-3">
                        <Badge variant={run.status === 'running' ? 'info' : run.status === 'completed' ? 'success' : run.status === 'queued' ? 'secondary' : 'destructive'}>
                          {run.status === 'running' ? <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse-warm mr-1" /> : null}
                          {run.status}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {run.duration_ms ? formatDuration(run.duration_ms) : run.status === 'running' ? (
                          <span className="flex items-center gap-1 text-info">
                            <Clock className="h-3 w-3 animate-pulse-warm" />
                            In progress
                          </span>
                        ) : '—'}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">{formatCompactDateTime(run.started_at)}</td>
                      <td className="px-4 py-3 text-xs text-destructive max-w-[200px] truncate" title={run.error_message || run.error || ''}>
                        {(run.error_message || run.error || '—').split('\n')[0]}
                      </td>
                    </tr>
                    {isExpanded ? (
                      <tr>
                        <td colSpan={8} className="px-4 py-0">
                          <div className="py-4 px-4 mb-2 rounded-md bg-muted/50 space-y-3">
                            <div className="flex items-center justify-between">
                              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Job Details</span>
                              <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => navigate(`/documents/${run.workflow_id}`)}>
                                Open Document →
                              </Button>
                            </div>
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
                              <div>
                                <span className="text-xs text-muted-foreground block">Job ID</span>
                                <span className="font-mono text-xs">{runId}</span>
                              </div>
                              <div>
                                <span className="text-xs text-muted-foreground block">Workflow ID</span>
                                <span className="font-mono text-xs">{run.workflow_id}</span>
                              </div>
                              <div>
                                <span className="text-xs text-muted-foreground block">Attempt</span>
                                <span className="text-xs">{run.attempt || 1}</span>
                              </div>
                              <div>
                                <span className="text-xs text-muted-foreground block">Completed</span>
                                <span className="text-xs">{formatCompactDateTime(run.completed_at)}</span>
                              </div>
                            </div>
                            <div>
                              <span className="text-xs text-muted-foreground block mb-2">Stage Progression</span>
                              <PipelineStepper currentStage={run.current_stage || run.stage} hasPages hasChunks={run.job_type?.includes('chunk') || run.stage === 'completed'} />
                            </div>
                            {run.error_message || run.error ? (
                              <div className="p-3 rounded-md bg-destructive/10 border border-destructive/20 text-sm text-destructive">
                                <div className="flex items-start gap-2">
                                  <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
                                  <span className="break-words">{run.error_message || run.error}</span>
                                </div>
                              </div>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                )
              })}
              {runs.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-16 text-center">
                    <Play className="h-8 w-8 mx-auto mb-3 text-muted-foreground/30" />
                    <p className="text-sm text-muted-foreground">No runs match the current filter.</p>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
