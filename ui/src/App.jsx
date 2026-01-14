import React, { useState, useEffect, useCallback } from 'react'
import { Routes, Route, Link, useParams, useNavigate } from 'react-router-dom'
import { Document, Page, pdfjs } from 'react-pdf'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import './markdown.css'

// Set up PDF.js worker - use cdnjs for better reliability
pdfjs.GlobalWorkerOptions.workerSrc = `//cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`

// Use relative paths that nginx will proxy
const API_BASE = 'http://localhost:8001'
const MARQO_BASE = 'https://localhost:8882'

const styles = {
  container: { maxWidth: '1200px', margin: '0 auto', padding: '20px' },
  wideContainer: { maxWidth: '1600px', margin: '0 auto', padding: '20px' },
  header: {
    background: '#1a1a2e', color: 'white', padding: '16px 20px',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center'
  },
  nav: { display: 'flex', gap: '20px' },
  navLink: { color: 'white', textDecoration: 'none', opacity: 0.8 },
  card: {
    background: 'white', borderRadius: '8px', padding: '20px',
    boxShadow: '0 2px 4px rgba(0,0,0,0.1)', marginBottom: '16px'
  },
  button: {
    background: '#4f46e5', color: 'white', border: 'none',
    padding: '10px 20px', borderRadius: '6px', cursor: 'pointer', fontSize: '14px'
  },
  buttonSecondary: {
    background: '#e5e7eb', color: '#374151', border: 'none',
    padding: '10px 20px', borderRadius: '6px', cursor: 'pointer', fontSize: '14px'
  },
  buttonSuccess: {
    background: '#10b981', color: 'white', border: 'none',
    padding: '10px 20px', borderRadius: '6px', cursor: 'pointer', fontSize: '14px'
  },
  buttonSmall: {
    padding: '6px 12px', fontSize: '12px', borderRadius: '4px',
    border: 'none', cursor: 'pointer'
  },
  input: {
    width: '100%', padding: '12px', border: '1px solid #d1d5db',
    borderRadius: '6px', fontSize: '14px', marginBottom: '12px'
  },
  textarea: {
    width: '100%', padding: '12px', border: '1px solid #d1d5db',
    borderRadius: '6px', fontSize: '14px', minHeight: '200px', fontFamily: 'monospace'
  },
  badge: (stage) => ({
    display: 'inline-block', padding: '4px 12px', borderRadius: '12px',
    fontSize: '12px', fontWeight: '500',
    background: {
      'registered': '#dbeafe', 'ocr_processing': '#fef3c7', 'ocr_review': '#fce7f3',
      'translation_processing': '#fef3c7', 'translation_review': '#e0e7ff',
      'chunking': '#fef3c7', 'chunk_review': '#fce7f3', 'ready_for_ingestion': '#d1fae5',
      'ingesting': '#fef3c7', 'completed': '#d1fae5', 'failed': '#fee2e2'
    }[stage] || '#e5e7eb',
    color: {
      'registered': '#1e40af', 'ocr_processing': '#92400e', 'ocr_review': '#9d174d',
      'translation_processing': '#92400e', 'translation_review': '#3730a3',
      'chunking': '#92400e', 'chunk_review': '#9d174d', 'ready_for_ingestion': '#065f46',
      'ingesting': '#92400e', 'completed': '#065f46', 'failed': '#991b1b'
    }[stage] || '#374151'
  }),
  // Stepper styles
  stepper: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '20px 0', marginBottom: '20px', overflowX: 'auto'
  },
  stepperStep: (status) => ({
    display: 'flex', flexDirection: 'column', alignItems: 'center', flex: '1',
    position: 'relative', minWidth: '80px'
  }),
  stepperCircle: (status) => ({
    width: '32px', height: '32px', borderRadius: '50%',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: '14px', fontWeight: '600', marginBottom: '8px',
    background: status === 'completed' ? '#10b981' : status === 'active' ? '#4f46e5' : status === 'failed' ? '#ef4444' : '#e5e7eb',
    color: status === 'pending' ? '#6b7280' : 'white',
    border: status === 'active' ? '3px solid #c7d2fe' : 'none'
  }),
  stepperLabel: (status) => ({
    fontSize: '11px', textAlign: 'center', color: status === 'active' ? '#4f46e5' : status === 'completed' ? '#065f46' : '#6b7280',
    fontWeight: status === 'active' ? '600' : '400', maxWidth: '70px'
  }),
  stepperLine: (status) => ({
    position: 'absolute', top: '16px', left: '50%', width: '100%', height: '2px',
    background: status === 'completed' ? '#10b981' : '#e5e7eb', zIndex: -1
  }),
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '16px' },
  flex: { display: 'flex', gap: '12px', alignItems: 'center' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '12px', borderBottom: '2px solid #e5e7eb', fontWeight: '600' },
  td: { padding: '12px', borderBottom: '1px solid #e5e7eb' },
  splitPane: {
    display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px', alignItems: 'start'
  },
  pdfContainer: {
    background: '#f3f4f6', borderRadius: '8px', padding: '16px',
    position: 'sticky', top: '20px', maxHeight: 'calc(100vh - 140px)', overflow: 'auto'
  },
  pdfControls: {
    display: 'flex', gap: '8px', alignItems: 'center', justifyContent: 'center',
    marginBottom: '12px', padding: '8px', background: 'white', borderRadius: '6px'
  },
  pageIndicator: {
    background: '#4f46e5', color: 'white', padding: '4px 12px',
    borderRadius: '4px', fontSize: '12px', fontWeight: '500'
  }
}

function Header() {
  return (
    <header style={styles.header}>
      <h1 style={{ fontSize: '20px', fontWeight: '600' }}>Document Ingestion Pipeline</h1>
      <nav style={styles.nav}>
        <Link to="/" style={styles.navLink}>Dashboard</Link>
        <Link to="/search" style={styles.navLink}>Search</Link>
        <Link to="/settings" style={styles.navLink}>Settings</Link>
        <Link to="/audit" style={styles.navLink}>Audit Log</Link>
      </nav>
    </header>
  )
}

