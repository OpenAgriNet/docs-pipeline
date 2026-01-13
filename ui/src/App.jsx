import React, { useState, useEffect, useCallback } from 'react'
import { Routes, Route, Link, useParams, useNavigate } from 'react-router-dom'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'

// Set up PDF.js worker - use cdnjs for better reliability
pdfjs.GlobalWorkerOptions.workerSrc = `//cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.mjs`

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
      'chunking': '#fef3c7', 'chunk_review': '#fce7f3', 'ready_for_ingestion': '#d1fae5',
      'ingesting': '#fef3c7', 'completed': '#d1fae5', 'failed': '#fee2e2'
    }[stage] || '#e5e7eb',
    color: {
      'registered': '#1e40af', 'ocr_processing': '#92400e', 'ocr_review': '#9d174d',
      'chunking': '#92400e', 'chunk_review': '#9d174d', 'ready_for_ingestion': '#065f46',
      'ingesting': '#92400e', 'completed': '#065f46', 'failed': '#991b1b'
    }[stage] || '#374151'
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
              <div key={doc.document_id} style={styles.card}>
                <div style={styles.flex}>
                  <span style={styles.badge(doc.stage)}>{doc.stage.replace('_', ' ')}</span>
                </div>
                <h4 style={{ margin: '12px 0' }}>{doc.filename}</h4>
                <p style={{ color: '#6b7280', fontSize: '14px' }}>
                  Pages: {doc.page_count} | Chunks: {doc.chunk_count}
                </p>
                <button
                  style={{ ...styles.button, marginTop: '12px' }}
                  onClick={() => navigate(`/documents/${doc.document_id.substring(0,12)}`)}
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
            <tr key={doc.document_id}>
              <td style={styles.td}>{doc.filename}</td>
              <td style={styles.td}><span style={styles.badge(doc.stage)}>{doc.stage.replace('_', ' ')}</span></td>
              <td style={styles.td}>{doc.page_count}</td>
              <td style={styles.td}>{doc.chunk_count}</td>
              <td style={styles.td}>
                <button
                  style={styles.buttonSecondary}
                  onClick={() => navigate(`/documents/${doc.document_id.substring(0,12)}`)}
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

function DocumentDetail() {
  const { workflowId } = useParams()
  const [doc, setDoc] = useState(null)
  const [pages, setPages] = useState([])
  const [chunks, setChunks] = useState([])
  const [activeTab, setActiveTab] = useState('pages')
  const [loading, setLoading] = useState(true)
  const [currentPdfPage, setCurrentPdfPage] = useState(1)
  const [numPages, setNumPages] = useState(null)

  const fullWorkflowId = `doc-${workflowId}`

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

  async function approveChunks() {
    await fetch(`${API_BASE}/documents/${fullWorkflowId}/approve-chunks`, { method: 'POST' })
    fetchAll()
  }

  if (loading) return <div style={styles.container}><p>Loading...</p></div>
  if (!doc) return <div style={styles.container}><p>Document not found</p></div>

  return (
    <div style={styles.wideContainer}>
      <div style={styles.card}>
        <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '20px' }}>
          <div>
            <h2>{doc.filename}</h2>
            <p style={{ color: '#6b7280', marginTop: '4px' }}>ID: {fullWorkflowId}</p>
          </div>
          <span style={styles.badge(doc.stage)}>{doc.stage?.replace('_', ' ')}</span>
        </div>

        <div style={{ ...styles.flex, marginBottom: '20px' }}>
          <div style={{ background: '#f3f4f6', padding: '12px 20px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: '600' }}>{doc.page_count}</div>
            <div style={{ fontSize: '12px', color: '#6b7280' }}>Pages</div>
          </div>
          <div style={{ background: '#f3f4f6', padding: '12px 20px', borderRadius: '8px' }}>
            <div style={{ fontSize: '24px', fontWeight: '600' }}>{doc.chunk_count}</div>
            <div style={{ fontSize: '12px', color: '#6b7280' }}>Chunks</div>
          </div>
        </div>

        {doc.stage === 'ocr_review' && (
          <button style={styles.buttonSuccess} onClick={approveOcr}>
            Approve OCR & Continue to Chunking
          </button>
        )}
        {doc.stage === 'chunk_review' && (
          <button style={styles.buttonSuccess} onClick={approveChunks}>
            Approve Chunks & Continue to Ingestion
          </button>
        )}
      </div>

      <div style={{ ...styles.flex, marginBottom: '16px' }}>
        {['pages', 'chunks', 'overview'].map(tab => (
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
            {pages.map(page => (
              <PageCard
                key={page.page_number}
                page={page}
                workflowId={fullWorkflowId}
                onUpdate={fetchAll}
                isActive={page.page_number === currentPdfPage}
                onFocus={() => setCurrentPdfPage(page.page_number)}
              />
            ))}
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
            {chunks.map(chunk => (
              <ChunkCard
                key={chunk.chunk_number}
                chunk={chunk}
                workflowId={fullWorkflowId}
                onUpdate={fetchAll}
                onPageClick={(pageNum) => setCurrentPdfPage(pageNum)}
              />
            ))}
          </div>
        </div>
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

function Search() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)

  async function handleSearch(e) {
    e.preventDefault()
    if (!query.trim()) return
    setLoading(true)
    try {
      const res = await fetch(`${MARQO_BASE}/indexes/documents-index/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ q: query, limit: 10 })
      })
      const data = await res.json()
      setResults(data.hits || [])
    } catch (e) {
      console.error('Search failed:', e)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <h2 style={{ marginBottom: '20px' }}>Search Documents</h2>
        <form onSubmit={handleSearch} style={styles.flex}>
          <input
            style={{ ...styles.input, flex: 1, marginBottom: 0 }}
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search veterinary documents..."
          />
          <button type="submit" style={styles.button} disabled={loading}>
            {loading ? 'Searching...' : 'Search'}
          </button>
        </form>
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
              <p style={{ fontSize: '14px', lineHeight: '1.6' }}>{hit.text}</p>
              <div style={{ marginTop: '12px', fontSize: '12px', color: '#6b7280' }}>
                Chunk #{hit.chunk_num} | {hit.token_count} tokens | Source: {hit.source}
              </div>
              {hit._highlights?.[0] && (
                <div style={{
                  marginTop: '12px', padding: '12px', background: '#fef3c7',
                  borderRadius: '6px', fontSize: '13px'
                }}>
                  <strong>Highlight:</strong> {hit._highlights[0].text}
                </div>
              )}
            </div>
          ))}
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
      </Routes>
    </>
  )
}
