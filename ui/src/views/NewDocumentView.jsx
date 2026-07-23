import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Switch } from '../components/ui/switch'
import { Upload, FileText, CheckCircle, X, AlertCircle, Clock, Loader2 } from 'lucide-react'
import { API_BASE } from '../config'
import { apiFetch } from '../auth/keycloak'
import { useAuth } from '../auth/AuthProvider'
import { fetchJson, formatCompactDateTime, getDocumentListLabel, summarizeIngestStatus } from '../lib/pipelineUi'

const SUPPORTED_TYPES = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.csv', '.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff']
const MAX_SIZE_MB = 100
const SUPPORTED_LABEL = 'PDF, Word, PowerPoint, Excel, CSV, TIFF, PNG, JPG, WEBP'

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
  const [lastWorkflowId, setLastWorkflowId] = useState('')

  useEffect(() => {
    loadRecent()
  }, [])

  async function loadRecent() {
    try {
      const docs = await fetchJson('/documents?limit=8')
      setRecentIngests(Array.isArray(docs) ? docs : [])
    } catch {
      setRecentIngests([])
    }
  }

  function validateFile(nextFile) {
    const ext = `.${nextFile.name.split('.').pop()?.toLowerCase()}`
    if (!SUPPORTED_TYPES.includes(ext)) {
      return `Unsupported file type: ${ext}. Supported: ${SUPPORTED_TYPES.join(', ')}`
    }
    if (nextFile.size / 1024 / 1024 > MAX_SIZE_MB) {
      return `File too large: ${(nextFile.size / 1024 / 1024).toFixed(1)} MB. Maximum: ${MAX_SIZE_MB} MB`
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

  if (uploadSuccess) {
    return (
      <div className="p-6 max-w-2xl mx-auto space-y-6">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Ingest Document</h1>
          <p className="text-sm text-muted-foreground mt-1">Upload a document to start a pipeline run</p>
        </div>
        <div className="panel p-8 text-center space-y-4">
          <div className="w-12 h-12 rounded-full bg-success/10 flex items-center justify-center mx-auto">
            <CheckCircle className="h-6 w-6 text-success" />
          </div>
          <div>
            <p className="text-lg font-serif font-semibold text-foreground">Document Ingested</p>
            <p className="text-sm text-muted-foreground mt-1">{file?.name} has been submitted to the pipeline</p>
          </div>
          <div className="flex items-center justify-center gap-3">
            <Button onClick={() => navigate(`/documents/${lastWorkflowId}`)}>Open Document</Button>
            <Button variant="outline" onClick={() => {
              setFile(null)
              setUploadSuccess(false)
              setLastWorkflowId('')
            }}>
              Ingest Another
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-serif font-semibold text-foreground">Ingest Document</h1>
        <p className="text-sm text-muted-foreground mt-1">Upload a document to start a pipeline run</p>
      </div>

      {!canUpload && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-warning/10 border border-warning/30 text-sm text-warning">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>You do not have permission to upload documents. Contact an administrator if you need upload access.</span>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-4">
          <div
            className={`panel border-2 border-dashed transition-colors ${dragging ? 'border-primary bg-primary/5' : validationError ? 'border-destructive/50' : 'border-border'}`}
            onDragOver={event => { event.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
          >
            <div className="p-12 text-center">
              {file ? (
                <div className="space-y-3">
                  <FileText className="h-10 w-10 mx-auto text-primary" />
                  <div>
                    <p className="text-sm font-medium text-foreground truncate max-w-[300px] mx-auto">{file.name}</p>
                    <p className="text-xs text-muted-foreground">{(file.size / 1024 / 1024).toFixed(2)} MB</p>
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => { setFile(null); setValidationError('') }}>
                    <X className="h-3.5 w-3.5 mr-1" />
                    Remove
                  </Button>
                </div>
              ) : (
                <div className="space-y-3">
                  <Upload className="h-10 w-10 mx-auto text-muted-foreground/50" />
                  <div>
                    <p className="text-sm text-foreground">Drop a file here or click to browse</p>
                    <p className="text-xs text-muted-foreground mt-1">
                      Supported: {SUPPORTED_LABEL} · Max {MAX_SIZE_MB} MB
                    </p>
                  </div>
                  <label>
                    <input type="file" className="hidden" onChange={handleFileChange} accept={SUPPORTED_TYPES.join(',')} />
                    <Button variant="outline" size="sm" asChild>
                      <span className="cursor-pointer">Browse Files</span>
                    </Button>
                  </label>
                </div>
              )}
            </div>
          </div>

          {validationError ? (
            <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
              <AlertCircle className="h-4 w-4 text-destructive shrink-0" />
              <span className="text-destructive">{validationError}</span>
            </div>
          ) : null}

          {uploadError ? (
            <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
              <AlertCircle className="h-4 w-4 text-destructive shrink-0" />
              <span className="text-destructive">{uploadError}</span>
              <Button variant="ghost" size="sm" className="ml-auto text-xs h-6" onClick={handleSubmit}>Retry</Button>
            </div>
          ) : null}

          <div className="panel p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-foreground">Auto-approve all stages</p>
                <p className="text-xs text-muted-foreground mt-0.5">Skip manual review for OCR, translation, and chunking</p>
              </div>
              <Switch checked={autoApprove} onCheckedChange={setAutoApprove} />
            </div>
          </div>

          <Button className="w-full h-11" onClick={handleSubmit} disabled={!file || uploading || !canUpload}>
            {uploading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Uploading...
              </>
            ) : (
              <>
                <Upload className="h-4 w-4 mr-2" />
                Start Ingestion
              </>
            )}
          </Button>
        </div>

        <div className="panel h-fit">
          <div className="panel-header flex items-center gap-2">
            <Clock className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-xs font-medium text-foreground">Recent Ingests</span>
          </div>
          <div className="divide-y divide-border">
            {recentIngests.length ? recentIngests.map(ingest => (
              <div
                key={ingest.workflow_id}
                className="px-4 py-2.5 hover:bg-accent/50 cursor-pointer transition-colors"
                onClick={() => navigate(`/documents/${ingest.workflow_id}`)}
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs text-foreground truncate flex-1">{getDocumentListLabel(ingest)}</span>
                  <Badge
                    variant={ingest.status === 'success' || ingest.stage === 'completed' ? 'success' : ingest.status === 'failed' || ingest.stage === 'failed' ? 'destructive' : 'info'}
                    className="text-[10px] shrink-0"
                  >
                    {ingest.status === 'processing' && <Loader2 className="h-2.5 w-2.5 mr-0.5 animate-spin" />}
                    {summarizeIngestStatus(ingest)}
                  </Badge>
                </div>
                <span className="text-[10px] text-muted-foreground">
                  {formatCompactDateTime(ingest.updated_at || ingest.created_at)}
                </span>
              </div>
            )) : (
              <div className="px-4 py-8 text-center text-xs text-muted-foreground">No recent ingests yet.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