function Dashboard() {
  const [documents, setDocuments] = useState([])
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    fetchDocuments()
    const interval = setInterval(fetchDocuments, 5000)
    return () => clearInterval(interval)
  }, [])

  async function fetchDocuments() {
    try {
      const res = await fetch(`${API_BASE}/documents`)
      const data = await res.json()
      setDocuments(data)
    } catch (e) {
      console.error('Failed to fetch documents:', e)
    } finally {
      setLoading(false)
    }
  }

  if (loading) return <div style={styles.container}><p>Loading...</p></div>

  const stages = ['ocr_review', 'chunk_review', 'ocr_processing', 'chunking', 'ingesting', 'completed', 'failed']
  const grouped = stages.reduce((acc, stage) => {
    acc[stage] = documents.filter(d => d.stage === stage)
    return acc
  }, {})

  return (
    <div style={styles.container}>
      <div style={{ ...styles.flex, marginBottom: '24px', justifyContent: 'space-between' }}>
        <h2>Documents ({documents.length})</h2>
        <button style={styles.button} onClick={() => navigate('/new')}>+ New Document</button>
      </div>

      {['ocr_review', 'chunk_review'].map(stage => grouped[stage]?.length > 0 && (
        <div key={stage} style={{ marginBottom: '32px' }}>
          <h3 style={{ marginBottom: '16px', color: '#9d174d' }}>
            Awaiting Review ({grouped[stage].length})
          </h3>
          <div style={styles.grid}>
            {grouped[stage].map(doc => (
              <div key={doc.workflow_id} style={styles.card}>
                <div style={styles.flex}>
                  <span style={styles.badge(doc.stage)}>{doc.stage.replace('_', ' ')}</span>
                </div>
                <h4 style={{ margin: '12px 0' }}>{doc.filename}</h4>
                <p style={{ color: '#6b7280', fontSize: '14px' }}>
                  Pages: {doc.page_count} | Chunks: {doc.chunk_count}
                </p>
                <button
                  style={{ ...styles.button, marginTop: '12px' }}
                  onClick={() => navigate(`/documents/${doc.workflow_id}`)}
                >
                  Review
                </button>
              </div>
            ))}
          </div>
        </div>
      ))}

      <h3 style={{ marginBottom: '16px' }}>All Documents</h3>
      <table style={styles.table}>
        <thead>
          <tr>
            <th style={styles.th}>Filename</th>
            <th style={styles.th}>Stage</th>
            <th style={styles.th}>Pages</th>
            <th style={styles.th}>Chunks</th>
            <th style={styles.th}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {documents.map(doc => (
            <tr key={doc.workflow_id}>
              <td style={styles.td}>{doc.filename}</td>
              <td style={styles.td}><span style={styles.badge(doc.stage)}>{doc.stage.replace('_', ' ')}</span></td>
              <td style={styles.td}>{doc.page_count}</td>
              <td style={styles.td}>{doc.chunk_count}</td>
              <td style={styles.td}>
                <button
                  style={styles.buttonSecondary}
                  onClick={() => navigate(`/documents/${doc.workflow_id}`)}
                >
                  View
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function NewDocument() {
  const [file, setFile] = useState(null)
  const [autoApprove, setAutoApprove] = useState(false)
  const [loading, setLoading] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const navigate = useNavigate()

  async function handleSubmit(e) {
    e.preventDefault()
    if (!file) {
      alert('Please select a PDF file')
      return
    }
    setLoading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)

      const res = await fetch(`${API_BASE}/upload?auto_approve=${autoApprove}`, {
        method: 'POST',
        body: formData
      })
      if (res.ok) {
        navigate('/')
      } else {
        const err = await res.json()
        alert(err.detail || 'Failed to upload and start workflow')
      }
    } catch (e) {
      alert('Failed to upload: ' + e.message)
    } finally {
      setLoading(false)
    }
  }

  function handleDrag(e) {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true)
    } else if (e.type === 'dragleave') {
      setDragActive(false)
    }
  }

  function handleDrop(e) {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      const droppedFile = e.dataTransfer.files[0]
      if (droppedFile.name.toLowerCase().endsWith('.pdf')) {
        setFile(droppedFile)
      } else {
        alert('Only PDF files are allowed')
      }
    }
  }

  function handleFileChange(e) {
    if (e.target.files && e.target.files[0]) {
      setFile(e.target.files[0])
    }
  }

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <h2 style={{ marginBottom: '20px' }}>Upload New Document</h2>
        <form onSubmit={handleSubmit}>
          <div
            style={{
              border: `2px dashed ${dragActive ? '#4f46e5' : '#d1d5db'}`,
              borderRadius: '8px',
              padding: '40px 20px',
              textAlign: 'center',
              marginBottom: '20px',
              background: dragActive ? '#eef2ff' : '#f9fafb',
              cursor: 'pointer',
              transition: 'all 0.2s'
            }}
            onDragEnter={handleDrag}
            onDragLeave={handleDrag}
            onDragOver={handleDrag}
            onDrop={handleDrop}
            onClick={() => document.getElementById('fileInput').click()}
          >
            <input
              id="fileInput"
              type="file"
              accept=".pdf"
              style={{ display: 'none' }}
              onChange={handleFileChange}
            />
            {file ? (
              <div>
                <div style={{ fontSize: '48px', marginBottom: '12px' }}>PDF</div>
                <div style={{ fontWeight: '500' }}>{file.name}</div>
                <div style={{ fontSize: '14px', color: '#6b7280' }}>
                  {(file.size / 1024 / 1024).toFixed(2)} MB
                </div>
              </div>
            ) : (
              <div>
                <div style={{ fontSize: '48px', marginBottom: '12px', opacity: 0.5 }}>+</div>
                <div style={{ fontWeight: '500' }}>Drop a PDF here or click to select</div>
                <div style={{ fontSize: '14px', color: '#6b7280', marginTop: '8px' }}>
                  Only PDF files are supported
                </div>
              </div>
            )}
          </div>

          <label style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '20px' }}>
            <input
              type="checkbox"
              checked={autoApprove}
              onChange={e => setAutoApprove(e.target.checked)}
            />
            Auto-approve (skip manual review)
          </label>
          <div style={styles.flex}>
            <button type="submit" style={styles.button} disabled={loading || !file}>
              {loading ? 'Uploading...' : 'Upload & Start Pipeline'}
            </button>
            <button type="button" style={styles.buttonSecondary} onClick={() => navigate('/')}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// PDF Viewer Component
function PdfViewer({ workflowId, currentPage, onPageChange, numPages, setNumPages }) {
  const [scale, setScale] = useState(1.0)
  const pdfUrl = `${API_BASE}/documents/${workflowId}/pdf`

  function onDocumentLoadSuccess({ numPages }) {
    setNumPages(numPages)
  }

  return (
    <div style={styles.pdfContainer}>
      <div style={styles.pdfControls}>
        <button
          style={{ ...styles.buttonSmall, background: '#e5e7eb', color: '#374151' }}
          onClick={() => onPageChange(Math.max(1, currentPage - 1))}
          disabled={currentPage <= 1}
        >
          Prev
        </button>
        <span style={{ fontSize: '14px' }}>
          Page {currentPage} of {numPages || '?'}
        </span>
        <button
          style={{ ...styles.buttonSmall, background: '#e5e7eb', color: '#374151' }}
          onClick={() => onPageChange(Math.min(numPages || currentPage, currentPage + 1))}
          disabled={currentPage >= numPages}
        >
          Next
        </button>
        <span style={{ margin: '0 8px', color: '#9ca3af' }}>|</span>
        <button
          style={{ ...styles.buttonSmall, background: '#e5e7eb', color: '#374151' }}
          onClick={() => setScale(s => Math.max(0.5, s - 0.1))}
        >
          -
        </button>
        <span style={{ fontSize: '12px', minWidth: '40px', textAlign: 'center' }}>
          {Math.round(scale * 100)}%
        </span>
        <button
          style={{ ...styles.buttonSmall, background: '#e5e7eb', color: '#374151' }}
          onClick={() => setScale(s => Math.min(2, s + 0.1))}
        >
          +
        </button>
      </div>

      <Document
        file={pdfUrl}
        onLoadSuccess={onDocumentLoadSuccess}
        loading={<div style={{ textAlign: 'center', padding: '40px' }}>Loading PDF...</div>}
        error={<div style={{ textAlign: 'center', padding: '40px', color: '#991b1b' }}>Failed to load PDF</div>}
      >
        <Page
          pageNumber={currentPage}
          scale={scale}
          renderTextLayer={true}
          renderAnnotationLayer={true}
        />
      </Document>
    </div>
  )
}

// Pipeline stages in order
const PIPELINE_STAGES = [
  { id: 'registered', label: 'Registered' },
  { id: 'ocr_processing', label: 'OCR' },
  { id: 'ocr_review', label: 'OCR Review' },
  { id: 'translation_processing', label: 'Translation' },
  { id: 'translation_review', label: 'Translation Review' },
  { id: 'chunking', label: 'Chunking' },
  { id: 'chunk_review', label: 'Chunk Review' },
  { id: 'ready_for_ingestion', label: 'Pre-Ingestion' },
  { id: 'ingesting', label: 'Ingesting' },
  { id: 'completed', label: 'Completed' },
]

