import { API_BASE } from '../config'
import { apiFetch } from '../auth/keycloak'

/**
 * User-facing labels for every backend stage (lists, badges, details).
 * Backend still uses technical stage ids; institute operators see plain language.
 */
export const stageMeta = {
  registered: {
    label: 'Uploaded',
    tone: 'neutral',
    shortLabel: 'Uploaded',
    description: 'Document has been uploaded and is waiting to start',
  },
  ocr_processing: {
    label: 'Extracting text',
    tone: 'warning',
    shortLabel: 'Extracting text',
    description: 'Reading text from the document pages',
  },
  ocr_review: {
    label: 'Review text',
    tone: 'accent',
    shortLabel: 'Review text',
    description: 'Check and correct the extracted text',
  },
  translation_processing: {
    label: 'Translating',
    tone: 'warning',
    shortLabel: 'Translating',
    description: 'Translating content into the required language',
  },
  translation_review: {
    label: 'Review translation',
    tone: 'accent',
    shortLabel: 'Review translation',
    description: 'Check and correct translations',
  },
  chunking: {
    label: 'Preparing content',
    tone: 'warning',
    shortLabel: 'Preparing content',
    description: 'Preparing content sections for search',
  },
  chunk_review: {
    label: 'Approve content',
    tone: 'accent',
    shortLabel: 'Approve content',
    description: 'Review content sections and approve for ingest',
  },
  ready_for_ingestion: {
    label: 'Ready to ingest',
    tone: 'success',
    shortLabel: 'Ready to ingest',
    description: 'Final check before publishing to dev',
  },
  ingesting: {
    label: 'Publishing to dev',
    tone: 'warning',
    shortLabel: 'Publishing to dev',
    description: 'Publishing the document to the dev search index',
  },
  approval_for_prod: {
    label: 'Approve for Prod',
    tone: 'accent',
    shortLabel: 'Approve for Prod',
    description: 'Approve publishing this document to production',
  },
  ingesting_prod: {
    label: 'Publishing to Prod',
    tone: 'warning',
    shortLabel: 'Publishing to Prod',
    description: 'Publishing the document to the production search index',
  },
  completed: {
    label: 'Completed',
    tone: 'success',
    shortLabel: 'Completed',
    description: 'Processing is complete',
  },
  failed: {
    label: 'Failed',
    tone: 'danger',
    shortLabel: 'Failed',
    description: 'Something went wrong — retry or ask an admin for help',
  },
}

export const navSections = [
  {
    title: 'Operate',
    items: [
      { to: '/', label: 'Dashboard', end: true },
      { to: '/documents', label: 'Documents' },
      { to: '/queue', label: 'Queue' },
      { to: '/runs', label: 'Runs' }
    ]
  },
  {
    title: 'Inspect',
    items: [
      { to: '/indexes', label: 'Indexes' },
      { to: '/search', label: 'Search' },
      { to: '/audit', label: 'Audit' }
    ]
  },
  {
    title: 'Configure',
    items: [
      { to: '/ingest', label: 'Ingest' },
    ]
  }
]

/** Full backend pipeline order (technical). Prefer USER_PIPELINE_STAGES in UI. */
export const PIPELINE_STAGES = [
  { id: 'registered', label: 'Uploaded', shortLabel: 'Uploaded' },
  { id: 'ocr_processing', label: 'Extracting text', shortLabel: 'Extracting text' },
  { id: 'ocr_review', label: 'Review text', shortLabel: 'Review text' },
  { id: 'translation_processing', label: 'Translating', shortLabel: 'Translating' },
  { id: 'translation_review', label: 'Review translation', shortLabel: 'Review translation' },
  { id: 'chunking', label: 'Preparing content', shortLabel: 'Preparing content' },
  { id: 'chunk_review', label: 'Approve content', shortLabel: 'Approve content' },
  { id: 'ready_for_ingestion', label: 'Ready to ingest', shortLabel: 'Ready to ingest' },
  { id: 'ingesting', label: 'Publishing to dev', shortLabel: 'Publishing to dev' },
  { id: 'approval_for_prod', label: 'Approve for production', shortLabel: 'Approve for production' },
  { id: 'ingesting_prod', label: 'Publishing to prod', shortLabel: 'Publishing to prod' },
  { id: 'completed', label: 'Completed', shortLabel: 'Completed' },
]

