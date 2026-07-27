import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Switch } from '../components/ui/switch'
import {
  Upload,
  FileText,
  CheckCircle,
  X,
  AlertCircle,
  Clock,
  Loader2,
  Sparkles,
  ShieldCheck,
  FileUp,
  ArrowRight,
  RefreshCw,
  ScanText,
  Languages,
  Layers,
  Database,
} from 'lucide-react'
import { API_BASE } from '../config'
import { apiFetch } from '../auth/keycloak'
import { useAuth } from '../auth/AuthProvider'
import { defaultUploadInstance, PORTAL_INSTANCE } from '../lib/instanceLabels'
import { fetchJson, formatCompactDateTime, getDocumentListLabel, summarizeIngestStatus } from '../lib/pipelineUi'

const SUPPORTED_TYPES = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.csv', '.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff']
const MAX_SIZE_MB = 100
const FILE_TYPE_CHIPS = ['PDF', 'Word', 'PowerPoint', 'Excel', 'CSV', 'Images', 'TIFF']

const PIPELINE_STEPS = [
  { id: 'ocr', label: 'OCR', icon: ScanText },
  { id: 'translate', label: 'Translate', icon: Languages },
  { id: 'chunk', label: 'Chunk', icon: Layers },
  { id: 'index', label: 'Index', icon: Database },
]