// Pipeline Stepper Component
// Props: currentStage, hasPages (bool), hasChunks (bool) - used to infer progress for failed workflows
function PipelineStepper({ currentStage, hasPages = false, hasChunks = false }) {
  const isFailed = currentStage === 'failed'

  // For failed workflows, infer the last reached stage from available data
  let effectiveIndex = PIPELINE_STAGES.findIndex(s => s.id === currentStage)
  let failedAtIndex = -1

  if (isFailed) {
    // Infer progress based on what data exists
    if (hasChunks) {
      // Got past chunking, likely failed at ingestion
      failedAtIndex = PIPELINE_STAGES.findIndex(s => s.id === 'ingesting')
      effectiveIndex = failedAtIndex
    } else if (hasPages) {
      // Got past OCR, likely failed during translation or chunking
      failedAtIndex = PIPELINE_STAGES.findIndex(s => s.id === 'chunking')
      effectiveIndex = failedAtIndex
    } else {
      // Failed early, during OCR
      failedAtIndex = PIPELINE_STAGES.findIndex(s => s.id === 'ocr_processing')
      effectiveIndex = failedAtIndex
    }
  }

  return (
    <div style={styles.stepper}>
      {PIPELINE_STAGES.map((stage, index) => {
        let status = 'pending'
        if (isFailed) {
          if (index < effectiveIndex) status = 'completed'
          else if (index === effectiveIndex) status = 'failed'
        } else {
          if (index < effectiveIndex) status = 'completed'
          else if (index === effectiveIndex) status = 'active'
        }

        return (
          <div key={stage.id} style={styles.stepperStep(status)}>
            {index < PIPELINE_STAGES.length - 1 && (
              <div style={styles.stepperLine(index < effectiveIndex ? 'completed' : 'pending')} />
            )}
            <div style={styles.stepperCircle(status)}>
              {status === 'completed' ? '✓' : status === 'failed' ? '✕' : index + 1}
            </div>
            <span style={styles.stepperLabel(status)}>{stage.label}</span>
          </div>
        )
      })}
    </div>
  )
}

