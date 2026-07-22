import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
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
  Tag,
  Trash2,
} from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'
import PipelineStepper from '../components/PipelineStepper'
import ChunkTagEditor from '../components/ChunkTagEditor'
import DomainTagBadges from '../components/DomainTagBadges'
import PagePager from '../components/PagePager'
import SourcePdfPreview from '../components/SourcePdfPreview'
import { StageBadge } from '../components/StageBadge'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Checkbox } from '../components/ui/checkbox'
import { Skeleton } from '../components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { Textarea } from '../components/ui/textarea'
import {
  fetchJson,
  formatCompactDateTime,
  getAuditActionOptions,
  getDocumentFileLabel,
  getDocumentListLabel,
  getDocumentMetaLabel,
  getStageLabel,
  collectDocumentTagLabels,
  summarizeAuditAction,
  summarizeAvailableAction,
} from '../lib/pipelineUi'

function getChunkEmptyMessage(doc) {
  const stage = doc?.stage
  if (stage === 'registered' || stage === 'ocr_processing') return 'Chunks are not available yet. The document is still moving through OCR.'
  if (stage === 'ocr_review') return 'Approve OCR before chunking can begin.'
  if (stage === 'translation_processing') return 'Translation is still running.'
  if (stage === 'translation_review') return 'Approve translation to continue into chunking.'
  if (stage === 'chunking') return 'Chunking is currently running for this document.'
  if (stage === 'failed') return 'Chunk data is blocked because the workflow failed.'
  return 'No chunk data is currently available for this document.'
}