/**
 * Simplified steps shown to institute operators.
 * Backend may still be on `chunking` while the UI highlights "Approve content".
 */
export const USER_PIPELINE_STAGES = [
  { id: 'registered', label: 'Uploaded' },
  { id: 'ocr_processing', label: 'Extracting text' },
  { id: 'ocr_review', label: 'Review text' },
  { id: 'translation_processing', label: 'Translating' },
  { id: 'translation_review', label: 'Review translation' },
  // `chunking` is folded into this step so users never see "Chunking"
  { id: 'chunk_review', label: 'Approve content' },
  { id: 'ready_for_ingestion', label: 'Ready to ingest' },
  { id: 'ingesting', label: 'Publishing to dev' },
  { id: 'approval_for_prod', label: 'Approve for production' },
  { id: 'ingesting_prod', label: 'Publishing to prod' },
  { id: 'completed', label: 'Completed' },
]

/** Map any backend stage id → which user-facing step should be active. */
export function mapStageToUserStep(stage) {
  const map = {
    registered: 'registered',
    ocr_processing: 'ocr_processing',
    ocr_review: 'ocr_review',
    translation_processing: 'translation_processing',
    translation_review: 'translation_review',
    chunking: 'chunk_review',
    chunk_review: 'chunk_review',
    ready_for_ingestion: 'ready_for_ingestion',
    ingesting: 'ingesting',
    approval_for_prod: 'approval_for_prod',
    ingesting_prod: 'ingesting_prod',
    completed: 'completed',
    failed: 'failed',
  }
  return map[stage] || stage
}

/** Stages that mean "work is running" (show spinner on the active user step). */
export const RUNNING_BACKEND_STAGES = new Set([
  'ocr_processing',
  'translation_processing',
  'chunking',
  'ingesting',
  'ingesting_prod',
])

export const DEFAULT_SEARCH_SETTINGS = {
  searchMethod: 'HYBRID',
  limit: 12,
  alpha: 0.6,
  rankingMethod: 'rrf',
  showHighlights: true,
  efSearch: 256,
  indexName: 'documents-index',
  candidateCap: 120,
  candidateMultiplier: 10,
  maxChunksPerDoc: 2,
  useE5Prefix: true,
  excludeReference: true,
  queryExpansionProfile: 'gu-v1',
  rerankMode: 'none',
  hybridRrfK: 60
}

export function getDocumentListLabel(doc) {
  return (
    doc?.display_name ||
    doc?.name_en ||
    doc?.name ||
    doc?.filename ||
    doc?.source_filename ||
    // Runs/jobs may only have workflow_id when document row is missing
    (doc?.workflow_id ? String(doc.workflow_id).slice(0, 12) + '…' : null) ||
    'Untitled document'
  )
}

export function getDocumentMetaLabel(doc) {
  if (doc?.workflow_id) return doc.workflow_id
  if (doc?.filename && doc.filename !== getDocumentListLabel(doc)) return doc.filename
  return 'No reference ID'
}

export function getDocumentFileLabel(doc) {
  return doc?.filename || doc?.source_filename || 'No source file'
}

export function getStageLabel(stage, options = {}) {
  const meta = stageMeta[stage]
  if (meta) {
    return options.compact ? meta.shortLabel : meta.label
  }
  return (stage || 'unknown').replace(/_/g, ' ')
}

/**
 * True when `value` represents a UTC timestamp: a Date object (a Date is
 * always UTC internally), a string with an explicit "Z" / "+00:00" suffix,
 * or a bare "YYYY-MM-DDTHH:MM..." string with no zone at all. The backend
 * writes timestamps with Python's `datetime.utcnow().isoformat()`, which
 * omits the zone entirely — so here, no zone means UTC, not "whatever the
 * browser happens to be set to."
 */
export function isUTC(value) {
  if (value instanceof Date) return true
  if (typeof value !== 'string') return false
  const trimmed = value.trim()
  if (!trimmed) return false
  if (/Z$/i.test(trimmed) || /[+-]00:?00$/.test(trimmed)) return true
  if (/[+-]\d{2}:?\d{2}$/.test(trimmed)) return false // explicit non-UTC offset
  return /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(trimmed) // bare ISO, no zone
}