function DocumentDetail() {
  const { workflowId } = useParams()
  const [doc, setDoc] = useState(null)
  const [pages, setPages] = useState([])
  const [chunks, setChunks] = useState([])
  const [activeTab, setActiveTab] = useState('pages')
  const [loading, setLoading] = useState(true)
  const [currentPdfPage, setCurrentPdfPage] = useState(1)
  const [numPages, setNumPages] = useState(null)

  // workflowId from URL is already the full workflow ID (e.g., "doc-abc123")
  const fullWorkflowId = workflowId

  useEffect(() => {
    fetchAll()
    const interval = setInterval(fetchAll, 5000)
    return () => clearInterval(interval)
  }, [workflowId])

  async function fetchAll() {
    try {
      const [docRes, pagesRes, chunksRes] = await Promise.all([
        fetch(`${API_BASE}/documents/${fullWorkflowId}`),
        fetch(`${API_BASE}/documents/${fullWorkflowId}/pages`),
        fetch(`${API_BASE}/documents/${fullWorkflowId}/chunks?include_excluded=true`)
      ])
      if (docRes.ok) setDoc(await docRes.json())
      if (pagesRes.ok) setPages(await pagesRes.json())
      if (chunksRes.ok) setChunks(await chunksRes.json())
    } catch (e) {
      console.error('Failed to fetch:', e)
    } finally {
      setLoading(false)
    }
  }

  async function approveOcr() {
    await fetch(`${API_BASE}/documents/${fullWorkflowId}/approve-ocr`, { method: 'POST' })
    fetchAll()
  }

  async function approveTranslation() {
    await fetch(`${API_BASE}/documents/${fullWorkflowId}/approve-translation`, { method: 'POST' })
    fetchAll()
  }

  async function approveChunks() {
    await fetch(`${API_BASE}/documents/${fullWorkflowId}/approve-chunks`, { method: 'POST' })
    fetchAll()
  }

  async function approveIngestion() {
    await fetch(`${API_BASE}/documents/${fullWorkflowId}/approve-ingestion`, { method: 'POST' })
    fetchAll()
  }

  // Count translated pages
  const translatedCount = pages.filter(p => p.translated_markdown).length

  if (loading) return <div style={styles.container}><p>Loading...</p></div>
  if (!doc) return <div style={styles.container}><p>Document not found</p></div>

  return (
    <div style={styles.wideContainer}>
      {/* Pipeline Stepper */}
      <div style={styles.card}>
        <PipelineStepper currentStage={doc.stage} hasPages={doc.page_count > 0} hasChunks={doc.chunk_count > 0} />
      </div>

      {/* Error Banner for Failed Documents */}
      {doc.stage === 'failed' && doc.error_message && (
        <div style={{ ...styles.card, background: '#fef2f2', border: '1px solid #fecaca' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: '12px' }}>
            <span style={{ color: '#dc2626', fontSize: '20px' }}>⚠</span>
            <div>
              <h3 style={{ color: '#991b1b', margin: 0, marginBottom: '4px' }}>Pipeline Failed</h3>
              <p style={{ color: '#b91c1c', margin: 0, fontFamily: 'monospace', fontSize: '13px' }}>{doc.error_message}</p>
            </div>
          </div>
        </div>
      )}

      <div style={styles.card}>
        <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '20px' }}>
          <div>
            <h2>{doc.filename}</h2>
            <p style={{ color: '#6b7280', marginTop: '4px' }}>ID: {fullWorkflowId}</p>
          </div>
          <span style={styles.badge(doc.stage)}>{doc.stage?.replace(/_/g, ' ')}</span>
        </div>

        <div style={{ ...styles.flex, marginBottom: '20px' }}>
          <div style={{ background: '#f3f4f6', padding: '12px 20px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: '600' }}>{doc.page_count}</div>
            <div style={{ fontSize: '12px', color: '#6b7280' }}>Pages</div>
          </div>
          {translatedCount > 0 && (
            <div style={{ background: '#e0e7ff', padding: '12px 20px', borderRadius: '8px' }}>
              <div style={{ fontSize: '24px', fontWeight: '600' }}>{translatedCount}</div>
              <div style={{ fontSize: '12px', color: '#3730a3' }}>Translated</div>
            </div>
          )}
          <div style={{ background: '#f3f4f6', padding: '12px 20px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: '600' }}>{doc.chunk_count}</div>
            <div style={{ fontSize: '12px', color: '#6b7280' }}>Chunks</div>
          </div>
        </div>

        {doc.stage === 'ocr_review' && (
          <button style={styles.buttonSuccess} onClick={approveOcr}>
            Approve OCR & Continue to Translation
          </button>
        )}
        {doc.stage === 'translation_review' && (
          <button style={styles.buttonSuccess} onClick={approveTranslation}>
            Approve Translations & Continue to Chunking
          </button>
        )}
        {doc.stage === 'chunk_review' && (
          <button style={styles.buttonSuccess} onClick={approveChunks}>
            Approve Chunks & Prepare for Ingestion
          </button>
        )}
        {doc.stage === 'ready_for_ingestion' && (
          <button style={styles.buttonSuccess} onClick={approveIngestion}>
            Approve & Ingest to Vector DB
          </button>
        )}
      </div>

      <div style={{ ...styles.flex, marginBottom: '16px' }}>
        {['pages', 'translations', 'chunks', 'overview', 'history'].map(tab => (
          <button
            key={tab}
            style={{
              ...styles.buttonSecondary,
              background: activeTab === tab ? '#4f46e5' : '#e5e7eb',
              color: activeTab === tab ? 'white' : '#374151'
            }}
            onClick={() => setActiveTab(tab)}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
          </button>
        ))}
      </div>

      {activeTab === 'overview' && (
        <div style={styles.card}>
          <h3 style={{ marginBottom: '16px' }}>Timeline</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {doc.created_at && <div>Created: {new Date(doc.created_at).toLocaleString()}</div>}
            {doc.ocr_completed_at && <div>OCR Completed: {new Date(doc.ocr_completed_at).toLocaleString()}</div>}
            {doc.translation_completed_at && <div>Translation Completed: {new Date(doc.translation_completed_at).toLocaleString()}</div>}
            {doc.chunks_completed_at && <div>Chunking Completed: {new Date(doc.chunks_completed_at).toLocaleString()}</div>}
            {doc.ingested_at && <div>Ingested: {new Date(doc.ingested_at).toLocaleString()}</div>}
          </div>
          {doc.error_message && (
            <div style={{ marginTop: '20px', padding: '12px', background: '#fee2e2', borderRadius: '8px', color: '#991b1b' }}>
              Error: {doc.error_message}
            </div>
          )}
        </div>
      )}

      {activeTab === 'pages' && (
        <div style={styles.splitPane}>
          <PdfViewer
            workflowId={fullWorkflowId}
            currentPage={currentPdfPage}
            onPageChange={setCurrentPdfPage}
            numPages={numPages}
            setNumPages={setNumPages}
          />
          <div>
            {pages.length === 0 ? (
              <div style={{ ...styles.card, textAlign: 'center', color: '#6b7280', padding: '40px' }}>
                <p style={{ fontSize: '16px', marginBottom: '8px' }}>No page data available</p>
                <p style={{ fontSize: '13px' }}>This document was processed before page persistence was enabled.</p>
              </div>
            ) : (
              pages.map(page => (
                <PageCard
                  key={page.page_number}
                  page={page}
                  workflowId={fullWorkflowId}
                  onUpdate={fetchAll}
                  isActive={page.page_number === currentPdfPage}
                  onFocus={() => setCurrentPdfPage(page.page_number)}
                />
              ))
            )}
          </div>
        </div>
      )}

      {activeTab === 'translations' && (
        <div style={styles.splitPane}>
          <PdfViewer
            workflowId={fullWorkflowId}
            currentPage={currentPdfPage}
            onPageChange={setCurrentPdfPage}
            numPages={numPages}
            setNumPages={setNumPages}
          />
          <div>
            {pages.length === 0 ? (
              <div style={{ ...styles.card, textAlign: 'center', color: '#6b7280', padding: '40px' }}>
                <p style={{ fontSize: '16px', marginBottom: '8px' }}>No translation data available</p>
                <p style={{ fontSize: '13px' }}>This document was processed before page persistence was enabled.</p>
              </div>
            ) : (
              pages.map(page => (
                <TranslationCard
                  key={page.page_number}
                  page={page}
                  workflowId={fullWorkflowId}
                  onUpdate={fetchAll}
                  isActive={page.page_number === currentPdfPage}
                  onFocus={() => setCurrentPdfPage(page.page_number)}
                />
              ))
            )}
          </div>
        </div>
      )}

      {activeTab === 'chunks' && (
        <div style={styles.splitPane}>
          <PdfViewer
            workflowId={fullWorkflowId}
            currentPage={currentPdfPage}
            onPageChange={setCurrentPdfPage}
            numPages={numPages}
            setNumPages={setNumPages}
          />
          <div>
            {chunks.length === 0 ? (
              <div style={{ ...styles.card, textAlign: 'center', color: '#6b7280', padding: '40px' }}>
                <p style={{ fontSize: '16px', marginBottom: '8px' }}>No chunk data available</p>
                <p style={{ fontSize: '13px' }}>This document was processed before chunk persistence was enabled.</p>
              </div>
            ) : (
              chunks.map(chunk => (
                <ChunkCard
                  key={chunk.chunk_number}
                  chunk={chunk}
                  workflowId={fullWorkflowId}
                  onUpdate={fetchAll}
                  onPageClick={(pageNum) => setCurrentPdfPage(pageNum)}
                />
              ))
            )}
          </div>
        </div>
      )}

      {activeTab === 'history' && (
        <AuditLog workflowId={fullWorkflowId} />
      )}
    </div>
  )
}

function PageCard({ page, workflowId, onUpdate, isActive, onFocus }) {
  const [editing, setEditing] = useState(false)
  const [markdown, setMarkdown] = useState(page.edited_markdown || page.original_markdown)

  async function save() {
    await fetch(`${API_BASE}/documents/${workflowId}/pages/${page.page_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_markdown: markdown, is_reviewed: true })
    })
    setEditing(false)
    onUpdate()
  }

  return (
    <div
      style={{
        ...styles.card,
        border: isActive ? '2px solid #4f46e5' : '2px solid transparent',
        cursor: 'pointer'
      }}
      onClick={() => !editing && onFocus()}
    >
      <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px' }}>
        <div style={styles.flex}>
          <h4>Page {page.page_number}</h4>
          <span style={styles.pageIndicator}>PDF Page {page.page_number}</span>
        </div>
        <div style={styles.flex}>
          {page.is_reviewed && <span style={{ color: '#10b981', fontSize: '14px' }}>Reviewed</span>}
          {!editing ? (
            <button style={styles.buttonSecondary} onClick={(e) => { e.stopPropagation(); setEditing(true) }}>Edit</button>
          ) : (
            <>
              <button style={styles.buttonSuccess} onClick={(e) => { e.stopPropagation(); save() }}>Save</button>
              <button style={styles.buttonSecondary} onClick={(e) => { e.stopPropagation(); setEditing(false) }}>Cancel</button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <textarea
          style={styles.textarea}
          value={markdown}
          onChange={e => setMarkdown(e.target.value)}
          onClick={e => e.stopPropagation()}
        />
      ) : (
        <pre style={{
          background: '#f9fafb', padding: '16px', borderRadius: '6px',
          overflow: 'auto', maxHeight: '400px', whiteSpace: 'pre-wrap', fontSize: '13px'
        }}>
          {page.edited_markdown || page.original_markdown}
        </pre>
      )}
    </div>
  )
}

function TranslationCard({ page, workflowId, onUpdate, isActive, onFocus }) {
  const [editing, setEditing] = useState(false)
  const [translation, setTranslation] = useState(page.edited_translation || page.translated_markdown || '')

  const hasTranslation = page.translated_markdown || page.edited_translation
  const detectedLang = page.detected_language || 'en'

  const langNames = {
    'en': 'English', 'hi': 'Hindi', 'gu': 'Gujarati', 'mr': 'Marathi',
    'ta': 'Tamil', 'te': 'Telugu', 'kn': 'Kannada', 'ml': 'Malayalam',
    'pa': 'Punjabi', 'bn': 'Bengali', 'or': 'Odia'
  }

  async function save() {
    await fetch(`${API_BASE}/documents/${workflowId}/pages/${page.page_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_translation: translation, translation_reviewed: true })
    })
    setEditing(false)
    onUpdate()
  }

  return (
    <div
      style={{
        ...styles.card,
        border: isActive ? '2px solid #4f46e5' : '2px solid transparent',
        cursor: 'pointer'
      }}
      onClick={() => !editing && onFocus()}
    >
      <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px' }}>
        <div style={styles.flex}>
          <h4>Page {page.page_number}</h4>
          <span style={{
            ...styles.pageIndicator,
            background: detectedLang === 'en' ? '#d1fae5' : '#e0e7ff',
            color: detectedLang === 'en' ? '#065f46' : '#3730a3'
          }}>
            {langNames[detectedLang] || detectedLang.toUpperCase()}
          </span>
          {page.translation_reviewed && <span style={{ color: '#10b981', fontSize: '14px' }}>Reviewed</span>}
        </div>
        <div style={styles.flex}>
          {hasTranslation && !editing && (
            <button style={styles.buttonSecondary} onClick={(e) => { e.stopPropagation(); setEditing(true) }}>Edit Translation</button>
          )}
          {editing && (
            <>
              <button style={styles.buttonSuccess} onClick={(e) => { e.stopPropagation(); save() }}>Save</button>
              <button style={styles.buttonSecondary} onClick={(e) => { e.stopPropagation(); setEditing(false) }}>Cancel</button>
            </>
          )}
        </div>
      </div>

      {detectedLang === 'en' ? (
        <div style={{ padding: '16px', background: '#f0fdf4', borderRadius: '6px', color: '#065f46' }}>
          This page is in English - no translation needed
        </div>
      ) : hasTranslation ? (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
          <div>
            <div style={{ fontSize: '12px', fontWeight: '600', color: '#6b7280', marginBottom: '8px' }}>
              Original ({langNames[detectedLang] || detectedLang})
            </div>
            <pre style={{
              background: '#f9fafb', padding: '12px', borderRadius: '6px',
              overflow: 'auto', maxHeight: '300px', whiteSpace: 'pre-wrap', fontSize: '12px'
            }}>
              {page.edited_markdown || page.original_markdown}
            </pre>
          </div>
          <div>
            <div style={{ fontSize: '12px', fontWeight: '600', color: '#6b7280', marginBottom: '8px' }}>
              English Translation
            </div>
            {editing ? (
              <textarea
                style={{ ...styles.textarea, minHeight: '300px' }}
                value={translation}
                onChange={e => setTranslation(e.target.value)}
                onClick={e => e.stopPropagation()}
              />
            ) : (
              <pre style={{
                background: '#eff6ff', padding: '12px', borderRadius: '6px',
                overflow: 'auto', maxHeight: '300px', whiteSpace: 'pre-wrap', fontSize: '12px'
              }}>
                {page.edited_translation || page.translated_markdown}
              </pre>
            )}
          </div>
        </div>
      ) : (
        <div style={{ padding: '16px', background: '#fef3c7', borderRadius: '6px', color: '#92400e' }}>
          Translation pending...
        </div>
      )}
    </div>
  )
}