function EmptyPanel({ icon: Icon, title, subtitle }) {
  return (
    <div className="p-12 text-center">
      <Icon className="h-8 w-8 mx-auto mb-3 text-muted-foreground/30" />
      <p className="text-sm font-medium text-foreground">{title}</p>
      {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
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

// Which permission each mutating action requires. Approvals / edits are
// review; anything that re-runs pipeline stages or touches the index is pipeline.
const ACTION_PERMISSION = {
  approve_ocr: 'review',
  approve_translation: 'review',
  approve_chunks: 'review',
  retry_translation: 'pipeline',
  reingest_document: 'pipeline',
  mark_reindex_required: 'pipeline',
  clear_reindex_required: 'pipeline',
  disable_document: 'admin',
  restore_document: 'admin',
}

export default function DocumentOpsView() {
  const { workflowId } = useParams()
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const canReview = hasPermission('review')
  const canPipeline = hasPermission('pipeline')
  const canAdmin = hasPermission('admin')
  const canRunAction = (action) => {
    const needed = ACTION_PERMISSION[action]
    return needed ? hasPermission(needed) : canReview
  }
  const [searchParams] = useSearchParams()
  const [doc, setDoc] = useState(null)
  const [pages, setPages] = useState([])
  const [chunks, setChunks] = useState([])
  const [indexChunks, setIndexChunks] = useState([])
  const [indexStatus, setIndexStatus] = useState(null)
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
  const [autoTaggingDoc, setAutoTaggingDoc] = useState(false)
  const [translationEdits, setTranslationEdits] = useState({})
  const [auditFilter, setAuditFilter] = useState('all')
  const [auditExpanded, setAuditExpanded] = useState(new Set())
  const [auditLogs, setAuditLogs] = useState([])
  const [highlightedChunk, setHighlightedChunk] = useState(null)

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
    } else if (stage === 'ingesting' || stage === 'completed') {
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

  useEffect(() => {
    load()
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [workflowId])

  async function load() {
    try {
      const docData = await fetchJson(`/documents/${workflowId}`)
      setDoc(docData)

      const results = await Promise.allSettled([
        fetchJson(`/documents/${workflowId}/pages`),
        fetchJson(`/documents/${workflowId}/chunks?include_excluded=true`),
        fetchJson(`/documents/${workflowId}/qdrant`),
        fetchJson(`/documents/${workflowId}/jobs`),
        fetchJson(`/documents/${workflowId}/runtime`),
        fetchJson(`/documents/${workflowId}/stage-io`),
        fetchJson(`/documents/${workflowId}/audit?limit=100`),
      ])

      const nextErrors = {}
      const assignResult = (result, setter, key, fallback, normalize) => {
        if (result.status === 'fulfilled') {
          setter(normalize ? normalize(result.value) : result.value)
          return
        }
        setter(fallback)
        nextErrors[key] = result.reason?.message || 'Unable to load this panel.'
      }

      assignResult(results[0], setPages, 'pages', [], value => Array.isArray(value) ? value : [])
      assignResult(results[1], setChunks, 'chunks', [], value => Array.isArray(value) ? value : [])
      if (results[2].status === 'fulfilled') {
        const status = results[2].value || {}
        setIndexStatus(status)
        setIndexChunks(Array.isArray(status.hits) ? status.hits : [])
      } else {
        setIndexStatus(null)
        setIndexChunks([])
        nextErrors.index = results[2].reason?.message || 'Unable to load this panel.'
      }
      assignResult(results[3], setJobs, 'jobs', [], value => Array.isArray(value) ? value : [])
      assignResult(results[4], setRuntime, 'runtime', null)
      assignResult(results[5], setStageIo, 'stageIo', null)
      if (results[6].status === 'fulfilled') setAuditLogs(results[6].value?.logs || [])
      else setAuditLogs([])
      setPanelErrors(nextErrors)
      setMessage('')
    } catch (error) {
      setDoc(null)
      setPanelErrors({})
      setMessage(error.message)
    } finally {
      setLoading(false)
    }
  }

  async function runAction(action) {
    setMessage('')
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
        const label = getDocumentListLabel(doc) || workflowId
        if (!window.confirm(`Remove document "${label}" from the console?\n\nThis soft-deletes the document (hides it from lists) and removes it from search. You can restore it later with admin tools.`)) {
          return
        }
        await fetchJson(`/documents/${workflowId}?remove_from_search=true`, { method: 'DELETE' })
        setMessage('Document removed.')
        navigate('/documents')
        return
      } else if (action === 'restore_document') {
        await fetchJson(`/documents/${workflowId}/restore`, { method: 'POST' })
      } else {
        await fetchJson(`/documents/${workflowId}/${action.replace(/_/g, '-')}`, { method: 'POST' })
      }
      setMessage(`${summarizeAvailableAction(action)} triggered.`)
      load()
    } catch (error) {
      setMessage(error.message)
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
      load()
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
      load()
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
      load()
    } catch (err) {
      setMessage(err.message)
    }
  }

  const visibleActions = (doc?.available_actions || []).filter(
    action => !['disable_document', 'restore_document', 'inspect_runtime', 'reconcile_document'].includes(action)
      && canRunAction(action)
  )
  const canRemoveDocument = canAdmin && (doc?.available_actions || []).includes('disable_document')
  const sortedPages = useMemo(() => [...pages].sort((a, b) => a.page_number - b.page_number), [pages])
  const reviewedPages = useMemo(() => pages.filter(p => p.is_reviewed).length, [pages])
  const reviewedChunks = useMemo(() => chunks.filter(c => c.is_reviewed).length, [chunks])
  const documentTagLabels = useMemo(() => collectDocumentTagLabels(chunks), [chunks])
  const taggedChunkCount = useMemo(
    () => chunks.filter(chunk => (chunk.domain_tags || []).length > 0).length,
    [chunks],
  )

  async function runAutoTagDocument() {
    try {
      setAutoTaggingDoc(true)
      const result = await fetchJson(`/documents/${workflowId}/auto-tag-chunks`, { method: 'POST' })
      setMessage(`Auto-tagged ${result.tagged_chunks || 0} chunk(s) with ${result.total_tags || 0} tags`)
      await load()
    } catch (error) {
      setMessage(error.message)
    } finally {
      setAutoTaggingDoc(false)
    }
  }
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

  if (loading) {
    return (
      <div className="p-6 space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-4 w-48" />
        <div className="flex gap-4">
          <Skeleton className="h-[500px] w-[380px]" />
          <Skeleton className="h-[500px] flex-1" />
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
    <div className="flex h-[calc(100vh-3.5rem)] min-h-0 flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 space-y-3 border-b border-border bg-card px-4 py-3">
        <div className="flex items-start gap-3">
          <Button variant="ghost" size="icon" className="mt-0.5 shrink-0" onClick={() => navigate('/documents')}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="max-w-[min(100%,28rem)] truncate text-lg font-serif font-semibold text-foreground">
                {getDocumentListLabel(doc)}
              </h1>
              <Badge variant={doc.authoritative ? 'default' : 'secondary'} className="text-[10px]">
                {doc.authoritative ? 'Authoritative' : 'Legacy'}
              </Badge>
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
                  <span>Reindex required</span>
                </div>
              )}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <span className="font-mono">{getDocumentMetaLabel(doc)}</span>
              <span className="text-border">·</span>
              <span className="max-w-[200px] truncate">{getDocumentFileLabel(doc)}</span>
              <span className="text-border">·</span>
              <span>{doc.page_count || pages.length} pages ({reviewedPages} reviewed)</span>
              <span className="text-border">·</span>
              <span>{doc.chunk_count || chunks.length} chunks ({reviewedChunks} reviewed)</span>
              {(doc.uploaded_by_email || doc.uploaded_by_username) && (
                <>
                  <span className="text-border">·</span>
                  <span title={(doc.uploaded_by_roles || []).join(', ') || undefined}>
                    Uploaded by {doc.uploaded_by_email || doc.uploaded_by_username}
                    {(doc.uploaded_by_roles || []).length > 0
                      ? ` (${(doc.uploaded_by_roles || []).slice(0, 3).join(', ')})`
                      : ''}
                    {doc.created_at ? ` · ${formatCompactDateTime(doc.created_at)}` : ''}
                  </span>
                </>
              )}
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="min-w-0 overflow-x-auto pb-0.5">
            <PipelineStepper currentStage={doc.stage} hasPages={pages.length > 0} hasChunks={chunks.length > 0} />
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-1.5">
            {chunks.length > 0 && (
              <Button
                size="sm"
                variant="outline"
                className="h-8 text-xs"
                onClick={runAutoTagDocument}
                disabled={autoTaggingDoc || !canPipeline}
              >
                <Tag className="mr-1 h-3.5 w-3.5" />
                {autoTaggingDoc ? 'Tagging…' : taggedChunkCount > 0 ? 'Re-run domain tags' : 'Auto-tag chunks'}
              </Button>
            )}
            {visibleActions.slice(0, 4).map(action => (
              <Button
                key={action}
                size="sm"
                variant={action.includes('approve') ? 'success' : action.includes('reindex') ? 'warning' : 'outline'}
                className="h-8 text-xs"
                onClick={() => runAction(action)}
              >
                {summarizeAvailableAction(action)}
              </Button>
            ))}
            {canRemoveDocument && (
              <Button
                size="sm"
                variant="outline"
                className="h-8 text-xs text-destructive border-destructive/30 hover:bg-destructive/10 hover:text-destructive"
                onClick={() => runAction('disable_document')}
              >
                <Trash2 className="mr-1 h-3.5 w-3.5" />
                Remove
              </Button>
            )}
          </div>
        </div>

        {documentTagLabels.length > 0 && (
          <div className="flex items-start gap-2 rounded-md border border-border bg-muted/30 px-3 py-2">
            <Tag className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <div className="min-w-0 space-y-1">
              <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Domain tags · {taggedChunkCount}/{chunks.length} chunks tagged
              </p>
              <DomainTagBadges tags={documentTagLabels} limit={12} />
            </div>
          </div>
        )}

        {message ? (
          <PanelNotice tone={message.toLowerCase().includes('fail') || message.toLowerCase().includes('error') ? 'error' : 'warning'} message={message} />
        ) : null}
      </div>

      {/* Main content */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left: Source preview */}
        <div className="hidden min-h-0 w-[min(100%,400px)] shrink-0 flex-col border-r border-border bg-muted/20 lg:flex">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-surface-warm px-3 py-2.5">
            <span className="text-xs font-medium text-foreground">Source Preview</span>
            {currentPageRecord && (
              <Badge variant={currentPageRecord.is_reviewed ? 'success' : 'secondary'} className="text-[10px]">
                {currentPageRecord.is_reviewed ? 'reviewed' : 'pending'}
              </Badge>
            )}
          </div>
          <div className="min-h-0 flex-1">
            <SourcePdfPreview workflowId={workflowId} currentPage={currentPage} />
          </div>
          <div className="shrink-0 space-y-2 border-t border-border bg-card p-2">
            <PagePager
              pages={sortedPages.length ? sortedPages : Array.from({ length: totalPages }, (_, i) => ({ page_number: i + 1 }))}
              currentPage={currentPage}
              onChange={setCurrentPage}
              getStatus={(p) => (p.is_reviewed ? 'done' : 'pending')}
              label="Preview pages"
            />
            <p className="truncate px-1 text-[10px] text-muted-foreground">
              Page {currentPage} of {getDocumentFileLabel(doc)}
            </p>
          </div>
        </div>

        {/* Right: Tabs */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          <Tabs value={activeTab} onValueChange={setActiveTab} className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <div className="shrink-0 border-b border-border bg-card px-2 sm:px-4">
              <TabsList className="h-11 w-full justify-start gap-0.5 overflow-x-auto rounded-none bg-transparent p-0 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                <TabsTrigger
                  value="ocr"
                  className="h-11 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Eye className="mr-1.5 h-3.5 w-3.5" />OCR
                </TabsTrigger>
                <TabsTrigger
                  value="translation"
                  className="h-11 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Layers className="mr-1.5 h-3.5 w-3.5" />Translation
                </TabsTrigger>
                <TabsTrigger
                  value="chunks"
                  className="h-11 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <FileCode className="mr-1.5 h-3.5 w-3.5" />
                  Chunks
                  {taggedChunkCount > 0 && (
                    <Badge variant="secondary" className="ml-1.5 h-4 px-1 text-[10px]">{taggedChunkCount}</Badge>
                  )}
                </TabsTrigger>
                <TabsTrigger
                  value="index"
                  className="h-11 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Database className="mr-1.5 h-3.5 w-3.5" />Index
                </TabsTrigger>
                <TabsTrigger
                  value="debug"
                  className="h-11 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <Bug className="mr-1.5 h-3.5 w-3.5" />Debug
                </TabsTrigger>
                <TabsTrigger
                  value="audit"
                  className="h-11 shrink-0 rounded-none border-b-2 border-transparent px-3 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
                >
                  <ClipboardList className="mr-1.5 h-3.5 w-3.5" />Audit
                </TabsTrigger>
              </TabsList>
            </div>

            <div className="min-h-0 flex-1 overflow-hidden">
              {/* OCR Review */}
              <TabsContent value="ocr" className="m-0 hidden h-full min-h-0 flex-col data-[state=active]:flex">
                <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border bg-card/60 px-4 py-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-foreground">OCR · Page {currentPage}</span>
                    {currentPageRecord && (
                      <Badge variant={currentPageRecord.is_reviewed ? 'success' : 'secondary'}>
                        {currentPageRecord.is_reviewed ? 'reviewed' : 'pending'}
                      </Badge>
                    )}
                    <span className="text-xs text-muted-foreground">
                      {reviewedPages}/{sortedPages.length || totalPages} reviewed
                    </span>
                  </div>
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
                </div>

                {(pageEdits[currentPage] !== undefined || doc.error_message || isOcrPending || ocrAlreadyPast) && (
                  <div className="shrink-0 space-y-2 border-b border-border px-4 py-2">
                    {pageEdits[currentPage] !== undefined && (
                      <div className="reindex-banner text-xs">
                        <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                        Editing this page may require rechunking and reindexing
                      </div>
                    )}
                    {doc.error_message && (
                      <PanelNotice title="Document Error" message={doc.error_message} />
                    )}
                    {isOcrPending && (
                      <div className="reindex-banner">
                        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-info" />
                        <div className="min-w-0 text-sm">
                          <p className="font-medium text-foreground">OCR is in progress</p>
                          <p className="mt-0.5 break-words text-xs text-muted-foreground">
                            Stage: {runtime?.temporal?.current_stage || runtime?.sqlite_stage || doc.stage}
                            {runtime?.temporal?.status ? ` · Temporal: ${runtime.temporal.status}` : ''}
                            {jobs[0]?.started_at ? ` · Started: ${formatCompactDateTime(jobs[0].started_at)}` : ''}
                          </p>
                        </div>
                      </div>
                    )}
                    {ocrAlreadyPast && (
                      <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
                        <p className="font-medium text-foreground">OCR already completed</p>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          This document is in <strong>{getStageLabel(doc.stage)}</strong>.
                          {doc.stage === 'translation_review'
                            ? ' Use Approve Translation on the Translation tab (not Approve OCR).'
                            : doc.stage === 'chunk_review'
                              ? ' Use Approve Chunks on the Chunks tab.'
                              : ' Approve OCR is only valid during the OCR review stage.'}
                        </p>
                      </div>
                    )}
                  </div>
                )}

                <div className="min-h-0 flex-1 overflow-hidden p-4">
                  {currentPageRecord ? (
                    <div className="grid h-full min-h-0 auto-rows-fr grid-cols-1 gap-4 lg:grid-cols-2">
                      <div className="flex min-h-[42vh] min-w-0 flex-col lg:min-h-0 lg:h-full">
                        <div className="mb-1.5 flex shrink-0 items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Original OCR Output
                          </label>
                          <span className="text-[10px] text-muted-foreground">read-only · scroll</span>
                        </div>
                        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain rounded-md border border-border bg-muted/30 p-3 font-mono text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                          {currentPageOcrText || '(No OCR output)'}
                        </div>
                      </div>
                      <div className="flex min-h-[42vh] min-w-0 flex-col lg:min-h-0 lg:h-full">
                        <div className="mb-1.5 flex shrink-0 items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Edited Text
                          </label>
                          <span className="text-[10px] text-muted-foreground">editable · scroll</span>
                        </div>
                        <div className="min-h-0 flex-1 overflow-hidden rounded-md border border-input bg-background focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2">
                          <Textarea
                            value={pageText}
                            onChange={e => setPageEdits({ ...pageEdits, [currentPage]: e.target.value })}
                            className="h-full min-h-full !min-h-0 resize-none overflow-y-auto border-0 bg-transparent font-mono text-sm leading-relaxed shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
                          />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="flex h-full items-center justify-center">
                      <EmptyPanel icon={FileText} title="No page data available yet" subtitle="OCR content will appear here after the stage emits page markdown" />
                    </div>
                  )}
                </div>

                <div className="shrink-0 border-t border-border bg-card p-2">
                  <PagePager
                    pages={sortedPages}
                    currentPage={currentPage}
                    onChange={setCurrentPage}
                    getStatus={(p) => (p.is_reviewed ? 'done' : 'pending')}
                    label="OCR pages"
                  />
                </div>
              </TabsContent>

              {/* Translation Review */}
              <TabsContent value="translation" className="m-0 hidden h-full min-h-0 flex-col data-[state=active]:flex">
                <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border bg-card/60 px-4 py-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-foreground">Translation · Page {currentPage}</span>
                    <span className="text-xs text-muted-foreground">
                      {translatedPages} of {sortedPages.length || totalPages} translated
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-8"
                      disabled={!canPipeline}
                      onClick={() => runAction('retry_translation')}
                    >
                      <RefreshCw className="mr-1 h-3.5 w-3.5" />Retry Translation
                    </Button>
                    <Button
                      size="sm"
                      variant="success"
                      className="h-8"
                      disabled={!canApproveTranslation}
                      title={!canApproveTranslation ? `Available only in translation_review (current: ${doc.stage})` : undefined}
                      onClick={() => runAction('approve_translation')}
                    >
                      <CheckCircle className="mr-1 h-3.5 w-3.5" />Approve Translation
                    </Button>
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
                  </div>
                )}

                {translationEdits[currentPage] !== undefined && (
                  <div className="shrink-0 border-b border-border px-4 py-2">
                    <div className="reindex-banner text-xs">
                      <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                      Changes to translations will require rechunking and reindexing downstream
                    </div>
                  </div>
                )}

                <div className="min-h-0 flex-1 overflow-hidden p-4">
                  {currentPageRecord && (currentPageRecord.translated_markdown || currentPageRecord.edited_translation) ? (
                    <div className="grid h-full min-h-0 auto-rows-fr grid-cols-1 gap-4 lg:grid-cols-2">
                      <div className="flex min-h-[42vh] min-w-0 flex-col lg:h-full lg:min-h-0">
                        <div className="mb-1.5 flex shrink-0 items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Original OCR Text
                          </label>
                          <span className="text-[10px] text-muted-foreground">read-only · scroll</span>
                        </div>
                        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain rounded-md border border-border bg-muted/30 p-3 font-mono text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                          {currentPageOcrText || '(No OCR output)'}
                        </div>
                      </div>
                      <div className="flex min-h-[42vh] min-w-0 flex-col lg:h-full lg:min-h-0">
                        <div className="mb-1.5 flex shrink-0 items-center justify-between gap-2">
                          <label className="text-xs font-medium text-muted-foreground">
                            Translated Text (Editable)
                          </label>
                          <span className="text-[10px] text-muted-foreground">editable · scroll</span>
                        </div>
                        <div className="min-h-0 flex-1 overflow-hidden rounded-md border border-input bg-background focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2">
                          <Textarea
                            value={translationText}
                            onChange={e => setTranslationEdits({ ...translationEdits, [currentPage]: e.target.value })}
                            className="h-full min-h-full !min-h-0 resize-none overflow-y-auto border-0 bg-transparent font-mono text-sm leading-relaxed shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
                          />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="flex h-full items-center justify-center">
                      <EmptyPanel
                        icon={Layers}
                        title={`No translation for page ${currentPage}`}
                        subtitle={translationEmptySubtitle}
                      />
                    </div>
                  )}
                </div>

                <div className="shrink-0 border-t border-border bg-card p-2">
                  <PagePager
                    pages={sortedPages}
                    currentPage={currentPage}
                    onChange={setCurrentPage}
                    getStatus={(p) => (
                      (p.translation_reviewed || p.translated_markdown || p.edited_translation)
                        ? 'accent'
                        : 'pending'
                    )}
                    label="Translation pages"
                  />
                </div>
              </TabsContent>

              {/* Chunks Review */}
              <TabsContent value="chunks" className="m-0 h-full min-h-0 overflow-y-auto overscroll-contain data-[state=inactive]:hidden">
                <div className="space-y-3 p-4">
                  {doc.stage === 'chunking' && chunkingProgress && (
                    <div className="panel p-3 space-y-2">
                      <div className="flex items-center justify-between text-xs">
                        <span className="font-medium text-foreground">Chunking in progress</span>
                        <span className="text-muted-foreground">
                          {chunkingProgress.pages_processed || 0}/{chunkingProgress.pages_total || 0} pages · {chunkingProgress.chunks_emitted || 0} chunks
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
                    <Button
                      size="sm"
                      variant="success"
                      disabled={!canApproveChunks}
                      title={!canApproveChunks ? `Available only in chunk_review (current: ${doc.stage})` : undefined}
                      onClick={() => runAction('approve_chunks')}
                    >
                      <CheckCircle className="h-3.5 w-3.5 mr-1" />Approve Chunks
                    </Button>
                  </div>

                  {chunks.filter(c => c.reindex_dirty).length > 0 && (
                    <div className="reindex-banner text-xs">
                      <RefreshCw className="h-3.5 w-3.5 text-warning shrink-0" />
                      <span>{chunks.filter(c => c.reindex_dirty).length} chunk(s) have been edited — reindexing required to sync search</span>
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
                              <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground cursor-pointer">
                                <Checkbox checked={!chunk.excluded} />
                                Include
                              </label>
                              <Button variant="ghost" size="sm" className="h-6 text-[10px]"
                                onClick={() => setCurrentPage(chunk.page_start)}
                              >
                                Jump to source
                              </Button>
                              <Button variant="ghost" size="sm" className="h-6 text-[10px]"
                                onClick={() => {
                                  const next = { ...chunkEdits }
                                  delete next[chunk.chunk_number]
                                  setChunkEdits(next)
                                }}
                              >
                                <RotateCcw className="h-3 w-3" />
                              </Button>
                            </div>
                            </div>
                            <DomainTagBadges chunk={chunk} />
                          </div>
                          <div className="p-3">
                            <Textarea
                              value={chunkEdits[chunk.chunk_number] ?? chunk.edited_text ?? chunk.text ?? chunk.original_text ?? ''}
                              onChange={e => setChunkEdits({ ...chunkEdits, [chunk.chunk_number]: e.target.value })}
                              className="text-xs font-mono min-h-[60px] resize-y"
                            />
                            <ChunkTagEditor
                              workflowId={workflowId}
                              chunk={chunk}
                              onSaved={load}
                              onMessage={setMessage}
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
                          : 'Chunks will be generated after the chunking stage completes'
                      }
                    />
                  )}
                </div>
              </TabsContent>

              {/* Index State */}
              <TabsContent value="index" className="m-0 h-full min-h-0 overflow-y-auto overscroll-contain data-[state=inactive]:hidden">
                <div className="space-y-4 p-4">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-foreground">Ingestion & Index State</span>
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" disabled={!canPipeline} onClick={() => runAction('reingest_document')}>
                        <RefreshCw className="h-3.5 w-3.5 mr-1" />Reingest
                      </Button>
                    </div>
                  </div>

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
                          Reindexing is required to sync edited content with the search index
                        </p>
                      </div>
                      <Button size="sm" variant="warning" className="ml-auto shrink-0" disabled={!canPipeline} onClick={() => runAction('reingest_document')}>
                        Reindex Now
                      </Button>
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
              <TabsContent value="debug" className="m-0 h-full min-h-0 overflow-y-auto overscroll-contain data-[state=inactive]:hidden">
                <div className="space-y-4 p-4">
                  <span className="text-sm font-medium text-foreground">Runtime & Debug</span>

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
                          <span className="text-muted-foreground">Reindex Required</span>
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
              <TabsContent value="audit" className="m-0 h-full min-h-0 overflow-y-auto overscroll-contain data-[state=inactive]:hidden">
                <div className="space-y-3 p-4">
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
    </div>
  )
}