/**
 * Convert any timestamp value to IST (Asia/Kolkata), formatted as
 * "dd-mm-yyyy hh:mm" (24-hour). Values that `isUTC` identifies as UTC are
 * corrected before parsing so `new Date()` can't misread them as local
 * time; values with an explicit non-UTC offset are respected as-is.
 */
export function toIST(value) {
  if (!value) return null
  let date
  if (value instanceof Date) {
    date = value
  } else {
    const trimmed = String(value).trim()
    const hasExplicitZone = /Z$/i.test(trimmed) || /[+-]\d{2}:?\d{2}$/.test(trimmed)
    date = new Date(isUTC(trimmed) && !hasExplicitZone ? `${trimmed.replace(' ', 'T')}Z` : trimmed)
  }
  if (Number.isNaN(date.getTime())) return null

  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date).reduce((acc, part) => {
    acc[part.type] = part.value
    return acc
  }, {})

  return `${parts.day}-${parts.month}-${parts.year} ${parts.hour}:${parts.minute}`
}

export function formatDateTime(value) {
  if (!value) return 'Unknown'
  return toIST(value) || 'Unknown'
}

export function formatCompactDateTime(value) {
  if (!value) return 'Not available'
  return toIST(value) || 'Not available'
}

export function formatCount(value) {
  return new Intl.NumberFormat().format(value || 0)
}

export function summarizeAvailableAction(action) {
  const actionLabels = {
    approve_ocr: 'Approve text',
    approve_translation: 'Approve translation',
    approve_chunks: 'Approve content',
    approve_ingestion: 'Approve publish to dev',
    approve_prod: 'Approve publish to prod',
    request_prod_ready: 'Request prod ready',
    reingest_document: 'Re-ingest',
    mark_reindex_required: 'Mark re-ingest',
    clear_reindex_required: 'Clear re-ingest',
    inspect_runtime: 'Inspect runtime',
    reconcile_document: 'Reconcile',
    disable_document: 'Remove',
    restore_document: 'Restore',
    page_reset: 'Reset page',
    chunk_reset: 'Reset section',
    retry_translation: 'Retry translation',
  }
  return actionLabels[action] || action
    .replace(/_/g, ' ')
    .replace(/\b\w/g, letter => letter.toUpperCase())
}

export function summarizeQueueReason(item) {
  const raw = item?.queue_reason || item?.error_message || ''
  if (!raw) return 'Awaiting action'
  const normalized = raw.toLowerCase()
  if (normalized.includes('ocr')) return 'Text needs review'
  if (normalized.includes('translation')) return 'Translation needs review'
  if (normalized.includes('chunk')) return 'Content needs review'
  if (normalized.includes('reindex') || normalized.includes('reingest')) return 'Re-ingest needed'
  if (normalized.includes('failed')) return 'Processing failed'
  return raw.length > 72 ? `${raw.slice(0, 69)}...` : raw
}

export function summarizeAuditAction(action) {
  const labels = {
    stage_change: 'Stage Change',
    page_edit: 'Page Edit',
    chunk_edit: 'Chunk Edit',
    approval: 'Approval',
    page_reset: 'Page Reset',
    chunk_reset: 'Chunk Reset',
    mark_reindex_required: 'Mark re-ingest',
    clear_reindex_required: 'Clear re-ingest',
    document_upload: 'Upload',
    disable_document: 'Remove Document',
    restore_document: 'Restore Document',
    translation_edit: 'Translation Edit',
    chunk_tag_edit: 'Chunk Tags',
    reingest_started: 'Reingest',
    retry_ocr: 'Retry OCR',
    retry_translation: 'Retry Translation',
    retry_chunking: 'Retry Chunking',
  }
  return labels[action] || summarizeAvailableAction(action)
}

export function summarizeIngestStatus(item) {
  const raw = `${item?.status || ''}`.toLowerCase()
  if (raw === 'success' || item?.stage === 'completed') return 'Completed'
  if (raw === 'failed' || item?.stage === 'failed') return 'Failed'
  if (raw === 'processing') return 'Processing'
  return getStageLabel(item?.stage, { compact: true })
}

export function getAuditActionOptions(logs = []) {
  const counts = logs.reduce((acc, entry) => {
    const key = entry?.action_type || 'unknown'
    acc[key] = (acc[key] || 0) + 1
    return acc
  }, {})

  return Object.entries(counts)
    .sort((left, right) => {
      const countDiff = right[1] - left[1]
      return countDiff || left[0].localeCompare(right[0])
    })
    .map(([value, count]) => ({
      value,
      count,
      label: summarizeAuditAction(value),
    }))
}