function ChunkCard({ chunk, workflowId, onUpdate, onPageClick }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(chunk.edited_text || chunk.original_text)

  const pageStart = chunk.page_start || 1
  const pageEnd = chunk.page_end || 1
  const pageRange = pageStart === pageEnd ? `Page ${pageStart}` : `Pages ${pageStart}-${pageEnd}`

  async function save() {
    await fetch(`${API_BASE}/documents/${workflowId}/chunks/${chunk.chunk_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_text: text, is_reviewed: true })
    })
    setEditing(false)
    onUpdate()
  }

  async function toggleExclude() {
    await fetch(`${API_BASE}/documents/${workflowId}/chunks/${chunk.chunk_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_excluded: !chunk.is_excluded })
    })
    onUpdate()
  }

  return (
    <div style={{ ...styles.card, opacity: chunk.is_excluded ? 0.5 : 1 }}>
      <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px' }}>
        <div style={styles.flex}>
          <h4>Chunk {chunk.chunk_number}</h4>
          <span style={{ fontSize: '12px', color: '#6b7280' }}>{chunk.token_count} tokens</span>
          <button
            style={{
              ...styles.pageIndicator,
              cursor: 'pointer',
              background: '#10b981'
            }}
            onClick={() => onPageClick(pageStart)}
            title={`Go to ${pageRange}`}
          >
            {pageRange}
          </button>
        </div>
        <div style={styles.flex}>
          {chunk.is_reviewed && <span style={{ color: '#10b981', fontSize: '14px' }}>Reviewed</span>}
          <button
            style={{ ...styles.buttonSecondary, background: chunk.is_excluded ? '#fee2e2' : '#e5e7eb' }}
            onClick={toggleExclude}
          >
            {chunk.is_excluded ? 'Include' : 'Exclude'}
          </button>
          {!editing ? (
            <button style={styles.buttonSecondary} onClick={() => setEditing(true)}>Edit</button>
          ) : (
            <>
              <button style={styles.buttonSuccess} onClick={save}>Save</button>
              <button style={styles.buttonSecondary} onClick={() => setEditing(false)}>Cancel</button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <textarea
          style={styles.textarea}
          value={text}
          onChange={e => setText(e.target.value)}
        />
      ) : (
        <pre style={{
          background: '#f9fafb', padding: '16px', borderRadius: '6px',
          overflow: 'auto', maxHeight: '300px', whiteSpace: 'pre-wrap', fontSize: '13px'
        }}>
          {chunk.edited_text || chunk.original_text}
        </pre>
      )}
    </div>
  )
}

function AuditLog({ workflowId }) {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [total, setTotal] = useState(0)

  useEffect(() => {
    fetchLogs()
  }, [workflowId, filter])

  async function fetchLogs() {
    setLoading(true)
    try {
      const url = filter === 'all'
        ? `${API_BASE}/documents/${workflowId}/audit?limit=100`
        : `${API_BASE}/documents/${workflowId}/audit?action_type=${filter}&limit=100`
      const res = await fetch(url)
      const data = await res.json()
      setLogs(data.logs || [])
      setTotal(data.total || 0)
    } catch (e) {
      console.error('Failed to fetch audit logs:', e)
    } finally {
      setLoading(false)
    }
  }

  const actionLabels = {
    'stage_change': 'Stage Change',
    'page_edit': 'Page Edit',
    'chunk_edit': 'Chunk Edit',
    'approval': 'Approval',
    'page_reset': 'Page Reset',
    'chunk_reset': 'Chunk Reset'
  }

  return (
    <div style={styles.card}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h3 style={{ margin: 0 }}>Audit Trail ({total} entries)</h3>
        <select
          value={filter}
          onChange={e => setFilter(e.target.value)}
          style={{ padding: '8px 12px', borderRadius: '6px', border: '1px solid #d1d5db', fontSize: '14px' }}
        >
          <option value="all">All Actions</option>
          <option value="stage_change">Stage Changes</option>
          <option value="page_edit">Page Edits</option>
          <option value="chunk_edit">Chunk Edits</option>
          <option value="approval">Approvals</option>
          <option value="page_reset">Page Resets</option>
          <option value="chunk_reset">Chunk Resets</option>
        </select>
      </div>

      {loading ? (
        <p style={{ textAlign: 'center', color: '#6b7280' }}>Loading audit logs...</p>
      ) : logs.length === 0 ? (
        <p style={{ textAlign: 'center', color: '#6b7280', padding: '40px 0' }}>
          No audit entries found. Changes will appear here as you edit pages, chunks, or approve stages.
        </p>
      ) : (
        <div style={{ maxHeight: '600px', overflow: 'auto' }}>
          {logs.map(log => (
            <AuditLogEntry key={log.id} log={log} />
          ))}
        </div>
      )}
    </div>
  )
}

