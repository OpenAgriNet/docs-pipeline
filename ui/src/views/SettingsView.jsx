import React, { useEffect, useState } from 'react'
import { AlertCircle, CheckCircle, ChevronDown, ChevronUp, Clock, RotateCcw, Save } from 'lucide-react'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '../components/ui/alert-dialog'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import { DEFAULT_SEARCH_SETTINGS, fetchJson, formatCompactDateTime } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'

function SettingsNotice({ tone = 'warning', message }) {
  const classes = tone === 'success'
    ? 'border-success/30 bg-success/10 text-success'
    : tone === 'error'
      ? 'border-destructive/30 bg-destructive/10 text-destructive'
      : 'border-warning/20 bg-warning/10 text-warning'

  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${classes}`}>
      <div className="flex items-center gap-2">
        {tone === 'success' ? <CheckCircle className="h-4 w-4 shrink-0" /> : <AlertCircle className="h-4 w-4 shrink-0" />}
        <span>{message}</span>
      </div>
    </div>
  )
}

export default function SettingsView() {
  const { hasPermission } = useAuth()
  const canAdmin = hasPermission('admin')
  const [settings, setSettings] = useState(DEFAULT_SEARCH_SETTINGS)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [saveSuccess, setSaveSuccess] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState([])
  const [showResetConfirm, setShowResetConfirm] = useState(false)

  useEffect(() => {
    fetchSettings()
  }, [])

  async function fetchSettings() {
    setError('')
    try {
      const [data, historyData] = await Promise.all([
        fetchJson('/settings/search'),
        fetchJson('/settings/search/audit?limit=20')
      ])
      setSettings(data)
      setHistory(historyData.logs || [])
      setDirty(false)
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  function update(key, value) {
    setSettings(current => ({ ...current, [key]: value }))
    setDirty(true)
    setSaveSuccess(false)
  }

  async function handleSave() {
    setError('')
    try {
      const data = await fetchJson('/settings/search', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
      })
      setSettings(data)
      setDirty(false)
      setSaveSuccess(true)
      fetchSettings()
    } catch (saveError) {
      setError(saveError.message)
    }
  }

  async function handleReset() {
    setError('')
    try {
      const data = await fetchJson('/settings/search/reset', { method: 'POST' })
      setSettings(data)
      setDirty(false)
      setSaveSuccess(false)
      fetchSettings()
    } catch (resetError) {
      setError(resetError.message)
    }
  }

  const fieldOptions = {
    searchMethod: ['HYBRID', 'TENSOR', 'LEXICAL'],
    rankingMethod: ['rrf', 'normalize_linear'],
    queryExpansionProfile: Array.from(new Set(['none', 'basic', 'advanced', DEFAULT_SEARCH_SETTINGS.queryExpansionProfile, settings.queryExpansionProfile].filter(Boolean))),
    rerankMode: Array.from(new Set(['none', 'bm25lite', 'rrf-lite', 'heuristic', 'cross-encoder', 'colbert', settings.rerankMode].filter(Boolean))),
  }

  const sections = [
    {
      title: 'Search Method',
      description: 'Configure the primary search strategy and ranking behavior.',
      fields: [
        { key: 'searchMethod', label: 'Search Method', type: 'select', help: 'Algorithm used for retrieval queries.' },
        { key: 'rankingMethod', label: 'Ranking Method', type: 'select', help: 'How multi-method candidates are combined.' },
        { key: 'alpha', label: 'Alpha', type: 'number', step: 0.1, help: 'Balance between tensor-heavy and lexical-heavy retrieval.' },
        { key: 'hybridRrfK', label: 'Hybrid RRF K', type: 'number', help: 'Smoothing constant used during reciprocal rank fusion.' }
      ]
    },
    {
      title: 'Result Limits',
      description: 'Control how many results are returned and how candidates are selected.',
      fields: [
        { key: 'limit', label: 'Result Limit', type: 'number', help: 'Maximum number of results returned per query.' },
        { key: 'candidateCap', label: 'Candidate Cap', type: 'number', help: 'Maximum initial candidates before filtering or reranking.' },
        { key: 'candidateMultiplier', label: 'Candidate Multiplier', type: 'number', help: 'Multiplier applied to result limit when generating candidates.' },
        { key: 'maxChunksPerDoc', label: 'Max Chunks per Doc', type: 'number', help: 'Caps repeated matches from a single document.' }
      ]
    },
    {
      title: 'Index Configuration',
      description: 'Target index and performance settings.',
      fields: [
        { key: 'indexName', label: 'Index Name', type: 'text', help: 'Default search index queried by the workbench and persisted settings.' },
        { key: 'efSearch', label: 'efSearch', type: 'number', help: 'Search breadth for the vector index. Higher usually improves recall.' }
      ]
    },
    {
      title: 'Advanced Options',
      description: 'Feature flags and optional processing steps.',
      fields: [
        { key: 'showHighlights', label: 'Show Highlights', type: 'boolean', help: 'Annotate matching terms inside result excerpts.' },
        { key: 'useE5Prefix', label: 'Use E5 Prefix', type: 'boolean', help: 'Prefix queries for E5-style embedding behavior.' },
        { key: 'excludeReference', label: 'Exclude Reference', type: 'boolean', help: 'Suppress bibliography and reference-style chunks.' },
        { key: 'queryExpansionProfile', label: 'Query Expansion', type: 'select', help: 'Optional query expansion profile applied before retrieval.' },
        { key: 'rerankMode', label: 'Rerank Mode', type: 'select', help: 'Optional second-pass reranking strategy.' }
      ]
    }
  ]

  if (loading) {
    return (
      <div className="p-6 max-w-3xl mx-auto space-y-4">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-4 w-48 mt-1" />
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-[150px] rounded-lg mt-4" />
        ))}
      </div>
    )
  }

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-serif text-2xl font-semibold text-foreground">Settings</h1>
          <p className="mt-1 text-sm text-muted-foreground">Persisted search defaults</p>
        </div>
        <div className="flex items-center gap-2">
          {!canAdmin && (
            <span className="text-xs text-warning">Read-only — admin permission required to change settings</span>
          )}
          <Button variant="outline" size="sm" onClick={() => setShowResetConfirm(true)} disabled={!canAdmin}>
            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
            Reset
          </Button>
          <Button size="sm" onClick={handleSave} disabled={!dirty || !canAdmin}>
            <Save className="h-3.5 w-3.5 mr-1.5" />
            Save
          </Button>
        </div>
      </div>

      {dirty || saveSuccess || error ? (
        <div className="space-y-2">
          {dirty ? <SettingsNotice message="Unsaved changes are local to this page." /> : null}
          {saveSuccess ? <SettingsNotice tone="success" message="Settings saved successfully" /> : null}
          {error ? <SettingsNotice tone="error" message={error} /> : null}
        </div>
      ) : null}

      {sections.map(section => (
        <div key={section.title} className="panel">
          <div className="panel-header">
            <h2 className="text-sm font-medium text-foreground">{section.title}</h2>
            <p className="text-xs text-muted-foreground mt-0.5">{section.description}</p>
          </div>
          <div className="divide-y divide-border">
            {section.fields.map(field => (
              <div key={field.key} className="px-4 py-3 flex items-center justify-between gap-4">
                <div className="min-w-0">
                  <label className="text-sm font-medium text-foreground whitespace-nowrap">{field.label}</label>
                  {field.help && <p className="text-[10px] text-muted-foreground mt-0.5">{field.help}</p>}
                </div>
                {field.type === 'select' ? (
                  <select
                    className="rounded-md border border-input bg-background px-3 py-1.5 text-sm w-48 shrink-0"
                    value={String(settings[field.key])}
                    onChange={event => update(field.key, event.target.value)}
                  >
                    {fieldOptions[field.key].map(option => (
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                ) : field.type === 'boolean' ? (
                  <button
                    className={`w-10 h-5 rounded-full transition-colors shrink-0 ${Boolean(settings[field.key]) ? 'bg-primary' : 'bg-muted'}`}
                    onClick={() => update(field.key, !settings[field.key])}
                  >
                    <div className={`w-4 h-4 rounded-full bg-card shadow transition-transform ${Boolean(settings[field.key]) ? 'translate-x-5' : 'translate-x-0.5'}`} />
                  </button>
                ) : (
                  <Input
                    type={field.type}
                    step={field.step}
                    value={settings[field.key]}
                    onChange={event => update(field.key, field.type === 'number' ? Number(event.target.value) : event.target.value)}
                    className="h-8 w-48 shrink-0"
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      ))}

      <div className="panel">
        <button className="panel-header flex items-center justify-between w-full cursor-pointer" onClick={() => setShowHistory(value => !value)}>
          <div className="flex items-center gap-2">
            <Clock className="h-4 w-4 text-muted-foreground" />
            <h2 className="text-sm font-medium text-foreground">Change History</h2>
          </div>
          {showHistory ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </button>
        {showHistory ? (
          <div className="divide-y divide-border">
            {history.length ? history.map(entry => (
              <div key={entry.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                <span className="font-mono text-xs font-medium text-foreground">{entry.field_name || entry.field || 'setting'}</span>
                <div className="flex items-center gap-1 text-xs">
                  <span className="text-muted-foreground line-through">{entry.old_value ?? '—'}</span>
                  <span className="text-muted-foreground">→</span>
                  <span className="font-medium text-foreground">{entry.new_value ?? '—'}</span>
                </div>
                <span className="ml-auto text-xs text-muted-foreground">{entry.actor || 'system'}</span>
                <span className="text-xs text-muted-foreground">{formatCompactDateTime(entry.timestamp)}</span>
              </div>
            )) : (
              <div className="px-4 py-8 text-center text-sm text-muted-foreground">No settings audit entries yet.</div>
            )}
          </div>
        ) : null}
      </div>

      <AlertDialog open={showResetConfirm} onOpenChange={setShowResetConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Reset to Defaults</AlertDialogTitle>
            <AlertDialogDescription>
              This will reset all search settings to their factory defaults. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleReset}>Reset All</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