function stripMarkdown(value) {
  return `${value || ''}`
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]+\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^>\s+/gm, '')
    .replace(/[*_~#-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

export function getSearchResultTitle(result) {
  return result?.name_en || result?.name || result?.filename || result?.document || 'Search Result'
}

export function getSearchResultSnippet(result) {
  const excerpt = result?.excerpt || result?.text || ''
  const plain = stripMarkdown(excerpt)
  return plain.length > 320 ? `${plain.slice(0, 317)}...` : plain
}

export function getSearchHighlights(result) {
  const rawHighlights = Array.isArray(result?._highlights)
    ? result._highlights
      .map(item => {
        const value = item?.text ?? item
        if (typeof value === 'string') return value
        if (value == null) return ''
        return String(value)
      })
      .filter(Boolean)
    : Array.isArray(result?.highlights)
      ? result.highlights
        .map(value => {
          if (typeof value === 'string') return value
          if (value == null) return ''
          return String(value)
        })
        .filter(Boolean)
      : []
  return [...new Set(rawHighlights)].slice(0, 4)
}

export function summarizeCandidateMethod(candidate) {
  const method = `${candidate?.search_method || candidate?.method || 'raw'}`.toLowerCase()
  const labels = {
    tensor: 'Tensor',
    lexical: 'Lexical',
    hybrid: 'Hybrid',
    raw: 'Raw',
  }
  return labels[method] || method.replace(/\b\w/g, letter => letter.toUpperCase())
}

export function getCandidateHitId(candidate) {
  return candidate?.chunk_id || candidate?._id || candidate?.id || '—'
}

export function getCandidateRank(candidate, index) {
  return candidate?.rank || index + 1
}

export function highlightSearchSnippet(text, highlights) {
  const snippet = typeof text === 'string' ? text : String(text ?? '')
  const safeHighlights = (highlights || [])
    .map(value => (typeof value === 'string' ? value : String(value ?? '')))
    .filter(Boolean)
  if (!safeHighlights.length) return [{ text: snippet, highlighted: false }]
  const escaped = safeHighlights
    .filter(Boolean)
    .map(value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .filter(Boolean)
  if (!escaped.length) return [{ text: snippet, highlighted: false }]
  const regex = new RegExp(`(${escaped.join('|')})`, 'gi')
  return snippet.split(regex).filter(Boolean).map(part => ({
    text: part,
    highlighted: safeHighlights.some(value => value.toLowerCase() === part.toLowerCase())
  }))
}

export async function fetchJson(path, options = {}) {
  const response = await apiFetch(`${API_BASE}${path}`, options)
  const isJson = response.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await response.json() : null
  if (!response.ok) {
    const detail = data?.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail != null
          ? JSON.stringify(detail)
          : `Request failed with ${response.status}`
    throw new Error(message)
  }
  return data
}

export async function fetchAllDocuments() {
  const cohorts = await fetchJson('/documents/cohorts')
  const total = cohorts?.total_documents || 0
  const pageSize = 500
  if (total <= pageSize) {
    return fetchJson(`/documents?limit=${pageSize}`)
  }

  const pages = Math.ceil(total / pageSize)
  const requests = []
  for (let page = 0; page < pages; page += 1) {
    requests.push(fetchJson(`/documents?limit=${pageSize}&offset=${page * pageSize}`))
  }
  const chunks = await Promise.all(requests)
  return chunks.flat()
}

export function inferRunStatusTone(status) {
  const normalized = `${status || ''}`.toLowerCase()
  if (normalized === 'completed' || normalized === 'success') return 'success'
  if (normalized === 'running') return 'warning'
  if (normalized === 'failed' || normalized === 'error') return 'danger'
  return 'neutral'
}

/** Deep-link into Document Ops (Chunks tab, optional chunk highlight). */
export function buildDocumentChunkUrl(workflowId, chunkNumber = null, { tab = 'chunks' } = {}) {
  const params = new URLSearchParams()
  if (tab) params.set('tab', tab)
  if (chunkNumber != null && chunkNumber !== '') {
    params.set('chunk', String(chunkNumber))
  }
  const query = params.toString()
  return `/documents/${workflowId}${query ? `?${query}` : ''}`
}