function AuditLogEntry({ log }) {
  const [expanded, setExpanded] = useState(false)

  const actionColors = {
    'stage_change': '#4f46e5',
    'page_edit': '#10b981',
    'chunk_edit': '#f59e0b',
    'approval': '#8b5cf6',
    'page_reset': '#ef4444',
    'chunk_reset': '#ef4444'
  }

  const actionLabels = {
    'stage_change': 'Stage Change',
    'page_edit': 'Page Edit',
    'chunk_edit': 'Chunk Edit',
    'approval': 'Approval',
    'page_reset': 'Page Reset',
    'chunk_reset': 'Chunk Reset'
  }

  function formatValue(jsonStr) {
    if (!jsonStr) return '(empty)'
    try {
      const parsed = JSON.parse(jsonStr)
      if (typeof parsed === 'string') return parsed
      if (typeof parsed === 'boolean') return parsed ? 'Yes' : 'No'
      return JSON.stringify(parsed, null, 2)
    } catch {
      return jsonStr
    }
  }

  function getDescription() {
    if (log.action_type === 'stage_change') {
      return `${formatValue(log.old_value)} → ${formatValue(log.new_value)}`
    }
    if (log.action_type === 'approval') {
      const meta = log.metadata ? JSON.parse(log.metadata) : {}
      return `Approved at ${meta.stage || 'unknown stage'}`
    }
    if (log.entity_type && log.entity_id) {
      return `${log.entity_type} #${log.entity_id}: ${log.field_name || ''}`
    }
    return log.field_name || ''
  }

  const color = actionColors[log.action_type] || '#6b7280'
  const hasDetails = log.old_value || log.new_value

  return (
    <div
      style={{
        padding: '12px 16px',
        borderBottom: '1px solid #e5e7eb',
        cursor: hasDetails ? 'pointer' : 'default'
      }}
      onClick={() => hasDetails && setExpanded(!expanded)}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <span style={{
          background: color + '20',
          color: color,
          padding: '4px 10px',
          borderRadius: '4px',
          fontSize: '12px',
          fontWeight: '500',
          whiteSpace: 'nowrap'
        }}>
          {actionLabels[log.action_type] || log.action_type}
        </span>
        <span style={{ flex: 1, fontSize: '14px', color: '#374151' }}>
          {getDescription()}
        </span>
        <span style={{ fontSize: '12px', color: '#9ca3af', whiteSpace: 'nowrap' }}>
          {new Date(log.timestamp).toLocaleString()}
        </span>
        {hasDetails && (
          <span style={{ color: '#9ca3af', fontSize: '12px' }}>
            {expanded ? '▼' : '▶'}
          </span>
        )}
      </div>

      {expanded && hasDetails && (
        <div style={{ marginTop: '12px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <div>
            <div style={{ fontWeight: '600', marginBottom: '6px', color: '#991b1b', fontSize: '12px' }}>Before</div>
            <pre style={{
              background: '#fef2f2',
              padding: '10px',
              borderRadius: '6px',
              overflow: 'auto',
              maxHeight: '200px',
              whiteSpace: 'pre-wrap',
              fontSize: '12px',
              margin: 0,
              border: '1px solid #fecaca'
            }}>
              {formatValue(log.old_value)}
            </pre>
          </div>
          <div>
            <div style={{ fontWeight: '600', marginBottom: '6px', color: '#065f46', fontSize: '12px' }}>After</div>
            <pre style={{
              background: '#f0fdf4',
              padding: '10px',
              borderRadius: '6px',
              overflow: 'auto',
              maxHeight: '200px',
              whiteSpace: 'pre-wrap',
              fontSize: '12px',
              margin: 0,
              border: '1px solid #bbf7d0'
            }}>
              {formatValue(log.new_value)}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

// Default search settings (fallback if API fails)
const DEFAULT_SEARCH_SETTINGS = {
  searchMethod: 'HYBRID',
  limit: 10,
  alpha: 0.7,
  rankingMethod: 'rrf',
  showHighlights: true,
  efSearch: 256
}

function Search() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [settings, setSettings] = useState(DEFAULT_SEARCH_SETTINGS)
  const [searchTime, setSearchTime] = useState(null)
  const [settingsLoading, setSettingsLoading] = useState(true)

  // Fetch settings from API on mount
  useEffect(() => {
    fetchSettings()
  }, [])

  async function fetchSettings() {
    try {
      const res = await fetch(`${API_BASE}/settings/search`)
      if (res.ok) {
        const data = await res.json()
        setSettings(data)
      }
    } catch (e) {
      console.error('Failed to fetch settings:', e)
    } finally {
      setSettingsLoading(false)
    }
  }

  async function handleSearch(e) {
    e.preventDefault()
    if (!query.trim()) return
    setLoading(true)
    setSearchTime(null)
    const startTime = performance.now()
    try {
      // Build search request based on settings
      const searchBody = {
        q: query,
        limit: settings.limit,
        showHighlights: settings.showHighlights,
        searchMethod: settings.searchMethod,
        efSearch: settings.efSearch
      }

      // Add hybrid-specific settings
      if (settings.searchMethod === 'HYBRID') {
        searchBody.hybridParameters = {
          alpha: settings.alpha,
          rankingMethod: settings.rankingMethod,
          searchableAttributesLexical: ['text', 'name'],
          searchableAttributesTensor: ['text']
        }
      }

      const res = await fetch(`${MARQO_BASE}/indexes/documents-index/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(searchBody)
      })
      const data = await res.json()
      setResults(data.hits || [])
      setSearchTime(performance.now() - startTime)
    } catch (e) {
      console.error('Search failed:', e)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '16px' }}>
          <h2 style={{ margin: 0 }}>Search Documents</h2>
          <Link to="/settings" style={{ ...styles.buttonSecondary, textDecoration: 'none' }}>
            Configure Search
          </Link>
        </div>

        <form onSubmit={handleSearch} style={styles.flex}>
          <input
            style={{ ...styles.input, flex: 1, marginBottom: 0 }}
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search veterinary documents..."
          />
          <button type="submit" style={styles.button} disabled={loading || settingsLoading}>
            {loading ? 'Searching...' : 'Search'}
          </button>
        </form>

        {/* Search info badges */}
        <div style={{ marginTop: '12px', display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{
            background: settings.searchMethod === 'HYBRID' ? '#dbeafe' : settings.searchMethod === 'TENSOR' ? '#e0e7ff' : '#fef3c7',
            color: settings.searchMethod === 'HYBRID' ? '#1e40af' : settings.searchMethod === 'TENSOR' ? '#3730a3' : '#92400e',
            padding: '4px 8px', borderRadius: '4px', fontSize: '11px'
          }}>
            {settings.searchMethod}
            {settings.searchMethod === 'HYBRID' && ` (α=${settings.alpha})`}
          </span>
          <span style={{ background: '#f3f4f6', color: '#374151', padding: '4px 8px', borderRadius: '4px', fontSize: '11px' }}>
            {settings.limit} results
          </span>
          {searchTime && (
            <span style={{ background: '#d1fae5', color: '#065f46', padding: '4px 8px', borderRadius: '4px', fontSize: '11px' }}>
              {searchTime.toFixed(0)}ms
            </span>
          )}
        </div>
      </div>

      {results.length > 0 && (
        <div>
          <h3 style={{ margin: '24px 0 16px' }}>Results ({results.length})</h3>
          {results.map((hit, i) => (
            <div key={i} style={styles.card}>
              <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px' }}>
                <div style={styles.flex}>
                  <h4>{hit.name}</h4>
                  {hit.page_start && (
                    <span style={styles.pageIndicator}>
                      {hit.page_start === hit.page_end
                        ? `Page ${hit.page_start}`
                        : `Pages ${hit.page_start}-${hit.page_end}`}
                    </span>
                  )}
                </div>
                <span style={{
                  background: '#dbeafe', color: '#1e40af',
                  padding: '4px 8px', borderRadius: '4px', fontSize: '12px'
                }}>
                  Score: {hit._score?.toFixed(3)}
                </span>
              </div>
              <div className="markdown-content" style={{
                fontSize: '14px',
                lineHeight: '1.6',
                maxHeight: '300px',
                overflow: 'auto',
                padding: '12px',
                background: '#f9fafb',
                borderRadius: '6px'
              }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {hit.text}
                </ReactMarkdown>
              </div>
              <div style={{ marginTop: '12px', fontSize: '12px', color: '#6b7280' }}>
                Chunk #{hit.chunk_num} | {hit.token_count} tokens | Source: {hit.source}
              </div>
              {hit._highlights?.[0] && (
                <div style={{
                  marginTop: '12px', padding: '12px', background: '#fef3c7',
                  borderRadius: '6px', fontSize: '13px'
                }}>
                  <strong>Highlight:</strong>{' '}
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
                    p: ({children}) => <span>{children}</span>
                  }}>
                    {hit._highlights[0].text}
                  </ReactMarkdown>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Settings() {
  const [settings, setSettings] = useState(DEFAULT_SEARCH_SETTINGS)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [auditLogs, setAuditLogs] = useState([])
  const [auditLoading, setAuditLoading] = useState(true)

  useEffect(() => {
    fetchSettings()
    fetchAuditLogs()
  }, [])

  async function fetchSettings() {
    try {
      const res = await fetch(`${API_BASE}/settings/search`)
      if (res.ok) {
        const data = await res.json()
        setSettings(data)
      }
    } catch (e) {
      console.error('Failed to fetch settings:', e)
    } finally {
      setLoading(false)
    }
  }

  async function fetchAuditLogs() {
    try {
      const res = await fetch(`${API_BASE}/settings/search/audit?limit=20`)
      if (res.ok) {
        const data = await res.json()
        setAuditLogs(data.logs || [])
      }
    } catch (e) {
      console.error('Failed to fetch audit logs:', e)
    } finally {
      setAuditLoading(false)
    }
  }

  function updateSetting(key, value) {
    setSettings(prev => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  async function saveSettings() {
    setSaving(true)
    try {
      const res = await fetch(`${API_BASE}/settings/search`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
      })
      if (res.ok) {
        setSaved(true)
        fetchAuditLogs()  // Refresh audit logs
        setTimeout(() => setSaved(false), 3000)
      }
    } catch (e) {
      console.error('Failed to save settings:', e)
    } finally {
      setSaving(false)
    }
  }

  async function resetSettings() {
    setSaving(true)
    try {
      const res = await fetch(`${API_BASE}/settings/search/reset`, {
        method: 'POST'
      })
      if (res.ok) {
        const data = await res.json()
        setSettings(data)
        setSaved(true)
        fetchAuditLogs()
        setTimeout(() => setSaved(false), 3000)
      }
    } catch (e) {
      console.error('Failed to reset settings:', e)
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div style={styles.container}>
        <div style={styles.card}>
          <p>Loading settings...</p>
        </div>
      </div>
    )
  }

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '24px' }}>
          <h2 style={{ margin: 0 }}>Search Settings</h2>
          <div style={styles.flex}>
            {saved && (
              <span style={{ color: '#059669', fontSize: '14px' }}>Saved!</span>
            )}
            <button
              style={styles.buttonSecondary}
              onClick={resetSettings}
              disabled={saving}
            >
              Reset to Defaults
            </button>
            <button
              style={styles.button}
              onClick={saveSettings}
              disabled={saving}
            >
              {saving ? 'Saving...' : 'Save Settings'}
            </button>
          </div>
        </div>

        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
          gap: '24px'
        }}>
          {/* Search Method */}
          <div style={{ background: '#f9fafb', padding: '16px', borderRadius: '8px' }}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: '600', marginBottom: '8px', color: '#111827' }}>
              Search Method
            </label>
            <select
              value={settings.searchMethod}
              onChange={e => updateSetting('searchMethod', e.target.value)}
              style={{ ...styles.input, marginBottom: '8px' }}
            >
              <option value="TENSOR">Tensor (Semantic)</option>
              <option value="LEXICAL">Lexical (Keyword)</option>
              <option value="HYBRID">Hybrid (Both)</option>
            </select>
            <p style={{ fontSize: '12px', color: '#6b7280', margin: 0 }}>
              {settings.searchMethod === 'TENSOR' && 'Uses AI embeddings to find semantically similar content, even with different wording.'}
              {settings.searchMethod === 'LEXICAL' && 'Traditional keyword matching using BM25 algorithm. Best for exact terms.'}
              {settings.searchMethod === 'HYBRID' && 'Combines semantic understanding with keyword matching for best results.'}
            </p>
          </div>

          {/* Alpha Slider (only for HYBRID) */}
          {settings.searchMethod === 'HYBRID' && (
            <div style={{ background: '#f9fafb', padding: '16px', borderRadius: '8px' }}>
              <label style={{ display: 'block', fontSize: '14px', fontWeight: '600', marginBottom: '8px', color: '#111827' }}>
                Hybrid Balance (Alpha): {settings.alpha.toFixed(2)}
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={settings.alpha}
                onChange={e => updateSetting('alpha', parseFloat(e.target.value))}
                style={{ width: '100%', marginBottom: '8px' }}
              />
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: '#6b7280' }}>
                <span>Lexical (0.0)</span>
                <span style={{ fontWeight: '600', color: '#4f46e5' }}>
                  {settings.alpha < 0.3 ? 'Keyword-heavy' : settings.alpha > 0.7 ? 'Semantic-heavy' : 'Balanced'}
                </span>
                <span>Semantic (1.0)</span>
              </div>
              <p style={{ fontSize: '12px', color: '#6b7280', margin: '8px 0 0' }}>
                Lower values favor exact keyword matches. Higher values favor meaning-based matches.
              </p>
            </div>
          )}

          {/* Ranking Method (only for HYBRID) */}
          {settings.searchMethod === 'HYBRID' && (
            <div style={{ background: '#f9fafb', padding: '16px', borderRadius: '8px' }}>
              <label style={{ display: 'block', fontSize: '14px', fontWeight: '600', marginBottom: '8px', color: '#111827' }}>
                Ranking Method
              </label>
              <select
                value={settings.rankingMethod}
                onChange={e => updateSetting('rankingMethod', e.target.value)}
                style={{ ...styles.input, marginBottom: '8px' }}
              >
                <option value="rrf">RRF (Reciprocal Rank Fusion)</option>
                <option value="normalize_linear">Normalize Linear</option>
              </select>
              <p style={{ fontSize: '12px', color: '#6b7280', margin: 0 }}>
                {settings.rankingMethod === 'rrf' && 'Combines rankings from both methods. Generally produces better results.'}
                {settings.rankingMethod === 'normalize_linear' && 'Linearly combines normalized scores. More predictable weighting.'}
              </p>
            </div>
          )}

          {/* Result Limit */}
          <div style={{ background: '#f9fafb', padding: '16px', borderRadius: '8px' }}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: '600', marginBottom: '8px', color: '#111827' }}>
              Results per Search: {settings.limit}
            </label>
            <input
              type="range"
              min="5"
              max="50"
              step="5"
              value={settings.limit}
              onChange={e => updateSetting('limit', parseInt(e.target.value))}
              style={{ width: '100%', marginBottom: '8px' }}
            />
            <p style={{ fontSize: '12px', color: '#6b7280', margin: 0 }}>
              Number of search results to return. More results = broader coverage but potentially less relevant.
            </p>
          </div>

          {/* efSearch (for TENSOR/HYBRID) */}
          {settings.searchMethod !== 'LEXICAL' && (
            <div style={{ background: '#f9fafb', padding: '16px', borderRadius: '8px' }}>
              <label style={{ display: 'block', fontSize: '14px', fontWeight: '600', marginBottom: '8px', color: '#111827' }}>
                Search Accuracy (efSearch): {settings.efSearch}
              </label>
              <input
                type="range"
                min="64"
                max="512"
                step="64"
                value={settings.efSearch}
                onChange={e => updateSetting('efSearch', parseInt(e.target.value))}
                style={{ width: '100%', marginBottom: '8px' }}
              />
              <p style={{ fontSize: '12px', color: '#6b7280', margin: 0 }}>
                HNSW search parameter. Higher values improve accuracy but increase search time.
              </p>
            </div>
          )}

          {/* Show Highlights */}
          <div style={{ background: '#f9fafb', padding: '16px', borderRadius: '8px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '12px', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={settings.showHighlights}
                onChange={e => updateSetting('showHighlights', e.target.checked)}
                style={{ width: '18px', height: '18px' }}
              />
              <div>
                <span style={{ fontSize: '14px', fontWeight: '600', color: '#111827' }}>Show Highlights</span>
                <p style={{ fontSize: '12px', color: '#6b7280', margin: '4px 0 0' }}>
                  Display highlighted matching text snippets in search results.
                </p>
              </div>
            </label>
          </div>
        </div>
      </div>

      {/* Audit Log Section */}
      <div style={styles.card}>
        <h3 style={{ marginBottom: '16px' }}>Settings Change History</h3>
        {auditLoading ? (
          <p style={{ color: '#6b7280' }}>Loading audit logs...</p>
        ) : auditLogs.length === 0 ? (
          <p style={{ color: '#6b7280' }}>No settings changes recorded yet.</p>
        ) : (
          <div style={{ maxHeight: '400px', overflow: 'auto' }}>
            <table style={{ ...styles.table, fontSize: '13px' }}>
              <thead>
                <tr>
                  <th style={styles.th}>Time</th>
                  <th style={styles.th}>Setting</th>
                  <th style={styles.th}>Old Value</th>
                  <th style={styles.th}>New Value</th>
                </tr>
              </thead>
              <tbody>
                {auditLogs.map((log, i) => (
                  <tr key={i}>
                    <td style={styles.td}>
                      {new Date(log.timestamp).toLocaleString()}
                    </td>
                    <td style={styles.td}>
                      <code style={{ background: '#f3f4f6', padding: '2px 6px', borderRadius: '4px' }}>
                        {log.field_name}
                      </code>
                    </td>
                    <td style={styles.td}>
                      <span style={{ color: '#dc2626' }}>{log.old_value || '-'}</span>
                    </td>
                    <td style={styles.td}>
                      <span style={{ color: '#059669' }}>{log.new_value || '-'}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// Global Audit Log page - shows all audit entries across all documents
function GlobalAuditLog() {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const limit = 50
  const navigate = useNavigate()

  useEffect(() => {
    fetchLogs()
  }, [filter, offset])

  async function fetchLogs() {
    setLoading(true)
    try {
      const url = filter === 'all'
        ? `${API_BASE}/audit?limit=${limit}&offset=${offset}`
        : `${API_BASE}/audit?action_type=${filter}&limit=${limit}&offset=${offset}`
      const res = await fetch(url)
      const data = await res.json()
      setLogs(data.logs || [])
      setTotal(data.total || 0)
    } catch (e) {
      console.error('Failed to fetch audit logs:', e)
    } finally {
      setLoading(false)
    }
  }

  const totalPages = Math.ceil(total / limit)
  const currentPage = Math.floor(offset / limit) + 1

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
          <div>
            <h2 style={{ margin: 0 }}>Global Audit Log</h2>
            <p style={{ color: '#6b7280', fontSize: '14px', marginTop: '4px' }}>
              Track all changes across all documents
            </p>
          </div>
          <select
            value={filter}
            onChange={e => { setFilter(e.target.value); setOffset(0) }}
            style={{ padding: '8px 12px', borderRadius: '6px', border: '1px solid #d1d5db', fontSize: '14px' }}
          >
            <option value="all">All Actions</option>
            <option value="stage_change">Stage Changes</option>
            <option value="page_edit">Page Edits</option>
            <option value="chunk_edit">Chunk Edits</option>
            <option value="approval">Approvals</option>
            <option value="page_reset">Page Resets</option>
            <option value="chunk_reset">Chunk Resets</option>
          </select>
        </div>

        {loading ? (
          <p style={{ textAlign: 'center', color: '#6b7280', padding: '40px 0' }}>Loading audit logs...</p>
        ) : logs.length === 0 ? (
          <p style={{ textAlign: 'center', color: '#6b7280', padding: '40px 0' }}>
            No audit entries found. Changes will appear here as documents are edited and approved.
          </p>
        ) : (
          <>
            <div style={{ marginBottom: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ color: '#6b7280', fontSize: '14px' }}>
                Showing {offset + 1}-{Math.min(offset + limit, total)} of {total} entries
              </span>
              <div style={styles.flex}>
                <button
                  style={{ ...styles.buttonSecondary, opacity: offset === 0 ? 0.5 : 1 }}
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                  disabled={offset === 0}
                >
                  Previous
                </button>
                <span style={{ fontSize: '14px', color: '#374151' }}>
                  Page {currentPage} of {totalPages}
                </span>
                <button
                  style={{ ...styles.buttonSecondary, opacity: offset + limit >= total ? 0.5 : 1 }}
                  onClick={() => setOffset(offset + limit)}
                  disabled={offset + limit >= total}
                >
                  Next
                </button>
              </div>
            </div>

            <div style={{ maxHeight: '600px', overflow: 'auto' }}>
              {logs.map(log => (
                <GlobalAuditLogEntry key={log.id} log={log} onNavigate={(wfId) => navigate(`/documents/${wfId}`)} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function GlobalAuditLogEntry({ log, onNavigate }) {
  const [expanded, setExpanded] = useState(false)

  const actionColors = {
    'stage_change': '#4f46e5',
    'page_edit': '#10b981',
    'chunk_edit': '#f59e0b',
    'approval': '#8b5cf6',
    'page_reset': '#ef4444',
    'chunk_reset': '#ef4444'
  }

  const actionLabels = {
    'stage_change': 'Stage Change',
    'page_edit': 'Page Edit',
    'chunk_edit': 'Chunk Edit',
    'approval': 'Approval',
    'page_reset': 'Page Reset',
    'chunk_reset': 'Chunk Reset'
  }

  function formatValue(jsonStr) {
    if (!jsonStr) return '(empty)'
    try {
      const parsed = JSON.parse(jsonStr)
      if (typeof parsed === 'string') return parsed
      if (typeof parsed === 'boolean') return parsed ? 'Yes' : 'No'
      return JSON.stringify(parsed, null, 2)
    } catch {
      return jsonStr
    }
  }

  function getDescription() {
    if (log.action_type === 'stage_change') {
      return `${formatValue(log.old_value)} → ${formatValue(log.new_value)}`
    }
    if (log.action_type === 'approval') {
      const meta = log.metadata ? JSON.parse(log.metadata) : {}
      return `Approved at ${meta.stage || 'unknown stage'}`
    }
    if (log.entity_type && log.entity_id) {
      return `${log.entity_type} #${log.entity_id}: ${log.field_name || ''}`
    }
    return log.field_name || ''
  }

  const color = actionColors[log.action_type] || '#6b7280'
  const hasDetails = log.old_value || log.new_value

  // Extract document name from document_id (format: "doc-timestamp-filename")
  const docId = log.document_id || ''
  const docDisplay = docId.replace(/^doc-\d+-/, '') || docId

  return (
    <div
      style={{
        padding: '12px 16px',
        borderBottom: '1px solid #e5e7eb',
        cursor: hasDetails ? 'pointer' : 'default'
      }}
      onClick={() => hasDetails && setExpanded(!expanded)}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <span style={{
          background: color + '20',
          color: color,
          padding: '4px 10px',
          borderRadius: '4px',
          fontSize: '12px',
          fontWeight: '500',
          whiteSpace: 'nowrap'
        }}>
          {actionLabels[log.action_type] || log.action_type}
        </span>
        <button
          style={{
            background: '#f3f4f6',
            border: 'none',
            padding: '4px 8px',
            borderRadius: '4px',
            fontSize: '12px',
            color: '#4f46e5',
            cursor: 'pointer',
            textDecoration: 'underline'
          }}
          onClick={(e) => { e.stopPropagation(); onNavigate(log.workflow_id) }}
          title={`View document: ${docDisplay}`}
        >
          {docDisplay.length > 30 ? docDisplay.substring(0, 30) + '...' : docDisplay}
        </button>
        <span style={{ flex: 1, fontSize: '14px', color: '#374151' }}>
          {getDescription()}
        </span>
        <span style={{ fontSize: '12px', color: '#9ca3af', whiteSpace: 'nowrap' }}>
          {new Date(log.timestamp).toLocaleString()}
        </span>
        {hasDetails && (
          <span style={{ color: '#9ca3af', fontSize: '12px' }}>
            {expanded ? '▼' : '▶'}
          </span>
        )}
      </div>

      {expanded && hasDetails && (
        <div style={{ marginTop: '12px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <div>
            <div style={{ fontWeight: '600', marginBottom: '6px', color: '#991b1b', fontSize: '12px' }}>Before</div>
            <pre style={{
              background: '#fef2f2',
              padding: '10px',
              borderRadius: '6px',
              overflow: 'auto',
              maxHeight: '200px',
              whiteSpace: 'pre-wrap',
              fontSize: '12px',
              margin: 0,
              border: '1px solid #fecaca'
            }}>
              {formatValue(log.old_value)}
            </pre>
          </div>
          <div>
            <div style={{ fontWeight: '600', marginBottom: '6px', color: '#065f46', fontSize: '12px' }}>After</div>
            <pre style={{
              background: '#f0fdf4',
              padding: '10px',
              borderRadius: '6px',
              overflow: 'auto',
              maxHeight: '200px',
              whiteSpace: 'pre-wrap',
              fontSize: '12px',
              margin: 0,
              border: '1px solid #bbf7d0'
            }}>
              {formatValue(log.new_value)}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

export default function App() {
  return (
    <>
      <Header />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/new" element={<NewDocument />} />
        <Route path="/documents/:workflowId" element={<DocumentDetail />} />
        <Route path="/search" element={<Search />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/audit" element={<GlobalAuditLog />} />
      </Routes>
    </>
  )
}
