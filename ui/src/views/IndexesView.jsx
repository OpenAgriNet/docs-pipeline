import React, { useEffect, useState } from 'react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent } from '../components/ui/card'
import { Skeleton } from '../components/ui/skeleton'
import { fetchAllDocuments, fetchJson, formatCompactDateTime, formatCount } from '../lib/pipelineUi'
import { Activity, AlertTriangle, CheckCircle, ChevronDown, ChevronUp, CircleAlert, Clock, Database, HardDrive, RefreshCcw } from 'lucide-react'

function formatMetric(value, suffix = '') {
  if (value === null || value === undefined || value === '') return '—'
  return `${value}${suffix}`
}

export default function IndexesView() {
  const [loading, setLoading] = useState(true)
  const [expandedIndex, setExpandedIndex] = useState(null)
  const [indexRows, setIndexRows] = useState([])
  const [error, setError] = useState('')
  const [actionMessage, setActionMessage] = useState('')
  const [busyIndex, setBusyIndex] = useState('')

  useEffect(() => {
    load()
  }, [])

  async function load() {
    setLoading(true)
    setError('')
    try {
      const rows = await fetchJson('/marqo/indexes/summary')
      setIndexRows(Array.isArray(rows) ? rows : [])
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleReindex(indexName, mode = 'stale') {
    setBusyIndex(indexName)
    setActionMessage('')
    setError('')
    try {
      const docs = await fetchAllDocuments()
      const workflowIds = docs
        .filter(doc => {
          if (mode === 'stale') return doc.reindex_required
          return doc.reindex_required || ['completed', 'ready_for_ingestion', 'chunk_review'].includes(doc.stage)
        })
        .map(doc => doc.workflow_id)

      if (!workflowIds.length) {
        setActionMessage(`No eligible documents found for ${mode === 'stale' ? 'stale reindex' : 'full reindex'}.`)
        return
      }

      const result = await fetchJson(
        `/documents/bulk/reindex?index_name=${encodeURIComponent(indexName)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ workflow_ids: workflowIds })
        }
      )

      setActionMessage(
        `${result.succeeded} workflow${result.succeeded === 1 ? '' : 's'} queued for ${mode === 'stale' ? 'stale' : 'full'} reindex from ${indexName}.`
      )
      await load()
    } catch (actionError) {
      setError(actionError.message)
    } finally {
      setBusyIndex('')
    }
  }

  if (loading) {
    return (
      <div className="p-6 max-w-7xl mx-auto space-y-4">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-4 w-48 mt-1" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[180px] rounded-lg" />
          ))}
        </div>
      </div>
    )
  }

  if (!error && indexRows.length === 0) {
    return (
      <div className="p-6 max-w-7xl mx-auto space-y-4">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Indexes</h1>
          <p className="text-sm text-muted-foreground mt-1">Search index health and status</p>
        </div>
        <div className="panel p-16 text-center">
          <Database className="h-10 w-10 mx-auto mb-3 text-muted-foreground/30" />
          <p className="text-sm font-medium text-foreground">No indexes configured</p>
          <p className="text-xs text-muted-foreground mt-1">Indexes will appear here once documents complete ingestion</p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div>
        <h1 className="text-2xl font-serif font-semibold text-foreground">Indexes</h1>
        <p className="text-sm text-muted-foreground mt-1">Search index health and status</p>
      </div>

      {error ? (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
          <AlertTriangle className="h-4 w-4 text-destructive shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {actionMessage ? (
        <Card className="shadow-none">
          <CardContent className="flex items-start gap-3 px-4 py-3 text-sm text-foreground">
            <CheckCircle className="mt-0.5 h-4 w-4 text-success" />
            <span>{actionMessage}</span>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {indexRows.map(idx => {
          const health = idx.live_error ? 'degraded' : idx.stale_documents > 0 ? 'warning' : 'healthy'
          return (
            <div key={idx.index_name} className={`panel ${health === 'degraded' ? 'border-destructive/30' : health === 'warning' ? 'border-warning/30' : ''}`}>
              <div className="panel-header flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Database className="h-4 w-4 text-muted-foreground" />
                  <h3 className="text-sm font-semibold font-mono text-foreground">{idx.index_name}</h3>
                </div>
                <div className="flex items-center gap-2">
                  {health === 'healthy' ? <CheckCircle className="h-3.5 w-3.5 text-success" /> : <AlertTriangle className={`h-3.5 w-3.5 ${health === 'warning' ? 'text-warning' : 'text-destructive'}`} />}
                  {idx.stale_documents > 0 ? (
                    <Badge variant="warning"><AlertTriangle className="h-3 w-3 mr-1" />Stale</Badge>
                  ) : (
                    <Badge variant="success"><CheckCircle className="h-3 w-3 mr-1" />Synced</Badge>
                  )}
                  {idx.has_domain_tags_field === false ? (
                    <Badge variant="outline">No tag field</Badge>
                  ) : idx.has_domain_tags_field ? (
                    <Badge variant="secondary">Tag filters</Badge>
                  ) : null}
                </div>
              </div>
              <div className="p-4 space-y-4">
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <p className="text-xs text-muted-foreground uppercase tracking-wider">Documents</p>
                    <p className="text-xl font-semibold font-serif mt-1">{formatCount(idx.documents)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground uppercase tracking-wider">Chunks</p>
                    <p className="text-xl font-semibold font-serif mt-1">{formatCount(idx.indexed_chunks)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground uppercase tracking-wider">Stale</p>
                    <p className={`text-xl font-semibold font-serif mt-1 ${idx.stale_documents > 0 ? 'text-warning' : ''}`}>
                      {formatCount(idx.stale_documents)}
                    </p>
                  </div>
                </div>

                {idx.stale_documents > 0 ? (
                  <Card className="border-warning/30 bg-warning/10 shadow-none">
                    <CardContent className="flex items-center gap-3 p-3">
                      <RefreshCcw className="h-4 w-4 shrink-0 text-warning" />
                      <span className="text-sm text-foreground">{formatCount(idx.stale_documents)} document(s) need reindexing</span>
                      <Button
                        size="sm"
                        variant="warning"
                        className="ml-auto"
                        disabled={busyIndex === idx.index_name}
                        onClick={() => handleReindex(idx.index_name, 'stale')}
                      >
                        {busyIndex === idx.index_name ? 'Queueing...' : 'Reindex Stale'}
                      </Button>
                    </CardContent>
                  </Card>
                ) : null}

                <button
                  className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors w-full"
                  onClick={() => setExpandedIndex(expandedIndex === idx.index_name ? null : idx.index_name)}
                >
                  {expandedIndex === idx.index_name ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                  Index stats
                </button>

                {expandedIndex === idx.index_name ? (
                  <div className="grid grid-cols-3 gap-3 pt-2 border-t border-border">
                    <div className="flex items-center gap-2">
                      <Activity className="h-3.5 w-3.5 text-muted-foreground" />
                      <div>
                        <p className="text-[10px] text-muted-foreground">Avg Query</p>
                        <p className="text-xs font-medium">{formatMetric(idx.avg_query_ms ?? idx.live_stats?.avgQueryMs, 'ms')}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <HardDrive className="h-3.5 w-3.5 text-muted-foreground" />
                      <div>
                        <p className="text-[10px] text-muted-foreground">Storage</p>
                        <p className="text-xs font-medium">{formatMetric(idx.storage_mb ?? idx.live_stats?.storageMb, ' MB')}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                      <div>
                        <p className="text-[10px] text-muted-foreground">Updated</p>
                        <p className="text-xs font-medium">{formatCompactDateTime(idx.last_indexed_at)}</p>
                      </div>
                    </div>
                  </div>
                ) : null}

                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-xs h-7"
                    disabled={busyIndex === idx.index_name}
                    onClick={() => handleReindex(idx.index_name, 'all')}
                  >
                    <RefreshCcw className="h-3 w-3 mr-1" />
                    {busyIndex === idx.index_name ? 'Queueing...' : 'Full Reindex'}
                  </Button>
                </div>

                <div className="text-xs text-muted-foreground">
                  Last updated: {formatCompactDateTime(idx.last_indexed_at)}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
