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
  const { hasPermission } = useAuth()
  const canUpload = hasPermission('upload')
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
    setUploading(true)
    setUploadError('')
    try {
      const formData = new FormData()
      formData.append('file', file)
      const response = await apiFetch(`${API_BASE}/upload?auto_approve=${autoApprove}`, { method: 'POST', body: formData })
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
      <div className="p-6 max-w-3xl mx-auto space-y-6">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">Pipeline</p>
          <h1 className="mt-1 text-2xl font-serif font-semibold text-foreground">Ingest Document</h1>
        </div>

        <div className="panel overflow-hidden">
          <div className="border-b border-border bg-success/5 px-6 py-10 text-center">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-success/15 ring-8 ring-success/10">
              <CheckCircle className="h-7 w-7 text-success" />
            </div>
            <p className="mt-5 text-xl font-serif font-semibold text-foreground">Document queued</p>
            <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
              <span className="font-medium text-foreground">{file?.name}</span> was submitted and is now running through the pipeline.
            </p>
          </div>

          <div className="space-y-5 p-6">
            <div className="rounded-lg border border-border bg-muted/40 px-4 py-3">
              <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Workflow ID</p>
              <p className="mt-1 font-mono text-sm text-foreground break-all">{lastWorkflowId}</p>
            </div>

            <div className="flex flex-wrap items-center justify-center gap-2">
              {PIPELINE_STEPS.map((step, index) => {
                const Icon = step.icon
                return (
                  <React.Fragment key={step.id}>
                    <div className="flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground">
                      <Icon className="h-3.5 w-3.5 text-primary" />
                      {step.label}
                    </div>
                    {index < PIPELINE_STEPS.length - 1 ? (
                      <ArrowRight className="h-3.5 w-3.5 text-muted-foreground/50" />
                    ) : null}
                  </React.Fragment>
                )
              })}
            </div>

            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-center">
              <Button variant="outline" onClick={resetUpload}>
                Ingest another
              </Button>
              <Button onClick={() => navigate(`/documents/${lastWorkflowId}`)}>
                Open document
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">Pipeline</p>
          <h1 className="mt-1 text-2xl font-serif font-semibold text-foreground">Ingest Document</h1>
          <p className="mt-1 max-w-xl text-sm text-muted-foreground">
            Upload a source file to register a workflow and run OCR, translation, chunking, and indexing.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <ShieldCheck className="h-3.5 w-3.5 text-primary" />
          <span>Max {MAX_SIZE_MB} MB · single file</span>
        </div>
      </div>

      {!canUpload && (
        <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2.5 text-sm text-warning">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>You do not have permission to upload documents. Contact an administrator if you need upload access.</span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <div className="space-y-4 lg:col-span-3">
          <div
            className={[
              'panel relative border-2 border-dashed transition-all duration-200',
              dragging ? 'border-primary bg-primary/5 shadow-[0_0_0_4px_hsl(var(--primary)/0.08)]' : '',
              validationError ? 'border-destructive/50' : '',
              !dragging && !validationError ? 'border-border hover:border-primary/40' : '',
              !canUpload ? 'opacity-70' : '',
            ].filter(Boolean).join(' ')}
            onDragOver={(event) => {
              event.preventDefault()
              if (canUpload) setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={canUpload ? handleDrop : (event) => event.preventDefault()}
          >
            <div className="p-8 sm:p-12 text-center">
              {file ? (
                <div className="space-y-4">
                  <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 ring-1 ring-primary/20">
                    <FileText className="h-7 w-7 text-primary" />
                  </div>
                  <div>
                    <p className="mx-auto max-w-md truncate text-base font-medium text-foreground">{file.name}</p>
                    <p className="mt-1 text-sm text-muted-foreground">{formatFileSize(file.size)} ready to upload</p>
                  </div>
                  <div className="flex items-center justify-center gap-2">
                    <Badge variant="outline" className="font-normal">
                      {`.${file.name.split('.').pop()?.toUpperCase() || 'FILE'}`}
                    </Badge>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setFile(null)
                        setValidationError('')
                      }}
                    >
                      <X className="mr-1 h-3.5 w-3.5" />
                      Remove
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="space-y-5">
                  <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-muted ring-1 ring-border">
                    <FileUp className={`h-7 w-7 ${dragging ? 'text-primary' : 'text-muted-foreground'}`} />
                  </div>
                  <div>
                    <p className="text-base font-medium text-foreground">
                      {dragging ? 'Drop file to attach' : 'Drop a file here or browse'}
                    </p>
                    <p className="mt-1.5 text-sm text-muted-foreground">
                      One document per upload. Supported formats below.
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center justify-center gap-1.5">
                    {FILE_TYPE_CHIPS.map((type) => (
                      <span
                        key={type}
                        className="rounded-full border border-border bg-muted/50 px-2.5 py-0.5 text-[11px] font-medium text-muted-foreground"
                      >
                        {type}
                      </span>
                    ))}
                  </div>
                  <label className={canUpload ? '' : 'pointer-events-none'}>
                    <input
                      type="file"
                      className="hidden"
                      disabled={!canUpload}
                      onChange={handleFileChange}
                      accept={SUPPORTED_TYPES.join(',')}
                    />
                    <Button variant="outline" size="sm" asChild disabled={!canUpload}>
                      <span className={canUpload ? 'cursor-pointer' : 'cursor-not-allowed'}>
                        <Upload className="mr-2 h-3.5 w-3.5" />
                        Browse files
                      </span>
                    </Button>
                  </label>
                </div>
              )}
            </div>
          </div>

          {validationError ? (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-sm">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              <span className="text-destructive">{validationError}</span>
            </div>
          ) : null}

          {uploadError ? (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-sm">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              <span className="flex-1 text-destructive">{uploadError}</span>
              <Button variant="ghost" size="sm" className="h-7 shrink-0 text-xs" onClick={handleSubmit}>
                Retry
              </Button>
            </div>
          ) : null}

          <div className="panel p-4">
            <div className="flex items-center justify-between gap-4">
              <div className="flex items-start gap-3">
                <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10">
                  <Sparkles className="h-4 w-4 text-primary" />
                </div>
                <div>
                  <p className="text-sm font-medium text-foreground">Auto-approve all stages</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Skip manual review for OCR, translation, and chunking. Best for trusted source files.
                  </p>
                </div>
              </div>
              <Switch checked={autoApprove} onCheckedChange={setAutoApprove} disabled={!canUpload} />
            </div>
          </div>

          <div className="panel p-4">
            <p className="mb-3 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              What happens next
            </p>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {PIPELINE_STEPS.map((step, index) => {
                const Icon = step.icon
                return (
                  <div
                    key={step.id}
                    className="relative rounded-lg border border-border bg-muted/30 px-3 py-2.5"
                  >
                    <div className="flex items-center gap-2">
                      <span className="flex h-6 w-6 items-center justify-center rounded-md bg-primary/10 text-[11px] font-semibold text-primary">
                        {index + 1}
                      </span>
                      <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                    </div>
                    <p className="mt-2 text-xs font-medium text-foreground">{step.label}</p>
                  </div>
                )
              })}
            </div>
          </div>

          <Button
            className="h-11 w-full text-sm font-medium"
            onClick={handleSubmit}
            disabled={!file || uploading || !canUpload}
          >
            {uploading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Uploading and starting workflow…
              </>
            ) : (
              <>
                <Upload className="mr-2 h-4 w-4" />
                Start ingestion
              </>
            )}
          </Button>
        </div>

        <div className="panel flex h-fit flex-col lg:col-span-2">
          <div className="panel-header flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Clock className="h-3.5 w-3.5 text-muted-foreground" />
              <span className="text-xs font-medium text-foreground">Recent ingests</span>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              onClick={loadRecent}
              disabled={loadingRecent}
              title="Refresh"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loadingRecent ? 'animate-spin' : ''}`} />
            </Button>
          </div>

          <div className="divide-y divide-border">
            {loadingRecent && !recentIngests.length ? (
              <div className="flex items-center justify-center gap-2 px-4 py-12 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Loading recent activity…
              </div>
            ) : recentIngests.length ? (
              recentIngests.map((ingest) => (
                <button
                  key={ingest.workflow_id}
                  type="button"
                  className="flex w-full flex-col gap-1.5 px-4 py-3 text-left transition-colors hover:bg-accent/50"
                  onClick={() => navigate(`/documents/${ingest.workflow_id}`)}
                >
                  <div className="flex items-start gap-2">
                    <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                      {getDocumentListLabel(ingest)}
                    </span>
                    <Badge variant={statusTone(ingest)} className="shrink-0 text-[10px]">
                      {(ingest.status === 'processing' || String(ingest.stage || '').includes('processing')) && (
                        <Loader2 className="mr-0.5 h-2.5 w-2.5 animate-spin" />
                      )}
                      {summarizeIngestStatus(ingest)}
                    </Badge>
                  </div>
                  <span className="pl-5 text-[10px] text-muted-foreground">
                    {formatCompactDateTime(ingest.updated_at || ingest.created_at)}
                  </span>
                </button>
              ))
            ) : (
              <div className="px-4 py-12 text-center">
                <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-muted">
                  <Clock className="h-4 w-4 text-muted-foreground" />
                </div>
                <p className="text-xs font-medium text-foreground">No recent ingests</p>
                <p className="mt-1 text-[11px] text-muted-foreground">Uploaded documents will appear here.</p>
              </div>
            )}
          </div>

          {recentIngests.length > 0 ? (
            <div className="border-t border-border p-3">
              <Button variant="ghost" size="sm" className="w-full text-xs" onClick={() => navigate('/documents')}>
                View all documents
                <ArrowRight className="ml-1.5 h-3 w-3" />
              </Button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
