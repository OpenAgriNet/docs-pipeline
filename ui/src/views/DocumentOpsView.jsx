import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  AlertCircle,
  AlertTriangle,
  Bug,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  ClipboardList,
  Database,
  Eye,
  ExternalLink,
  FileCode,
  FileText,
  Layers,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
} from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'
import DocumentHeaderSummary from '../components/DocumentHeaderSummary'
import PipelineStepper from '../components/PipelineStepper'
import PagePager from '../components/PagePager'
import SourcePdfPreview from '../components/SourcePdfPreview'
import { StageBadge } from '../components/StageBadge'
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
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Checkbox } from '../components/ui/checkbox'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { Textarea } from '../components/ui/textarea'
import {
  fetchJson,
  formatCompactDateTime,
  getAuditActionOptions,
  getDocumentFileLabel,
  getDocumentListLabel,
  getStageLabel,
  summarizeAuditAction,
  summarizeAvailableAction,
} from '../lib/pipelineUi'

function getChunkEmptyMessage(doc) {
  const stage = doc?.stage
  if (stage === 'registered' || stage === 'ocr_processing') return 'Content sections are not ready yet. Text is still being extracted.'
  if (stage === 'ocr_review') return 'Approve the extracted text before content can be prepared.'
  if (stage === 'translation_processing') return 'Translation is still running.'
  if (stage === 'translation_review') return 'Approve the translation before content can be prepared.'
  if (stage === 'chunking') return 'Content sections are being prepared for this document.'
  if (stage === 'failed') return 'Content is blocked because processing failed.'
  return 'No content sections are available for this document yet.'
}

function EmptyPanel({ icon: Icon, title, subtitle, compact = false }) {
  return (
    <div className={compact ? 'px-3 py-6 text-center' : 'p-8 text-center'}>
      <Icon className={`mx-auto mb-2 text-muted-foreground/30 ${compact ? 'h-6 w-6' : 'h-8 w-8'}`} />
      <p className="text-sm font-medium text-foreground">{title}</p>
      {subtitle && <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>}
    </div>
  )
}

function PanelNotice({ tone = 'error', title, message }) {
  const toneClasses = tone === 'warning'
    ? 'bg-warning/10 border-warning/20 text-warning'
    : 'bg-destructive/10 border-destructive/20 text-destructive'

  return (
    <div className={`rounded-md border p-3 text-sm ${toneClasses}`}>
      <div className="flex items-start gap-2">
        <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
        <div className="min-w-0">
          {title ? <p className="font-medium">{title}</p> : null}
          <p className={title ? 'mt-0.5 break-words' : 'break-words'}>{message}</p>
        </div>
      </div>
    </div>
  )
}

// Client-side preview only — matches pipeline/scheme_catalog.py's
// derive_scheme_code() closely enough for instant feedback, but the server
// is the authoritative, collision-checked source of truth for the real code.
const SCHEME_CODE_STOPWORDS = new Set(['a', 'an', 'the', 'of', 'on', 'for', 'and', 'or', 'to', 'in', 'at', 'by', 'with', '&'])

function previewSchemeCode(title) {
  const words = (title || '').match(/[A-Za-z0-9]+/g) || []
  const letters = words
    .filter(w => !SCHEME_CODE_STOPWORDS.has(w.toLowerCase()))
    .map(w => w[0].toLowerCase())
    .join('')
  if (letters.length >= 2) return letters
  const slug = (title || '').toLowerCase().replace(/[^a-z0-9]+/g, '')
  return slug.slice(0, 8) || 'scheme'
}

const DOCUMENT_KIND_OPTIONS = [
  { value: 'scheme', label: 'Scheme' },
  { value: 'advisory', label: 'Advisory' },
  { value: 'video', label: 'Video' },
  { value: '__custom__', label: 'Custom…' },
]