function formatFileSize(bytes) {
  if (!bytes && bytes !== 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function statusTone(ingest) {
  if (ingest.status === 'success' || ingest.stage === 'completed') return 'success'
  if (ingest.status === 'failed' || ingest.stage === 'failed') return 'destructive'
  return 'info'
}

export default function NewDocumentView() {
  const navigate = useNavigate()
  const { hasPermission, instances, isSuperAdmin, user } = useAuth()
  const canUpload = hasPermission('upload')
  // Instance from login (Keycloak) — not user-selected on this form.
  const documentInstance = defaultUploadInstance({
    isSuperAdmin,
    instances,
    portalInstance: user?.portal_instance || PORTAL_INSTANCE,
  })
  const [file, setFile] = useState(null)
  const [dragging, setDragging] = useState(false)
  const [autoApprove, setAutoApprove] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadSuccess, setUploadSuccess] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [validationError, setValidationError] = useState('')
  const [recentIngests, setRecentIngests] = useState([])
  const [loadingRecent, setLoadingRecent] = useState(true)
  const [lastWorkflowId, setLastWorkflowId] = useState('')

  useEffect(() => {
    loadRecent()
  }, [])

  async function loadRecent() {
    setLoadingRecent(true)
    try {
      const docs = await fetchJson('/documents?limit=10')
      setRecentIngests(Array.isArray(docs) ? docs : [])
    } catch {
      setRecentIngests([])
    } finally {
      setLoadingRecent(false)
    }
  }

  function validateFile(nextFile) {
    const ext = `.${nextFile.name.split('.').pop()?.toLowerCase()}`
    if (!SUPPORTED_TYPES.includes(ext)) {
      return `Unsupported file type: ${ext}. Supported: ${SUPPORTED_TYPES.join(', ')}`
    }
    if (nextFile.size / 1024 / 1024 > MAX_SIZE_MB) {
      return `File too large: ${formatFileSize(nextFile.size)}. Maximum: ${MAX_SIZE_MB} MB`
    }
    return ''
  }

  function handleFile(nextFile) {
    const validation = validateFile(nextFile)
    if (validation) {
      setValidationError(validation)
      setFile(null)
    } else {
      setValidationError('')
      setUploadError('')
      setFile(nextFile)
    }
  }

  const handleDrop = useCallback((event) => {
    event.preventDefault()
    setDragging(false)
    const nextFile = event.dataTransfer.files[0]
    if (nextFile) handleFile(nextFile)
  }, [])

  function handleFileChange(event) {
    if (event.target.files?.[0]) handleFile(event.target.files[0])
  }

  async function handleSubmit() {
    if (!file || !canUpload) return
    if (!documentInstance && !isSuperAdmin) {
      setUploadError(
        'No state on your account. Ask an admin to assign a Keycloak group (e.g. /states/MH/contributor).',
      )
      return
    }
    setUploading(true)
    setUploadError('')
    try {
      const formData = new FormData()
      formData.append('file', file)
      const params = new URLSearchParams({
        auto_approve: String(autoApprove),
        instance: documentInstance || PORTAL_INSTANCE,
      })
      const response = await apiFetch(`${API_BASE}/upload?${params}`, { method: 'POST', body: formData })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Failed to upload and start workflow')
      setLastWorkflowId(data.workflow_id)
      setUploadSuccess(true)
      await loadRecent()
    } catch (submitError) {
      setUploadError(submitError.message)
    } finally {
      setUploading(false)
    }
  }

  function resetUpload() {
    setFile(null)
    setUploadSuccess(false)
    setLastWorkflowId('')
    setUploadError('')
    setValidationError('')
  }

  if (uploadSuccess) {
    return (
      <div className="w-full space-y-4 p-4 sm:p-5">
        <h1 className="font-serif text-xl font-semibold text-foreground">Document queued</h1>
        <div className="panel overflow-hidden">
          <div className="border-b border-border bg-success/5 px-5 py-8 text-center">
            <div className="mx-auto flex size-12 items-center justify-center rounded-full bg-success/15">
              <CheckCircle className="size-6 text-success" />
            </div>
            <p className="mt-3 text-sm text-muted-foreground">
              <span className="font-medium text-foreground">{file?.name}</span> is running through the pipeline.
            </p>
            <p className="mt-2 font-mono text-[11px] text-muted-foreground break-all">{lastWorkflowId}</p>
          </div>
          <div className="flex flex-col-reverse gap-2 p-4 sm:flex-row sm:justify-end">
            <Button variant="outline" size="sm" onClick={resetUpload}>
              Ingest another
            </Button>
            <Button size="sm" onClick={() => navigate(`/documents/${lastWorkflowId}`)}>
              Open document
              <ArrowRight className="ml-1.5 size-3.5" />
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col gap-3 p-3 sm:gap-4 sm:p-4">
      {/* Header — compact, full width */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h1 className="font-serif text-xl font-semibold tracking-tight text-foreground">
            Ingest Document
          </h1>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Upload a file to start OCR, translation, chunking, and indexing.
          </p>
        </div>
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <ShieldCheck className="size-3.5 text-primary" />
          Max {MAX_SIZE_MB} MB · single file
        </div>
      </div>

      {!canUpload ? (
        <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span>You do not have permission to upload documents.</span>
        </div>
      ) : null}

      {/* Full-width grid — no max-width cap */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-12 lg:gap-4">
        {/* Left column */}
        <div className="flex min-w-0 flex-col gap-3 lg:col-span-8">
          <div
            className={[
              'rounded-xl border-2 border-dashed bg-card transition-colors',
              dragging ? 'border-primary bg-primary/5' : 'border-border',
              validationError ? 'border-destructive/50' : '',
              !dragging && !validationError ? 'hover:border-primary/35' : '',
              !canUpload ? 'opacity-60' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            onDragOver={(event) => {
              event.preventDefault()
              if (canUpload) setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={canUpload ? handleDrop : (event) => event.preventDefault()}
          >
            <div className="px-4 py-6 text-center sm:px-6 sm:py-8">
              {file ? (
                <div className="space-y-3">
                  <div className="mx-auto flex size-11 items-center justify-center rounded-xl bg-primary/10">
                    <FileText className="size-5 text-primary" />
                  </div>
                  <div>
                    <p className="mx-auto max-w-md truncate text-sm font-medium text-foreground">
                      {file.name}
                    </p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {formatFileSize(file.size)} ready
                    </p>
                  </div>
                  <div className="flex items-center justify-center gap-2">
                    <Badge variant="outline" className="text-[10px] font-normal">
                      {`.${file.name.split('.').pop()?.toUpperCase() || 'FILE'}`}
                    </Badge>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => {
                        setFile(null)
                        setValidationError('')
                      }}
                    >
                      <X className="mr-1 size-3.5" />
                      Remove
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="mx-auto flex size-11 items-center justify-center rounded-xl bg-muted">
                    <FileUp
                      className={`size-5 ${dragging ? 'text-primary' : 'text-muted-foreground'}`}
                    />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-foreground">
                      {dragging ? 'Drop file to attach' : 'Drop a file here or browse'}
                    </p>
                    <p className="mt-0.5 text-xs text-muted-foreground">One document per upload</p>
                  </div>
                  <div className="flex flex-wrap items-center justify-center gap-1">
                    {FILE_TYPE_CHIPS.map((type) => (
                      <span
                        key={type}
                        className="rounded-md border border-border/80 bg-muted/40 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                      >
                        {type}
                      </span>
                    ))}
                  </div>
                  <label className={canUpload ? 'inline-block' : 'pointer-events-none inline-block'}>
                    <input
                      type="file"
                      className="hidden"
                      disabled={!canUpload}
                      onChange={handleFileChange}
                      accept={SUPPORTED_TYPES.join(',')}
                    />
                    <Button variant="outline" size="sm" className="h-8" asChild disabled={!canUpload}>
                      <span className={canUpload ? 'cursor-pointer' : 'cursor-not-allowed'}>
                        <Upload className="mr-1.5 size-3.5" />
                        Browse files
                      </span>
                    </Button>
                  </label>
                </div>
              )}
            </div>
          </div>

          {validationError ? (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              <AlertCircle className="mt-0.5 size-4 shrink-0" />
              <span>{validationError}</span>
            </div>
          ) : null}

          {uploadError ? (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              <AlertCircle className="mt-0.5 size-4 shrink-0" />
              <span className="min-w-0 flex-1">{uploadError}</span>
              <Button variant="ghost" size="sm" className="h-7 shrink-0 text-xs" onClick={handleSubmit}>
                Retry
              </Button>
            </div>
          ) : null}

          {/* Options row */}
          <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex min-w-0 items-center gap-2.5">
              <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/10">
                <Sparkles className="size-3.5 text-primary" />
              </div>
              <div className="min-w-0">
                <p className="text-sm font-medium text-foreground">Auto-approve stages</p>
                <p className="text-[11px] text-muted-foreground">Skip OCR / translation / chunk review</p>
              </div>
            </div>
            <Switch checked={autoApprove} onCheckedChange={setAutoApprove} disabled={!canUpload} />
          </div>

          {/* Pipeline strip */}
          <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
            {PIPELINE_STEPS.map((step, index) => {
              const Icon = step.icon
              return (
                <React.Fragment key={step.id}>
                  <div className="flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-[11px] text-muted-foreground">
                    <span className="flex size-4 items-center justify-center rounded-full bg-primary/10 text-[9px] font-semibold text-primary">
                      {index + 1}
                    </span>
                    <Icon className="size-3 opacity-70" />
                    {step.label}
                  </div>
                  {index < PIPELINE_STEPS.length - 1 ? (
                    <ArrowRight className="hidden size-3 text-muted-foreground/40 sm:block" />
                  ) : null}
                </React.Fragment>
              )
            })}
          </div>

          <Button
            className="h-10 w-full text-sm font-medium sm:w-auto sm:min-w-[200px]"
            onClick={handleSubmit}
            disabled={!file || uploading || !canUpload}
          >
            {uploading ? (
              <>
                <Loader2 className="mr-2 size-4 animate-spin" />
                Starting…
              </>
            ) : (
              <>
                <Upload className="mr-2 size-4" />
                Start ingestion
              </>
            )}
          </Button>
        </div>

        {/* Recent — fills remaining width */}
        <div className="flex min-h-[280px] min-w-0 flex-col overflow-hidden rounded-xl border border-border bg-card lg:col-span-4 lg:min-h-0 lg:self-stretch">
          <div className="flex items-center justify-between border-b border-border px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
              <Clock className="size-3.5 text-muted-foreground" />
              Recent
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="size-7 p-0"
              onClick={loadRecent}
              disabled={loadingRecent}
              title="Refresh"
            >
              <RefreshCw className={`size-3.5 ${loadingRecent ? 'animate-spin' : ''}`} />
            </Button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {loadingRecent && !recentIngests.length ? (
              <div className="flex items-center justify-center gap-2 px-3 py-10 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />
                Loading…
              </div>
            ) : recentIngests.length ? (
              <div className="divide-y divide-border">
                {recentIngests.map((ingest) => (
                  <button
                    key={ingest.workflow_id}
                    type="button"
                    className="flex w-full flex-col gap-1 px-3 py-2.5 text-left transition-colors hover:bg-muted/40"
                    onClick={() => navigate(`/documents/${ingest.workflow_id}`)}
                  >
                    <div className="flex items-start gap-2">
                      <FileText className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                        {getDocumentListLabel(ingest)}
                      </span>
                      <Badge variant={statusTone(ingest)} className="shrink-0 text-[10px]">
                        {(ingest.status === 'processing' ||
                          String(ingest.stage || '').includes('processing')) && (
                          <Loader2 className="mr-0.5 size-2.5 animate-spin" />
                        )}
                        {summarizeIngestStatus(ingest)}
                      </Badge>
                    </div>
                    <span className="pl-5 text-[10px] text-muted-foreground">
                      {formatCompactDateTime(ingest.updated_at || ingest.created_at)}
                    </span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="px-3 py-10 text-center">
                <p className="text-xs font-medium text-foreground">No recent uploads</p>
                <p className="mt-0.5 text-[11px] text-muted-foreground">They will show up here.</p>
              </div>
            )}
          </div>

          {recentIngests.length > 0 ? (
            <div className="border-t border-border p-2">
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-full text-xs"
                onClick={() => navigate('/documents')}
              >
                View all documents
                <ArrowRight className="ml-1 size-3" />
              </Button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