function DocumentClassificationPanel({ doc, workflowId, canClassify, onSaved }) {
  const hasKind = doc.document_kind && doc.document_kind !== 'document'
  const [kind, setKind] = useState(hasKind && !DOCUMENT_KIND_OPTIONS.some(o => o.value === doc.document_kind) ? '__custom__' : (doc.document_kind || ''))
  const [customKind, setCustomKind] = useState(hasKind && !DOCUMENT_KIND_OPTIONS.some(o => o.value === doc.document_kind) ? doc.document_kind : '')
  const [schemeName, setSchemeName] = useState(doc.scheme_name || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const effectiveKind = kind === '__custom__' ? customKind.trim().toLowerCase() : kind
  const isScheme = effectiveKind === 'scheme'
  const canSave = Boolean(effectiveKind) && (!isScheme || schemeName.trim().length > 0)

  async function save() {
    if (!canSave || saving) return
    setSaving(true)
    setError('')
    try {
      await fetchJson(`/documents/${workflowId}/scheme-metadata`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          document_kind: effectiveKind,
          ...(isScheme ? { scheme_name: schemeName.trim() } : {}),
        }),
      })
      await onSaved()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const alreadySaved = doc.document_kind === effectiveKind && (!isScheme || doc.scheme_name === schemeName.trim())

  return (
    <div className="space-y-2 rounded-md border border-border bg-muted/30 p-3">
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Document type — used by the Master Catalog / AI layer
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <span className="text-[10px] text-muted-foreground">Type</span>
          <Select value={kind} onValueChange={setKind} disabled={!canClassify}>
            <SelectTrigger className="h-8 w-[160px] text-xs">
              <SelectValue placeholder="Select type" />
            </SelectTrigger>
            <SelectContent>
              {DOCUMENT_KIND_OPTIONS.map(opt => (
                <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {kind === '__custom__' && (
          <div className="flex flex-col gap-1">
            <span className="text-[10px] text-muted-foreground">Custom type</span>
            <Input
              className="h-8 w-[140px] text-xs"
              value={customKind}
              disabled={!canClassify}
              onChange={e => setCustomKind(e.target.value)}
              placeholder="e.g. faq"
            />
          </div>
        )}
        {isScheme && (
          <>
            <div className="flex flex-col gap-1">
              <span className="text-[10px] text-muted-foreground">Scheme name</span>
              <Input
                className="h-8 w-[260px] text-xs"
                value={schemeName}
                disabled={!canClassify}
                onChange={e => setSchemeName(e.target.value)}
                placeholder="e.g. National Mission on Natural Farming"
              />
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-[10px] text-muted-foreground">Scheme code (auto)</span>
              <span className="flex h-8 items-center rounded-md border border-dashed border-input px-2 text-xs text-muted-foreground">
                {doc.scheme_code && doc.scheme_name === schemeName.trim() ? doc.scheme_code : (schemeName.trim() ? previewSchemeCode(schemeName) : '—')}
              </span>
            </div>
          </>
        )}
        {canClassify && (
          <Button size="sm" className="h-8 text-xs" disabled={!canSave || saving || alreadySaved} onClick={save}>
            {saving ? 'Saving…' : alreadySaved ? 'Saved' : 'Save'}
          </Button>
        )}
      </div>
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
      {!canClassify && !hasKind ? (
        <p className="text-xs text-muted-foreground">You don't have permission to set document type.</p>
      ) : null}
    </div>
  )
}

// Which permission each mutating action requires. Approvals / edits are
// review; anything that re-runs pipeline stages or touches the index is pipeline.
const ACTION_PERMISSION = {
  approve_ocr: 'review',
  approve_translation: 'review',
  approve_chunks: 'review',
  // Publishing to DEV is its own permission — state_contributor has 'review'
  // (so OCR / translation / chunk approvals work) but not this one.
  approve_ingestion: 'approve_ingestion',
  approve_prod: 'admin',
  request_prod_ready: 'review',
  retry_translation: 'pipeline',
  reingest_document: 'pipeline',
  mark_reindex_required: 'pipeline',
  clear_reindex_required: 'pipeline',
  // Super admins delete anything; state admins delete only their own uploads
  // (the API re-checks ownership and blocks purge for non-super-admins).
  disable_document: 'delete_own',
  restore_document: 'admin',
}

/** Stages that are still running work — poll lightly for progress. */
const ACTIVE_STAGES = new Set([
  'registered',
  'ocr_processing',
  'translation_processing',
  'chunking',
  'ingesting',
  'ingesting_prod',
])

// These actions just signal a Temporal workflow and return immediately — the
// actual stage transition happens later, off-request, once the workflow
// picks up the signal and its activities finish. The button must stay
// disabled until that transition is actually observed, not just until the
// signal call itself returns.
const STAGE_TRANSITION_ACTIONS = new Set([
  'approve_ocr',
  'approve_translation',
  'approve_chunks',
  'approve_ingestion',
  'approve_prod',
  'retry_translation',
  'reingest_document',
])

const STAGE_POLL_INTERVAL_MS = 1500
const STAGE_POLL_MAX_ATTEMPTS = 40 // ~60s safety net so a stuck workflow doesn't wedge the button forever

function stageWantsPages(stage, pageCount = 0) {
  // Skip empty registered docs; otherwise pages are local SQLite (fast) and power OCR/translation.
  if ((stage === 'registered' || stage === 'ocr_processing') && !pageCount) return false
  return true
}

function stageWantsChunks(stage, chunkCount = 0) {
  if (chunkCount > 0) return true
  return [
    'chunking',
    'chunk_review',
    'ready_for_ingestion',
    'ingesting',
    'approval_for_prod',
    'ingesting_prod',
    'completed',
  ].includes(stage || '')
}

function stageWantsRuntime(stage) {
  return ACTIVE_STAGES.has(stage || '')
}

export default function DocumentOpsView() {
  const { workflowId } = useParams()
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const canReview = hasPermission('review')
  const canPipeline = hasPermission('pipeline')
  const canAdmin = hasPermission('admin')
  // State View has search only — no create/edit/delete/upload/approve.
  const canEdit = canReview
  const isViewOnly = !canEdit && !canPipeline && !canAdmin
  const canRunAction = (action) => {
    if (isViewOnly) return false
    const needed = ACTION_PERMISSION[action]
    return needed ? hasPermission(needed) : canReview
  }
  const [searchParams] = useSearchParams()
  const [doc, setDoc] = useState(null)
  const [pages, setPages] = useState([])
  const [chunks, setChunks] = useState([])
  const [indexChunks, setIndexChunks] = useState([])
  const [indexStatus, setIndexStatus] = useState(null)
  const [confirmRemoveOpen, setConfirmRemoveOpen] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [jobs, setJobs] = useState([])
  const [runtime, setRuntime] = useState(null)
  const [stageIo, setStageIo] = useState(null)
  const [panelErrors, setPanelErrors] = useState({})
  const [activeTab, setActiveTab] = useState('ocr')
  const [loading, setLoading] = useState(true)
  const [currentPage, setCurrentPage] = useState(1)
  const [message, setMessage] = useState('')
  const [pageEdits, setPageEdits] = useState({})
  const [chunkEdits, setChunkEdits] = useState({})
  const [translationEdits, setTranslationEdits] = useState({})
  const [auditFilter, setAuditFilter] = useState('all')
  const [auditExpanded, setAuditExpanded] = useState(new Set())
  const [auditLogs, setAuditLogs] = useState([])
  const [highlightedChunk, setHighlightedChunk] = useState(null)
  const [panelLoading, setPanelLoading] = useState({})
  const [actionPending, setActionPending] = useState(null)
  const requestIdRef = useRef(0)
  const attemptedPanelsRef = useRef({})
  const activeTabRef = useRef(activeTab)
  activeTabRef.current = activeTab
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const setPanelBusy = useCallback((key, busy) => {
    setPanelLoading(prev => {
      if (Boolean(prev[key]) === Boolean(busy)) return prev
      return { ...prev, [key]: busy }
    })
  }, [])

  const patchPanelError = useCallback((key, errorMessage) => {
    setPanelErrors(prev => {
      if (errorMessage) {
        if (prev[key] === errorMessage) return prev
        return { ...prev, [key]: errorMessage }
      }
      if (!(key in prev)) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }, [])

  const loadPages = useCallback(async (wid) => {
    setPanelBusy('pages', true)
    try {
      const value = await fetchJson(`/documents/${wid}/pages`)
      setPages(Array.isArray(value) ? value : [])
      patchPanelError('pages', null)
    } catch (error) {
      setPages([])
      patchPanelError('pages', error.message || 'Unable to load pages.')
    } finally {
      setPanelBusy('pages', false)
    }
  }, [patchPanelError, setPanelBusy])

  const loadChunks = useCallback(async (wid) => {
    setPanelBusy('chunks', true)
    try {
      const value = await fetchJson(`/documents/${wid}/chunks?include_excluded=true`)
      setChunks(Array.isArray(value) ? value : [])
      patchPanelError('chunks', null)
    } catch (error) {
      setChunks([])
      patchPanelError('chunks', error.message || 'Unable to load chunks.')
    } finally {
      setPanelBusy('chunks', false)
    }
  }, [patchPanelError, setPanelBusy])

  const loadIndex = useCallback(async (wid) => {
    setPanelBusy('index', true)
    try {
      const status = (await fetchJson(`/documents/${wid}/qdrant`)) || {}
      setIndexStatus(status)
      setIndexChunks(Array.isArray(status.hits) ? status.hits : [])
      patchPanelError('index', null)
    } catch (error) {
      setIndexStatus(null)
      setIndexChunks([])
      patchPanelError('index', error.message || 'Unable to load index status.')
    } finally {
      setPanelBusy('index', false)
    }
  }, [patchPanelError, setPanelBusy])

  const loadRuntimeAndJobs = useCallback(async (wid) => {
    setPanelBusy('runtime', true)
    try {
      const [runtimeResult, jobsResult] = await Promise.allSettled([
        fetchJson(`/documents/${wid}/runtime`),
        fetchJson(`/documents/${wid}/jobs`),
      ])
      if (runtimeResult.status === 'fulfilled') {
        setRuntime(runtimeResult.value)
        patchPanelError('runtime', null)
      } else {
        setRuntime(null)
        patchPanelError('runtime', runtimeResult.reason?.message || 'Unable to load runtime.')
      }
      if (jobsResult.status === 'fulfilled') {
        setJobs(Array.isArray(jobsResult.value) ? jobsResult.value : [])
        patchPanelError('jobs', null)
      } else {
        setJobs([])
        patchPanelError('jobs', jobsResult.reason?.message || 'Unable to load jobs.')
      }
    } finally {
      setPanelBusy('runtime', false)
    }
  }, [patchPanelError, setPanelBusy])

  const loadStageIo = useCallback(async (wid) => {
    setPanelBusy('stageIo', true)
    try {
      const value = await fetchJson(`/documents/${wid}/stage-io`)
      setStageIo(value)
      patchPanelError('stageIo', null)
    } catch (error) {
      setStageIo(null)
      patchPanelError('stageIo', error.message || 'Unable to load stage I/O.')
    } finally {
      setPanelBusy('stageIo', false)
    }
  }, [patchPanelError, setPanelBusy])

  const loadAudit = useCallback(async (wid) => {
    setPanelBusy('audit', true)
    try {
      const value = await fetchJson(`/documents/${wid}/audit?limit=100`)
      setAuditLogs(value?.logs || [])
      patchPanelError('audit', null)
    } catch (error) {
      setAuditLogs([])
      patchPanelError('audit', error.message || 'Unable to load audit log.')
    } finally {
      setPanelBusy('audit', false)
    }
  }, [patchPanelError, setPanelBusy])

  /**
   * Progressive load:
   * 1) document shell (unblocks UI immediately)
   * 2) SQLite pages/chunks needed for the active review surface
   * 3) heavy panels (Qdrant / Temporal / audit) only when tab or stage needs them
   */
  const load = useCallback(async ({ soft = false, forcePanels = null } = {}) => {
    const requestId = ++requestIdRef.current
    const wid = workflowId
    const tab = activeTabRef.current

    try {
      if (!soft) setLoading(true)
      const docData = await fetchJson(`/documents/${wid}`)
      if (requestId !== requestIdRef.current) return
      setDoc(docData)
      setMessage('')
      // Unblock shell as soon as document metadata is ready.
      if (!soft) setLoading(false)

      const stage = docData?.stage
      const force = forcePanels || {}
      const pageCount = Number(docData?.page_count || 0)
      const chunkCount = Number(docData?.chunk_count || 0)
      const wantPages = force.pages || stageWantsPages(stage, pageCount) || tab === 'ocr' || tab === 'translation'
      const wantChunks = force.chunks || stageWantsChunks(stage, chunkCount) || tab === 'chunks'
      const wantRuntime = force.runtime || stageWantsRuntime(stage) || tab === 'debug'
      const wantIndex = force.index || tab === 'index'
      const wantStageIo = force.stageIo || tab === 'debug'
      const wantAudit = force.audit || tab === 'audit'

      const localTasks = []
      if (wantPages) {
        attemptedPanelsRef.current.pages = true
        localTasks.push(loadPages(wid))
      }
      if (wantChunks) {
        attemptedPanelsRef.current.chunks = true
        localTasks.push(loadChunks(wid))
      }
      // Local SQLite panels in parallel; do not wait on remote index/Temporal for first paint.
      await Promise.allSettled(localTasks)
      if (requestId !== requestIdRef.current) return

      const remoteTasks = []
      if (wantRuntime) {
        attemptedPanelsRef.current.runtime = true
        remoteTasks.push(loadRuntimeAndJobs(wid))
      }
      if (wantIndex) {
        attemptedPanelsRef.current.index = true
        remoteTasks.push(loadIndex(wid))
      }
      if (wantStageIo) {
        attemptedPanelsRef.current.stageIo = true
        remoteTasks.push(loadStageIo(wid))
      }
      if (wantAudit) {
        attemptedPanelsRef.current.audit = true
        remoteTasks.push(loadAudit(wid))
      }
      // Fire-and-forget remote panels so a slow Qdrant/Temporal never freezes the cockpit.
      if (remoteTasks.length) {
        Promise.allSettled(remoteTasks)
      }
    } catch (error) {
      if (requestId !== requestIdRef.current) return
      setDoc(null)
      setPanelErrors({})
      setMessage(error.message)
      if (!soft) setLoading(false)
    }
  }, [workflowId, loadPages, loadChunks, loadIndex, loadRuntimeAndJobs, loadStageIo, loadAudit])

  useEffect(() => {
    const tab = searchParams.get('tab')
    const chunk = searchParams.get('chunk')
    if (tab) setActiveTab(tab)
    if (chunk) {
      const parsed = Number(chunk)
      setHighlightedChunk(Number.isFinite(parsed) ? parsed : null)
    } else {
      setHighlightedChunk(null)
    }
  }, [workflowId, searchParams])

  // When no ?tab= is set, open the review tab that matches the pipeline stage
  // so operators don't hit approve-ocr after the doc has already moved on.
  useEffect(() => {
    if (searchParams.get('tab') || !doc?.stage) return
    const stage = doc.stage
    if (stage === 'ocr_review' || stage === 'ocr_processing' || stage === 'registered') {
      setActiveTab('ocr')
    } else if (stage === 'translation_review' || stage === 'translation_processing') {
      setActiveTab('translation')
    } else if (stage === 'chunk_review' || stage === 'chunking' || stage === 'ready_for_ingestion') {
      setActiveTab('chunks')
    } else if (stage === 'ingesting' || stage === 'approval_for_prod' || stage === 'ingesting_prod' || stage === 'completed') {
      setActiveTab('index')
    }
  }, [doc?.stage, workflowId, searchParams])

  useEffect(() => {
    if (loading || activeTab !== 'chunks' || highlightedChunk == null) return
    if (!chunks.some(chunk => chunk.chunk_number === highlightedChunk)) return
    const frame = requestAnimationFrame(() => {
      document.getElementById(`chunk-card-${highlightedChunk}`)?.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      })
    })
    return () => cancelAnimationFrame(frame)
  }, [loading, activeTab, highlightedChunk, chunks])

  // Initial load + reset panels when switching documents.
  useEffect(() => {
    attemptedPanelsRef.current = {}
    setPages([])
    setChunks([])
    setIndexChunks([])
    setIndexStatus(null)
    setJobs([])
    setRuntime(null)
    setStageIo(null)
    setAuditLogs([])
    setPanelErrors({})
    setPanelLoading({})
    setMessage('')
    load({ soft: false })
  }, [workflowId, load])

  // Lazy-load heavy panels once when the operator opens those tabs.
  useEffect(() => {
    if (!doc || loading) return
    const wid = workflowId
    const attempted = attemptedPanelsRef.current
    if ((activeTab === 'ocr' || activeTab === 'translation') && !attempted.pages) {
      attempted.pages = true
      loadPages(wid)
    }
    if (activeTab === 'chunks' && !attempted.chunks) {
      attempted.chunks = true
      loadChunks(wid)
    }
    if (activeTab === 'index' && !attempted.index) {
      attempted.index = true
      loadIndex(wid)
    }
    if (activeTab === 'debug') {
      if (!attempted.runtime) {
        attempted.runtime = true
        loadRuntimeAndJobs(wid)
      }
      if (!attempted.stageIo) {
        attempted.stageIo = true
        loadStageIo(wid)
      }
    }
    if (activeTab === 'audit' && !attempted.audit) {
      attempted.audit = true
      loadAudit(wid)
    }
  }, [
    activeTab,
    doc,
    loading,
    workflowId,
    loadPages,
    loadChunks,
    loadIndex,
    loadRuntimeAndJobs,
    loadStageIo,
    loadAudit,
  ])

  // Light polling only while the pipeline is actively processing.
  useEffect(() => {
    if (!doc?.stage || !ACTIVE_STAGES.has(doc.stage)) return undefined
    const interval = setInterval(() => {
      load({ soft: true })
    }, 5000)
    return () => clearInterval(interval)
  }, [doc?.stage, load])

  async function reloadAfterMutation() {
    const tab = activeTabRef.current
    await load({
      soft: true,
      forcePanels: {
        pages: true,
        chunks: true,
        runtime: true,
        index: tab === 'index',
        stageIo: tab === 'debug',
        audit: tab === 'audit',
      },
    })
  }

  // Signal-based actions return as soon as the Temporal signal is delivered,
  // well before the workflow actually processes it and moves `stage`. Poll
  // the document directly (bypassing the heavier `load()` pipeline) until
  // stage visibly moves off `previousStage`, so the button stays disabled
  // for the real duration of backend processing, not just the request.
  async function waitForStageChange(previousStage) {
    const targetWorkflowId = workflowId
    for (let attempt = 0; attempt < STAGE_POLL_MAX_ATTEMPTS; attempt++) {
      await new Promise(resolve => setTimeout(resolve, STAGE_POLL_INTERVAL_MS))
      if (!mountedRef.current || workflowId !== targetWorkflowId) return
      try {
        const latest = await fetchJson(`/documents/${targetWorkflowId}`)
        if (latest?.stage && latest.stage !== previousStage) {
          if (mountedRef.current && workflowId === targetWorkflowId) {
            await reloadAfterMutation()
          }
          return
        }
      } catch {
        // transient fetch error mid-poll — keep trying until max attempts
      }
    }
  }

  async function runAction(action) {
    if (actionPending) return
    setActionPending(action)
    setMessage('')
    const stageBeforeAction = doc?.stage
    try {
      if (action === 'mark_reindex_required') {
        await fetchJson(`/documents/${workflowId}/mark-reindex-required`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'Marked manually from document cockpit' }),
        })
      } else if (action === 'clear_reindex_required') {
        await fetchJson(`/documents/${workflowId}/clear-reindex-required`, { method: 'POST' })
      } else if (action === 'reingest_document') {
        await fetchJson(`/documents/${workflowId}/reingest`, { method: 'POST' })
      } else if (action === 'disable_document') {
        // Opens the confirm dialog; the actual delete runs in confirmRemoveDocument.
        setConfirmRemoveOpen(true)
        return
      } else if (action === 'restore_document') {
        await fetchJson(`/documents/${workflowId}/restore`, { method: 'POST' })
      } else {
        await fetchJson(`/documents/${workflowId}/${action.replace(/_/g, '-')}`, { method: 'POST' })
      }
      setMessage(`${summarizeAvailableAction(action)} triggered.`)
      await reloadAfterMutation()

      if (STAGE_TRANSITION_ACTIONS.has(action) && stageBeforeAction) {
        await waitForStageChange(stageBeforeAction)
      }
    } catch (error) {
      setMessage(error.message)
    } finally {
      if (mountedRef.current) setActionPending(null)
    }
  }

  async function savePage(pageNumber, text) {
    try {
      await fetchJson(`/documents/${workflowId}/pages/${pageNumber}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edited_markdown: text })
      })
      setMessage('Page saved')
      const next = { ...pageEdits }
      delete next[pageNumber]
      setPageEdits(next)
      await reloadAfterMutation()
    } catch (err) {
      setMessage(err.message)
    }
  }

  async function saveTranslation(pageNumber, text) {
    try {
      await fetchJson(`/documents/${workflowId}/pages/${pageNumber}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edited_translation: text, translation_reviewed: true })
      })
      setMessage('Translation saved')
      const next = { ...translationEdits }
      delete next[pageNumber]
      setTranslationEdits(next)
      await reloadAfterMutation()
    } catch (err) {
      setMessage(err.message)
    }
  }

  async function saveChunk(chunkNumber, text) {
    try {
      await fetchJson(`/documents/${workflowId}/chunks/${chunkNumber}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edited_text: text })
      })
      setMessage(`Chunk ${chunkNumber} saved`)
      const next = { ...chunkEdits }
      delete next[chunkNumber]
      setChunkEdits(next)
      await reloadAfterMutation()
    } catch (err) {
      setMessage(err.message)
    }
  }

  const visibleActions = (doc?.available_actions || []).filter(
    action => !['disable_document', 'restore_document', 'inspect_runtime', 'reconcile_document'].includes(action)
      // super_admin already sees the real "Approve publish to prod" button at this
      // stage — the request is only useful as a nudge from a non-admin reviewer.
      && !(action === 'request_prod_ready' && canAdmin)
      && canRunAction(action)
  )
  const canRemoveDocument = canAdmin && (doc?.available_actions || []).includes('disable_document')
  // 'document' is never offered as a choice in the classification panel (only
  // scheme/advisory/video/custom are) — seeing it here means the panel was
  // never used. Mirrors the same check the reingest endpoint enforces server-side.
  const isDocClassified = Boolean(
    (doc?.document_kind && doc.document_kind !== 'document') || doc?.scheme_code || doc?.scheme_name
  )
  const ingestBlockedByClassification = doc?.stage === 'ready_for_ingestion' && !isDocClassified
  const sortedPages = useMemo(() => [...pages].sort((a, b) => a.page_number - b.page_number), [pages])
  const reviewedPages = useMemo(() => pages.filter(p => p.is_reviewed).length, [pages])
  const reviewedChunks = useMemo(() => chunks.filter(c => c.is_reviewed).length, [chunks])
  const translatedPages = useMemo(
    () => pages.filter(p => p.translation_reviewed || p.translated_markdown || p.edited_translation).length,
    [pages]
  )
  const currentPageRecord = useMemo(
    () => sortedPages.find(p => p.page_number === currentPage) || sortedPages[0] || null,
    [sortedPages, currentPage]
  )

  useEffect(() => {
    if (!sortedPages.length) return
    if (!sortedPages.some(p => p.page_number === currentPage)) {
      setCurrentPage(sortedPages[0].page_number)
    }
  }, [sortedPages, currentPage])

  const filteredAudit = auditFilter === 'all' ? auditLogs : auditLogs.filter(e => e.action_type === auditFilter)
  const auditOptions = getAuditActionOptions(auditLogs)
  const currentPageLanguage = currentPageRecord?.detected_language || currentPageRecord?.language_detected || ''
  const translationEmptySubtitle = String(currentPageLanguage || '').toLowerCase().startsWith('en')
    ? 'No sections detected to translate on this page'
    : 'This page was not detected as needing translation'
  const currentPageOcrText = currentPageRecord
    ? (currentPageRecord.ocr_markdown ?? currentPageRecord.original_markdown ?? '')
    : ''
  const pageText = currentPageRecord ? (pageEdits[currentPage] ?? currentPageRecord.edited_markdown ?? currentPageOcrText ?? '') : ''
  // What the translation stage actually reads (mirrors the backend's
  // `edited_markdown or original_markdown` precedence) — must be shown as
  // "Original text" in the Translation Review tab so the reviewer is
  // comparing the translation against the text that produced it, not the
  // pre-correction OCR output.
  const currentPageTranslationSourceText = currentPageRecord
    ? (currentPageRecord.edited_markdown || currentPageOcrText)
    : ''
  const translationText = currentPageRecord ? (translationEdits[currentPage] ?? (currentPageRecord.edited_translation || currentPageRecord.translated_markdown || '')) : ''
  const isOcrPending = !currentPageRecord && (doc?.stage === 'registered' || doc?.stage === 'ocr_processing')
  const canApproveOcr = canReview && doc?.stage === 'ocr_review'
  const canApproveTranslation = canReview && doc?.stage === 'translation_review'
  const canApproveChunks = canReview && doc?.stage === 'chunk_review'
  const ocrAlreadyPast = doc?.stage && !['registered', 'ocr_processing', 'ocr_review'].includes(doc.stage)
  const chunkingProgress = runtime?.chunking_progress || null
  const chunkingPercent = Math.max(0, Math.min(100, Number(chunkingProgress?.percent || 0)))
  const indexedChunkCount = Number.isFinite(indexStatus?.indexed_chunk_count)
    ? indexStatus.indexed_chunk_count
    : indexChunks.length
  const hasIndexedChunks = indexedChunkCount > 0
  const syncState = doc?.reindex_required
    ? 'stale'
    : (indexStatus?.status === 'indexed' && hasIndexedChunks ? 'synced' : 'missing')

  // Which environments actually hold this document's vectors, so the confirm
  // dialog names them instead of always claiming "DEV and PROD". DEV is written
  // at the `ingesting` stage; PROD only after a superadmin promotes it.
  const removalEnvironments = (() => {
    const stage = doc?.stage
    if (!stage) return []
    const inDev = ['ingesting', 'approval_for_prod', 'ingesting_prod', 'completed'].includes(stage)
    const inProd = ['ingesting_prod', 'completed'].includes(stage)
    const envs = []
    if (inDev) envs.push('Dev')
    if (inProd) envs.push('Production')
    return envs
  })()
  const removalEnvLabel = removalEnvironments.join(' and ')
  const removalDocLabel = getDocumentListLabel(doc) || workflowId

  async function confirmRemoveDocument() {
    try {
      setRemoving(true)
      setMessage('')
      await fetchJson(`/documents/${workflowId}?remove_from_search=true&purge=true`, { method: 'DELETE' })
      setConfirmRemoveOpen(false)
      setMessage('Document removed.')
      navigate('/documents')
    } catch (err) {
      setMessage(err.message)
      setConfirmRemoveOpen(false)
    } finally {
      setRemoving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex h-[calc(100svh-3.5rem)] flex-col gap-3 p-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-10 w-full" />
        <div className="flex min-h-0 flex-1 gap-3">
          <Skeleton className="hidden h-full w-[260px] lg:block" />
          <Skeleton className="h-full flex-1" />
        </div>
      </div>
    )
  }

  if (!doc) {
    return (
      <div className="p-6">
        <PanelNotice message={message || 'Document not found.'} />
      </div>
    )
  }

  const totalPages = sortedPages.length || doc.page_count || 1

  return (
    <div className="flex h-[calc(100svh-3.5rem)] min-h-0 w-full min-w-0 flex-col overflow-hidden">
      {/* Fixed header — does not scroll the whole app */}
      <div className="shrink-0 space-y-2 border-b border-border bg-card px-3 py-2 sm:px-4">
        <DocumentHeaderSummary
          className="min-w-0"
          doc={doc}
          reviewedPages={reviewedPages}
          reviewedChunks={reviewedChunks}
          pageCount={doc.page_count || pages.length}
          chunkCount={doc.chunk_count || chunks.length}
          onBack={() => navigate('/documents')}
          badges={
            <>
              {doc.failed && <Badge variant="destructive" className="text-[10px]">Failed</Badge>}
              {doc.processing && (
                <Badge variant="info" className="text-[10px]">
                  <Loader2 className="mr-0.5 h-2.5 w-2.5 animate-spin" />
                  Processing
                </Badge>
              )}
              {doc.reindex_required && (
                <div className="reindex-banner py-1 px-2 text-[10px]">
                  <RefreshCw className="h-3 w-3 text-warning" />
                  <span>Re-ingest required</span>
                </div>
              )}
              {doc.prod_ready_requested_at && (
                <Badge variant="info" className="text-[10px]">
                  Prod ready requested{doc.prod_ready_requested_by_username ? ` by ${doc.prod_ready_requested_by_username}` : ''}
                </Badge>
              )}
            </>
          }
        />

        {isViewOnly ? (
          <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            <strong className="text-foreground">View only</strong>
            {' — '}
            You can browse this document but cannot upload, edit, approve, delete, or run pipeline actions.
          </div>
        ) : null}

        <div className="flex min-w-0 flex-col gap-2 xl:flex-row xl:items-center xl:gap-3">
          <PipelineStepper
            className="min-w-0 flex-1"
            currentStage={doc.stage}
            hasPages={pages.length > 0 || Boolean(doc.page_count)}
            hasChunks={chunks.length > 0 || Boolean(doc.chunk_count)}
          />
          <div className="flex shrink-0 flex-wrap items-center gap-1.5 xl:justify-end">
            {visibleActions.slice(0, 4).map(action => {
              const blockedByClassification = action === 'approve_ingestion' && ingestBlockedByClassification
              return (
                <Button
                  key={action}
                  size="sm"
                  variant={action.includes('approve') ? 'success' : action.includes('reindex') ? 'warning' : 'outline'}
                  className="h-8 text-xs"
                  disabled={Boolean(actionPending) || blockedByClassification}
                  title={blockedByClassification ? 'Set document type below before approving for dev' : undefined}
                  onClick={() => runAction(action)}
                >
                  {actionPending === action
                    ? 'Working…'
                    : blockedByClassification
                      ? 'Set document type first'
                      : summarizeAvailableAction(action)}
                </Button>
              )
            })}
            {canRemoveDocument && (
              <Button
                size="sm"
                variant="outline"
                className="h-8 text-xs text-destructive border-destructive/30 hover:bg-destructive/10 hover:text-destructive"
                disabled={Boolean(actionPending)}
                onClick={() => runAction('disable_document')}
              >
                <Trash2 className="mr-1 h-3.5 w-3.5" />
                {actionPending === 'disable_document' ? 'Removing…' : 'Remove'}
              </Button>
            )}
          </div>
        </div>

        {doc.stage === 'ready_for_ingestion' && (
          <DocumentClassificationPanel
            doc={doc}
            workflowId={workflowId}
            canClassify={canReview}
            onSaved={reloadAfterMutation}
          />
        )}

        {message ? (
          <PanelNotice tone={message.toLowerCase().includes('fail') || message.toLowerCase().includes('error') ? 'error' : 'warning'} message={message} />
        ) : null}
      </div>

      {/* Body fills remaining viewport; only panels scroll */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left: preview column fills height; PDF area scrolls if tall */}
        <aside className="hidden min-h-0 w-[min(30vw,300px)] min-w-[220px] max-w-[300px] shrink-0 flex-col border-r border-border bg-muted/20 lg:flex">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-surface-warm px-2.5 py-2">
            <span className="text-xs font-medium text-foreground">Source Preview</span>
            {currentPageRecord && (
              <Badge variant={currentPageRecord.is_reviewed ? 'success' : 'secondary'} className="text-[10px]">
                {currentPageRecord.is_reviewed ? 'reviewed' : 'pending'}
              </Badge>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-hidden">
            <SourcePdfPreview workflowId={workflowId} currentPage={currentPage} />
          </div>
        </aside>

        {/* Right: tabs + scrollable panel content only */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          <Tabs value={activeTab} onValueChange={setActiveTab} className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <div className="shrink-0 border-b border-border bg-card px-2 sm:px-4">
              <TabsList className="h-10 w-full justify-start gap-0.5 overflow-x-auto rounded-none bg-transparent p-0 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                <TabsTrigger
                  value="ocr"
                  className="h-10 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Eye className="mr-1.5 h-3.5 w-3.5" />OCR
                </TabsTrigger>
                <TabsTrigger
                  value="translation"
                  className="h-10 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Layers className="mr-1.5 h-3.5 w-3.5" />Translation
                </TabsTrigger>
                <TabsTrigger
                  value="chunks"
                  className="h-10 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <FileCode className="mr-1.5 h-3.5 w-3.5" />
                  Content
                </TabsTrigger>
                <TabsTrigger
                  value="index"
                  className="h-10 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Database className="mr-1.5 h-3.5 w-3.5" />Index
                </TabsTrigger>
                <TabsTrigger
                  value="debug"
                  className="h-10 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Bug className="mr-1.5 h-3.5 w-3.5" />Debug
                </TabsTrigger>
                <TabsTrigger
                  value="audit"
                  className="h-10 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <ClipboardList className="mr-1.5 h-3.5 w-3.5" />Audit
                </TabsTrigger>
              </TabsList>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
              {/* OCR Review */}
              <TabsContent value="ocr" className="m-0 mt-0 hidden min-h-full flex-col data-[state=active]:flex">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-card/60 px-3 py-2 sm:px-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-foreground">
                      Review text{sortedPages.length ? ` · Page ${currentPage}` : ''}
                    </span>
                    {currentPageRecord && (
                      <Badge variant={currentPageRecord.is_reviewed ? 'success' : 'secondary'}>
                        {currentPageRecord.is_reviewed ? 'reviewed' : 'pending'}
                      </Badge>
                    )}
                    {sortedPages.length > 0 && (
                      <span className="text-xs text-muted-foreground">
                        {reviewedPages}/{sortedPages.length} reviewed
                      </span>
                    )}
                  </div>
                  {currentPageRecord && canEdit && (
                    <div className="flex items-center gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-8"
                        onClick={() => {
                          const next = { ...pageEdits }
                          delete next[currentPage]
                          setPageEdits(next)
                        }}
                      >
                        <RotateCcw className="mr-1 h-3.5 w-3.5" />Reset
                      </Button>
                      <Button size="sm" className="h-8" disabled={!canReview} onClick={() => savePage(currentPage, pageText)}>
                        <Save className="mr-1 h-3.5 w-3.5" />Save
                      </Button>
                    </div>
                  )}
                </div>

                {(pageEdits[currentPage] !== undefined || doc.error_message || ocrAlreadyPast) && (
                  <div className="space-y-2 border-b border-border px-3 py-2 sm:px-4">
                    {pageEdits[currentPage] !== undefined && (
                      <div className="reindex-banner text-xs">
                        <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                        Editing this page may require re-preparing content and re-ingest
                      </div>
                    )}
                    {doc.error_message && (
                      <PanelNotice title="Document Error" message={doc.error_message} />
                    )}
                    {ocrAlreadyPast && (
                      <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
                        <p className="font-medium text-foreground">Text extraction already completed</p>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          This document is in <strong>{getStageLabel(doc.stage)}</strong>.
                          {doc.stage === 'translation_review'
                            ? ' Use Approve translation on the Translation tab (not Approve text).'
                            : doc.stage === 'chunk_review'
                              ? ' Use Approve content on the Content tab.'
                              : ' Approve text is only available during the Review text stage.'}
                        </p>
                      </div>
                    )}
                  </div>
                )}

                <div className="flex min-h-0 flex-1 flex-col px-3 py-3 sm:px-4">
                  {currentPageRecord ? (
                    <div className="grid flex-1 grid-cols-1 gap-3 lg:grid-cols-2">
                      <div className="flex min-h-0 min-w-0 flex-col">
                        <div className="mb-1.5 flex items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Original text
                          </label>
                          <span className="text-[10px] text-muted-foreground">read-only</span>
                        </div>
                        <div className="min-h-[12rem] flex-1 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                          {currentPageOcrText || '(No text yet)'}
                        </div>
                      </div>
                      <div className="flex min-h-0 min-w-0 flex-col">
                        <div className="mb-1.5 flex items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            {canEdit ? 'Edited text' : 'Current text'}
                          </label>
                          <span className="text-[10px] text-muted-foreground">
                            {canEdit ? 'editable' : 'read-only'}
                          </span>
                        </div>
                        <div className={`flex min-h-[12rem] flex-1 flex-col overflow-hidden rounded-md border border-input bg-background ${canEdit ? 'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2' : 'bg-muted/20'}`}>
                          <Textarea
                            value={pageText}
                            readOnly={!canEdit}
                            onChange={e => {
                              if (!canEdit) return
                              setPageEdits({ ...pageEdits, [currentPage]: e.target.value })
                            }}
                            className="min-h-[12rem] flex-1 resize-none border-0 bg-transparent font-mono text-sm leading-relaxed shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
                          />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="flex flex-1 items-start rounded-lg border border-border bg-muted/20 px-3 py-4">
                      {isOcrPending ? (
                        <div className="flex items-start gap-2.5">
                          <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-primary" />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-foreground">
                              {doc.stage === 'ocr_processing' ? 'Extracting text…' : 'Waiting to extract text'}
                            </p>
                            <p className="mt-0.5 text-xs text-muted-foreground">
                              {getStageLabel(doc.stage)}
                              {jobs[0]?.started_at ? ` · Started ${formatCompactDateTime(jobs[0].started_at)}` : ''}
                            </p>
                            <p className="mt-1.5 text-xs text-muted-foreground">
                              Page text will show here when extraction finishes.
                            </p>
                          </div>
                        </div>
                      ) : (
                        <EmptyPanel
                          compact
                          icon={FileText}
                          title="No page data yet"
                          subtitle="Text will appear here after extraction finishes."
                        />
                      )}
                    </div>
                  )}
                </div>

                {sortedPages.length > 0 && (
                  <div className="border-t border-border bg-card px-2 py-2">
                    <PagePager
                      pages={sortedPages}
                      currentPage={currentPage}
                      onChange={setCurrentPage}
                      getStatus={(p) => (p.is_reviewed ? 'done' : 'pending')}
                      label="Pages"
                    />
                  </div>
                )}
              </TabsContent>

              {/* Translation Review */}
              <TabsContent value="translation" className="m-0 mt-0 hidden flex-col data-[state=active]:flex">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-card/60 px-3 py-2 sm:px-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-foreground">
                      Translation{sortedPages.length ? ` · Page ${currentPage}` : ''}
                    </span>
                    {sortedPages.length > 0 && (
                      <span className="text-xs text-muted-foreground">
                        {translatedPages} of {sortedPages.length} translated
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {canPipeline && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-8"
                      disabled={!canPipeline || Boolean(actionPending)}
                      onClick={() => runAction('retry_translation')}
                    >
                      <RefreshCw className="mr-1 h-3.5 w-3.5" />
                      {actionPending === 'retry_translation' ? 'Retrying…' : 'Retry Translation'}
                    </Button>
                    )}
                    {canEdit && (
                      <Button
                        size="sm"
                        variant="success"
                        className="h-8"
                        disabled={!canApproveTranslation || Boolean(actionPending)}
                        title={!canApproveTranslation ? `Available only in translation_review (current: ${doc.stage})` : undefined}
                        onClick={() => runAction('approve_translation')}
                      >
                        <CheckCircle className="mr-1 h-3.5 w-3.5" />
                        {actionPending === 'approve_translation' ? 'Approving…' : 'Approve Translation'}
                      </Button>
                    )}
                  </div>
                </div>

                {(currentPageLanguage || currentPageRecord?.translation_provider || translationEdits[currentPage] !== undefined) && (
                  <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2">
                    <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                      {currentPageLanguage && (
                        <span>Language: <strong className="text-foreground">{String(currentPageLanguage).toUpperCase()}</strong></span>
                      )}
                      {currentPageRecord?.translation_provider && (
                        <>
                          <span>·</span>
                          <span>Provider: {currentPageRecord.translation_provider}</span>
                          <span>·</span>
                          <span>Model: {currentPageRecord.translation_model}</span>
                        </>
                      )}
                    </div>
                    {canEdit && (
                      <div className="flex items-center gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-8"
                          onClick={() => {
                            const next = { ...translationEdits }
                            delete next[currentPage]
                            setTranslationEdits(next)
                          }}
                        >
                          <RotateCcw className="mr-1 h-3.5 w-3.5" />Reset
                        </Button>
                        <Button size="sm" className="h-8" disabled={!canReview} onClick={() => saveTranslation(currentPage, translationText)}>
                          <Save className="mr-1 h-3.5 w-3.5" />Save
                        </Button>
                      </div>
                    )}
                  </div>
                )}

                {canEdit && translationEdits[currentPage] !== undefined && (
                  <div className="shrink-0 border-b border-border px-4 py-2">
                    <div className="reindex-banner text-xs">
                      <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                      Changes to translations will require re-preparing content and re-ingest downstream
                    </div>
                  </div>
                )}

                <div className="px-3 py-3 sm:px-4">
                  {currentPageRecord && (currentPageRecord.translated_markdown || currentPageRecord.edited_translation) ? (
                    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                      <div className="flex min-w-0 flex-col">
                        <div className="mb-1.5 flex items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Original text
                          </label>
                          <span className="text-[10px] text-muted-foreground">
                            {currentPageRecord?.edited_markdown ? 'reviewed · read-only' : 'read-only'}
                          </span>
                        </div>
                        <div className="min-h-[12rem] rounded-md border border-border bg-muted/30 p-3 font-mono text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                          {currentPageTranslationSourceText || '(No text yet)'}
                        </div>
                      </div>
                      <div className="flex min-w-0 flex-col">
                        <div className="mb-1.5 flex items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Translated text
                          </label>
                          <span className="text-[10px] text-muted-foreground">
                            {canEdit ? 'editable' : 'read-only'}
                          </span>
                        </div>
                        <div className={`rounded-md border border-input bg-background ${canEdit ? 'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2' : 'bg-muted/20'}`}>
                          <Textarea
                            value={translationText}
                            readOnly={!canEdit}
                            onChange={e => {
                              if (!canEdit) return
                              setTranslationEdits({ ...translationEdits, [currentPage]: e.target.value })
                            }}
                            className="min-h-[12rem] resize-y border-0 bg-transparent font-mono text-sm leading-relaxed shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
                          />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="rounded-lg border border-border bg-muted/20">
                      <EmptyPanel
                        compact
                        icon={Layers}
                        title={sortedPages.length ? `No translation for page ${currentPage}` : 'No translation yet'}
                        subtitle={translationEmptySubtitle}
                      />
                    </div>
                  )}
                </div>

                {sortedPages.length > 0 && (
                  <div className="border-t border-border bg-card px-2 py-2">
                    <PagePager
                      pages={sortedPages}
                      currentPage={currentPage}
                      onChange={setCurrentPage}
                      getStatus={(p) => (
                        (p.translation_reviewed || p.translated_markdown || p.edited_translation)
                          ? 'accent'
                          : 'pending'
                      )}
                      label="Pages"
                    />
                  </div>
                )}
              </TabsContent>

              {/* Chunks Review */}
              <TabsContent value="chunks" className="m-0 mt-0 data-[state=inactive]:hidden">
                <div className="space-y-3 px-3 py-3 sm:px-4">
                  {doc.stage === 'chunking' && chunkingProgress && (
                    <div className="panel p-3 space-y-2">
                      <div className="flex items-center justify-between text-xs">
                        <span className="font-medium text-foreground">Preparing content…</span>
                        <span className="text-muted-foreground">
                          {chunkingProgress.pages_processed || 0}/{chunkingProgress.pages_total || 0} pages · {chunkingProgress.chunks_emitted || 0} sections
                        </span>
                      </div>
                      <div className="h-2 rounded-full bg-muted overflow-hidden">
                        <div
                          className="h-full bg-primary transition-all duration-500 ease-out"
                          style={{ width: `${chunkingPercent}%` }}
                        />
                      </div>
                      <p className="text-[11px] text-muted-foreground">{chunkingPercent.toFixed(0)}%</p>
                    </div>
                  )}

                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <span className="text-sm font-medium text-foreground">{chunks.length} chunks</span>
                      <span className="text-xs text-muted-foreground">
                        {reviewedChunks} reviewed · {chunks.filter(c => c.reindex_dirty).length} dirty
                      </span>
                    </div>
                    {canEdit && (
                      <Button
                        size="sm"
                        variant="success"
                        disabled={!canApproveChunks || Boolean(actionPending)}
                        title={!canApproveChunks ? `Available only in chunk_review (current: ${doc.stage})` : undefined}
                        onClick={() => runAction('approve_chunks')}
                      >
                        <CheckCircle className="h-3.5 w-3.5 mr-1" />
                        {actionPending === 'approve_chunks' ? 'Approving…' : 'Approve content'}
                      </Button>
                    )}
                  </div>

                  {chunks.filter(c => c.reindex_dirty).length > 0 && (
                    <div className="reindex-banner text-xs">
                      <RefreshCw className="h-3.5 w-3.5 text-warning shrink-0" />
                      <span>{chunks.filter(c => c.reindex_dirty).length} section(s) have been edited — re-ingest required to sync search</span>
                    </div>
                  )}

                  {chunks.length > 0 ? (
                    <div className="space-y-2">
                      {chunks.map(chunk => (
                        <div
                          key={chunk.chunk_number}
                          id={`chunk-card-${chunk.chunk_number}`}
                          className={`panel scroll-mt-4 transition-shadow ${
                            chunk.reindex_dirty ? 'border-warning/40' : ''
                          } ${
                            highlightedChunk === chunk.chunk_number
                              ? 'ring-2 ring-primary/70 shadow-md bg-primary/5'
                              : ''
                          }`}
                        >
                          <div className="px-4 py-2.5 border-b border-border bg-surface-warm space-y-2">
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-3 flex-wrap">
                                <span className="text-xs font-medium">Chunk {chunk.chunk_number}</span>
                                <span className="text-xs text-muted-foreground">
                                  Pages {chunk.page_start}–{chunk.page_end}
                                </span>
                                {chunk.is_reviewed && <Badge variant="success" className="text-[10px]">Reviewed</Badge>}
                                {chunk.reindex_dirty && <Badge variant="warning" className="text-[10px]">Dirty</Badge>}
                                {chunk.excluded && <Badge variant="destructive" className="text-[10px]">Excluded</Badge>}
                              </div>
                              <div className="flex items-center gap-1.5">
                              {canEdit ? (
                                <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground cursor-pointer">
                                  <Checkbox checked={!chunk.excluded} />
                                  Include
                                </label>
                              ) : chunk.excluded ? (
                                <span className="text-[10px] text-muted-foreground">Excluded</span>
                              ) : null}
                              <Button variant="ghost" size="sm" className="h-6 text-[10px]"
                                onClick={() => setCurrentPage(chunk.page_start)}
                              >
                                Jump to source
                              </Button>
                              {canEdit && (
                                <Button variant="ghost" size="sm" className="h-6 text-[10px]"
                                  onClick={() => {
                                    const next = { ...chunkEdits }
                                    delete next[chunk.chunk_number]
                                    setChunkEdits(next)
                                  }}
                                >
                                  <RotateCcw className="h-3 w-3" />
                                </Button>
                              )}
                            </div>
                            </div>
                          </div>
                          <div className="p-3">
                            <Textarea
                              value={chunkEdits[chunk.chunk_number] ?? chunk.edited_text ?? chunk.text ?? chunk.original_text ?? ''}
                              readOnly={!canEdit}
                              onChange={e => {
                                if (!canEdit) return
                                setChunkEdits({ ...chunkEdits, [chunk.chunk_number]: e.target.value })
                              }}
                              className={`text-xs font-mono min-h-[60px] resize-y ${!canEdit ? 'bg-muted/20' : ''}`}
                            />
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <EmptyPanel
                      icon={FileCode}
                      title={getChunkEmptyMessage(doc)}
                      subtitle={
                        doc.stage === 'chunking' && chunkingProgress
                          ? `Chunking ${chunkingPercent.toFixed(0)}% · ${chunkingProgress.pages_processed || 0}/${chunkingProgress.pages_total || 0} pages · ${chunkingProgress.chunks_emitted || 0} chunks`
                          : 'Content sections will appear after preparation finishes'
                      }
                    />
                  )}
                </div>
              </TabsContent>

              {/* Index State */}
              <TabsContent value="index" className="m-0 mt-0 data-[state=inactive]:hidden">
                <div className="space-y-4 p-4">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-foreground">Ingestion & Index State</span>
                    {canPipeline && (
                      <div className="flex gap-2">
                        <Button size="sm" variant="outline" disabled={!canPipeline || Boolean(actionPending)} onClick={() => runAction('reingest_document')}>
                          <RefreshCw className="h-3.5 w-3.5 mr-1" />
                          {actionPending === 'reingest_document' ? 'Reingesting…' : 'Reingest'}
                        </Button>
                      </div>
                    )}
                  </div>
                  {panelLoading.index && (
                    <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      Loading Qdrant index status…
                    </div>
                  )}
                  {panelErrors.index && !panelLoading.index && (
                    <PanelNotice title="Index status unavailable" message={panelErrors.index} />
                  )}

                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <div className="stat-card">
                      <p className="text-xs text-muted-foreground uppercase tracking-wider">Indexed Chunks</p>
                      <p className="text-xl font-semibold font-serif mt-1">{indexedChunkCount}</p>
                    </div>
                    <div className="stat-card">
                      <p className="text-xs text-muted-foreground uppercase tracking-wider">Index</p>
                      <p className="text-sm font-mono mt-1">{indexStatus?.index_name || doc.index_status?.[0]?.index_name || 'primary-docs'}</p>
                    </div>
                    <div className={`stat-card ${doc.reindex_required ? 'border-warning/40 bg-warning/5' : ''}`}>
                      <p className="text-xs text-muted-foreground uppercase tracking-wider">Sync Status</p>
                      <p className="text-sm font-medium mt-1">
                        {syncState === 'stale' ? (
                          <span className="text-warning flex items-center gap-1">
                            <AlertTriangle className="h-3.5 w-3.5" />Stale
                          </span>
                        ) : syncState === 'synced' ? (
                          <span className="text-success flex items-center gap-1">
                            <CheckCircle className="h-3.5 w-3.5" />Synced
                          </span>
                        ) : (
                          <span className="text-muted-foreground flex items-center gap-1">
                            <AlertCircle className="h-3.5 w-3.5" />Missing
                          </span>
                        )}
                      </p>
                    </div>
                  </div>

                  {doc.reindex_required && (
                    <div className="reindex-banner">
                      <RefreshCw className="h-4 w-4 text-warning shrink-0" />
                      <div className="text-sm">
                        <p className="font-medium text-foreground">Document edits have made search data stale</p>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          Re-ingest is required to sync edited content with search
                        </p>
                      </div>
                      {canPipeline && (
                        <Button size="sm" variant="warning" className="ml-auto shrink-0" disabled={!canPipeline || Boolean(actionPending)} onClick={() => runAction('reingest_document')}>
                          {actionPending === 'reingest_document' ? 'Re-ingesting…' : 'Re-ingest now'}
                        </Button>
                      )}
                    </div>
                  )}

                  {hasIndexedChunks ? (
                    <div className="panel">
                      <div className="panel-header">
                        <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                          Indexed Chunks ({indexedChunkCount})
                        </span>
                      </div>
                      <div className="divide-y divide-border">
                        {indexChunks.slice(0, 6).map((chunk, i) => (
                          <div key={chunk._id || chunk.chunk_number || i} className="px-4 py-2.5 flex items-center gap-3 text-sm">
                            <span className="text-xs font-mono text-muted-foreground">#{chunk.chunk_num || chunk.chunk_number}</span>
                            <span className="text-xs truncate flex-1">{String(chunk.text ?? chunk.original_text ?? '').slice(0, 80)}...</span>
                            <Badge variant="success" className="text-[10px] shrink-0">Synced</Badge>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <EmptyPanel
                      icon={Database}
                      title="No indexed data"
                      subtitle="Content will appear here after the document completes ingestion"
                    />
                  )}
                </div>
              </TabsContent>

              {/* Debug / Runtime */}
              <TabsContent value="debug" className="m-0 mt-0 data-[state=inactive]:hidden">
                <div className="space-y-4 p-4">
                  <span className="text-sm font-medium text-foreground">Runtime & Debug</span>
                  {panelLoading.runtime && (
                    <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      Loading Temporal runtime…
                    </div>
                  )}
                  {(panelErrors.runtime || panelErrors.stageIo) && !panelLoading.runtime && (
                    <PanelNotice
                      title="Debug data unavailable"
                      message={panelErrors.runtime || panelErrors.stageIo}
                    />
                  )}

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div className="panel p-4 space-y-3">
                      <h3 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Document State</h3>
                      <div className="space-y-2 text-sm">
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">SQLite Stage</span>
                          <StageBadge stage={runtime?.sqlite_stage || doc.stage} compact />
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Temporal Stage</span>
                          <StageBadge stage={runtime?.temporal?.current_stage || doc.stage} compact />
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Run ID</span>
                          <span className="font-mono text-xs">{runtime?.temporal?.run_id || doc.current_job_id || 'none'}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Failed</span>
                          <span className="text-xs">{doc.failed ? 'Yes' : 'No'}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Re-ingest required</span>
                          <span className={`text-xs ${doc.reindex_required ? 'text-warning font-medium' : ''}`}>
                            {doc.reindex_required ? 'Yes' : 'No'}
                          </span>
                        </div>
                        {doc.error_message && (
                          <PanelNotice title="Document Error" message={doc.error_message} />
                        )}
                      </div>
                    </div>

                    <div className="panel p-4 space-y-3">
                      <h3 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Stage I/O Summary</h3>
                      {panelErrors.stageIo ? (
                        <PanelNotice title="Stage I/O Unavailable" message={panelErrors.stageIo} />
                      ) : (stageIo?.stages || []).length ? (
                        <div className="divide-y divide-border">
                          {(stageIo.stages || []).map(stage => (
                            <div key={stage.stage} className="py-2 flex items-center gap-3 text-xs">
                              <StageBadge stage={stage.stage} compact />
                              <span className="text-muted-foreground flex-1">
                                {stage.input_artifacts?.length || 0} inputs · {stage.output_artifacts?.length || 0} outputs
                              </span>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p className="text-xs text-muted-foreground">No stage I/O records available.</p>
                      )}
                    </div>
                  </div>

                  <div className="panel">
                    <div className="panel-header">
                      <h3 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Job History</h3>
                    </div>
                    {panelErrors.jobs ? (
                      <div className="p-4">
                        <PanelNotice title="Job History Unavailable" message={panelErrors.jobs} />
                      </div>
                    ) : jobs.length > 0 ? (
                      <div className="divide-y divide-border">
                        {jobs.map(run => (
                          <div key={run.id} className="px-4 py-2.5 flex items-center gap-3 text-sm">
                            <span className="font-mono text-xs text-muted-foreground">{run.id}</span>
                            <span className="capitalize">{run.job_type}</span>
                            <Badge variant={run.status === 'running' ? 'info' : run.status === 'completed' ? 'success' : 'destructive'} className="text-[10px]">
                              {run.status}
                            </Badge>
                            {run.error_message && (
                              <span className="text-xs text-destructive truncate max-w-[200px]" title={run.error_message}>
                                {run.error_message}
                              </span>
                            )}
                            <span className="ml-auto text-xs text-muted-foreground">
                              {formatCompactDateTime(run.started_at)}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <EmptyPanel icon={Play} title="No jobs recorded" />
                    )}
                  </div>
                </div>
              </TabsContent>

              {/* Audit */}
              <TabsContent value="audit" className="m-0 mt-0 data-[state=inactive]:hidden">
                <div className="space-y-3 p-4">
                  {panelLoading.audit && (
                    <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      Loading audit log…
                    </div>
                  )}
                  {panelErrors.audit && !panelLoading.audit && (
                    <PanelNotice title="Audit log unavailable" message={panelErrors.audit} />
                  )}
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-foreground">Document Audit Log</span>
                    {auditLogs.length > 0 && (
                      <select
                        className="rounded-md border border-input bg-background px-3 py-1 text-xs"
                        value={auditFilter}
                        onChange={e => setAuditFilter(e.target.value)}
                      >
                        <option value="all">All ({auditLogs.length})</option>
                        {auditOptions.map(option => (
                          <option key={option.value} value={option.value}>{option.label} ({option.count})</option>
                        ))}
                      </select>
                    )}
                  </div>
                  <div className="panel divide-y divide-border">
                    {filteredAudit.length > 0 ? filteredAudit.map(entry => (
                      <div key={entry.id} className="px-4 py-2.5">
                        <div className="flex items-center gap-3 text-sm">
                          <Badge variant="secondary" className="text-[10px] capitalize whitespace-nowrap">
                            {summarizeAuditAction(entry.action_type)}
                          </Badge>
                          <span
                            className="text-xs text-muted-foreground truncate max-w-[280px]"
                            title={
                              [entry.actor_email || entry.actor_username, entry.actor_roles]
                                .filter(Boolean)
                                .join(' · ') || entry.actor || 'system'
                            }
                          >
                            {entry.actor
                              || entry.actor_email
                              || entry.actor_username
                              || 'system'}
                          </span>
                          <span className="ml-auto text-xs text-muted-foreground">
                            {formatCompactDateTime(entry.timestamp)}
                          </span>
                          <button
                            className="text-muted-foreground hover:text-foreground transition-colors"
                            onClick={() => {
                              const next = new Set(auditExpanded)
                              if (next.has(entry.id)) next.delete(entry.id)
                              else next.add(entry.id)
                              setAuditExpanded(next)
                            }}
                          >
                            {auditExpanded.has(entry.id) ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                          </button>
                        </div>
                        {auditExpanded.has(entry.id) && (
                          <div className="mt-2 space-y-2">
                            {entry.metadata && (
                              <div className="p-2 rounded-md bg-muted/50 text-xs font-mono whitespace-pre-wrap text-muted-foreground">
                                {typeof entry.metadata === 'string' ? entry.metadata : JSON.stringify(entry.metadata, null, 2)}
                              </div>
                            )}
                            {(entry.old_value || entry.new_value) && (
                              <div className="grid grid-cols-2 gap-2">
                                <div className="p-2 rounded-md bg-destructive/5 border border-destructive/10">
                                  <span className="text-[10px] font-medium text-destructive uppercase block mb-1">Before</span>
                                  <pre className="text-[10px] font-mono text-muted-foreground whitespace-pre-wrap">
                                    {entry.old_value || '(empty)'}
                                  </pre>
                                </div>
                                <div className="p-2 rounded-md bg-success/5 border border-success/10">
                                  <span className="text-[10px] font-medium text-success uppercase block mb-1">After</span>
                                  <pre className="text-[10px] font-mono text-muted-foreground whitespace-pre-wrap">
                                    {entry.new_value || '(empty)'}
                                  </pre>
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )) : (
                      <EmptyPanel icon={ClipboardList} title="No audit entries" subtitle="Actions on this document will be recorded here" />
                    )}
                  </div>
                </div>
              </TabsContent>
            </div>
          </Tabs>
        </div>
      </div>

      <AlertDialog open={confirmRemoveOpen} onOpenChange={open => !removing && setConfirmRemoveOpen(open)}>
        <AlertDialogContent className="max-w-lg">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {removalEnvironments.length > 0
                ? `Remove this document from ${removalEnvLabel}?`
                : 'Remove this document?'}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-3">
                <p>
                  Are you sure you want to remove{' '}
                  <strong className="text-foreground">{removalDocLabel}</strong>
                  {removalEnvironments.length > 0 ? (
                    <> from <strong className="text-foreground">{removalEnvLabel}</strong>?</>
                  ) : (
                    <>?</>
                  )}
                </p>

                <div className="rounded-md border border-border bg-muted/30 p-3">
                  <p className="text-[10px] uppercase tracking-wider text-muted-foreground">What happens</p>
                  <ul className="mt-1.5 space-y-1 text-xs">
                    <li>The uploaded file and everything read from it is deleted</li>
                    <li>All reviewed pages and content are deleted</li>
                    {removalEnvironments.length > 0 && (
                      <li>It stops appearing in {removalEnvLabel} search results</li>
                    )}
                    <li>Any processing still running is stopped</li>
                  </ul>
                </div>

                {removalEnvironments.includes('Production') && (
                  <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                    This document is live in Production. Farmers will stop seeing it in
                    search straight away.
                  </p>
                )}

                <p className="text-xs">
                  This can’t be undone. To bring the document back you would need to
                  upload it and review it again from the start.
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removing}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={event => { event.preventDefault(); confirmRemoveDocument() }}
              disabled={removing}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {removing ? 'Removing…' : 'Yes, remove it'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
