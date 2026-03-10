import React, { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { API_BASE } from '../config'
import { styles } from '../styles/appStyles'
import PdfViewer from '../components/PdfViewer'
import PipelineStepper from '../components/PipelineStepper'
import { DocumentAuditLog } from '../components/AuditPanels'
import { ChunkCard, PageCard, TranslationCard } from '../components/ReviewCards'

function SidePanel({ doc, jobs, activeTab, setActiveTab, translatedCount, chunkCount, marqoCount }) {
  return (
    <div style={styles.sideStack}>
      <div style={styles.card}>
        <h3 style={{ marginTop: 0 }}>Document status</h3>
        <div style={{ display: 'grid', gap: '10px' }}>
          <div><strong>Stage:</strong> <span style={styles.badge(doc.stage)}>{doc.stage?.replace(/_/g, ' ')}</span></div>
          <div><strong>Pages:</strong> {doc.page_count}</div>
          <div><strong>Translated pages:</strong> {translatedCount}</div>
          <div><strong>Chunks:</strong> {chunkCount}</div>
          <div><strong>Indexed chunks:</strong> {marqoCount}</div>
        </div>
      </div>

      <div style={styles.card}>
        <h3 style={{ marginTop: 0 }}>Views</h3>
        <div style={styles.tabs}>
          {['overview', 'pages', 'translations', 'chunks', 'artifacts', 'marqo', 'history'].map(tab => (
            <button key={tab} style={styles.tabButton(activeTab === tab)} onClick={() => setActiveTab(tab)}>
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
            </button>
          ))}
        </div>
      </div>

      <div style={styles.card}>
        <h3 style={{ marginTop: 0 }}>Job history</h3>
        <div style={{ display: 'grid', gap: '10px' }}>
          {jobs.length === 0 && <p style={{ margin: 0, color: '#64748b' }}>No jobs recorded yet.</p>}
          {jobs.map(job => (
            <div key={job.id} style={styles.panelMuted}>
              <strong>{job.job_type}</strong>
              <div style={{ fontSize: '13px', color: '#334155', marginTop: '4px' }}>{job.status} · {job.current_stage || 'n/a'}</div>
              <div style={{ fontSize: '12px', color: '#64748b', marginTop: '4px' }}>{new Date(job.started_at).toLocaleString()}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function ArtifactPanel({ doc, workflowId }) {
  return (
    <div style={styles.card}>
      <h3 style={{ marginTop: 0 }}>Stage artifacts</h3>
      <p style={{ color: '#64748b' }}>Original uploads, normalized sources, JSON exports, and Marqo payloads are linked here.</p>
      <div style={{ display: 'grid', gap: '12px' }}>
        {(doc.artifacts || []).map(artifact => (
          <div key={artifact.id} style={{ padding: '16px', borderRadius: '14px', background: '#f8fafc' }}>
            <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '8px', flexWrap: 'wrap' }}>
              <div>
                <strong>{artifact.artifact_type}</strong>
                <div style={{ fontSize: '12px', color: '#64748b' }}>{artifact.stage || 'n/a'} · {artifact.filename}</div>
              </div>
              <a href={`${API_BASE}/documents/${workflowId}/artifacts/${artifact.id}/content`} target="_blank" rel="noreferrer" style={{ ...styles.buttonSecondary, textDecoration: 'none' }}>
                Open
              </a>
            </div>
            <div style={{ fontSize: '12px', color: '#64748b' }}>{artifact.storage_uri}</div>
          </div>
        ))}
        {(doc.artifacts || []).length === 0 && <p style={{ color: '#64748b' }}>No persisted artifacts yet.</p>}
      </div>
    </div>
  )
}

function MarqoPanel({ doc, marqoChunks }) {
  return (
    <div style={styles.card}>
      <h3 style={{ marginTop: 0 }}>Marqo index view</h3>
      <div style={{ ...styles.flex, marginBottom: '16px', flexWrap: 'wrap' }}>
        {(doc.index_status || []).map(status => (
          <div key={status.index_name} style={{ padding: '12px 16px', borderRadius: '12px', background: '#eff6ff' }}>
            <strong>{status.index_name}</strong>
            <div style={{ fontSize: '12px', color: '#1d4ed8' }}>{status.status} · {status.chunk_count_indexed} chunks</div>
          </div>
        ))}
      </div>
      <div style={{ display: 'grid', gap: '12px' }}>
        {marqoChunks.map((hit, index) => (
          <div key={hit._id || index} style={{ padding: '16px', borderRadius: '14px', background: '#f8fafc' }}>
            <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '8px', flexWrap: 'wrap' }}>
              <strong>{hit.filename || doc.filename}</strong>
              <span style={{ fontSize: '12px', color: '#64748b' }}>Chunk {hit.chunk_num}</span>
            </div>
            <div style={{ fontSize: '12px', color: '#64748b', marginBottom: '8px' }}>Pages {hit.page_start} - {hit.page_end} · {hit.token_count} tokens</div>
            <pre style={{ background: '#fff', padding: '12px', borderRadius: '10px', overflow: 'auto', whiteSpace: 'pre-wrap', maxHeight: '240px' }}>{hit.text}</pre>
          </div>
        ))}
        {marqoChunks.length === 0 && <p style={{ color: '#64748b' }}>No indexed chunks found for this document.</p>}
      </div>
    </div>
  )
}

export default function DocumentOpsView() {
  const { workflowId } = useParams()
  const [doc, setDoc] = useState(null)
  const [pages, setPages] = useState([])
  const [chunks, setChunks] = useState([])
  const [marqoChunks, setMarqoChunks] = useState([])
  const [jobs, setJobs] = useState([])
  const [activeTab, setActiveTab] = useState('overview')
  const [loading, setLoading] = useState(true)
  const [currentPdfPage, setCurrentPdfPage] = useState(1)
  const [numPages, setNumPages] = useState(null)
  const [reingesting, setReingesting] = useState(false)
  const [actionMessage, setActionMessage] = useState('')

  useEffect(() => {
    fetchAll()
    const interval = setInterval(fetchAll, 5000)
    return () => clearInterval(interval)
  }, [workflowId])

  async function fetchAll() {
    try {
      const [docRes, pagesRes, chunksRes, marqoRes, jobsRes] = await Promise.all([
        fetch(`${API_BASE}/documents/${workflowId}`),
        fetch(`${API_BASE}/documents/${workflowId}/pages`),
        fetch(`${API_BASE}/documents/${workflowId}/chunks?include_excluded=true`),
        fetch(`${API_BASE}/documents/${workflowId}/marqo/chunks`),
        fetch(`${API_BASE}/documents/${workflowId}/jobs`)
      ])
      if (docRes.ok) setDoc(await docRes.json())
      if (pagesRes.ok) setPages(await pagesRes.json())
      if (chunksRes.ok) setChunks(await chunksRes.json())
      if (marqoRes.ok) setMarqoChunks(await marqoRes.json())
      if (jobsRes.ok) setJobs(await jobsRes.json())
    } catch (error) {
      console.error('Failed to fetch document state:', error)
    } finally {
      setLoading(false)
    }
  }

  async function approve(stagePath) {
    await fetch(`${API_BASE}/documents/${workflowId}/${stagePath}`, { method: 'POST' })
    setActionMessage(`Triggered ${stagePath.replace('approve-', '')} approval.`)
    fetchAll()
  }

  async function reingestDocument() {
    setReingesting(true)
    setActionMessage('')
    try {
      const response = await fetch(`${API_BASE}/documents/${workflowId}/reingest`, { method: 'POST' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Failed to start reingestion')
      setActionMessage(`Reingestion started for ${data.filename || doc.filename}.`)
      fetchAll()
    } catch (error) {
      setActionMessage(error.message)
    } finally {
      setReingesting(false)
    }
  }

  if (loading) return <div style={styles.container}><p>Loading...</p></div>
  if (!doc) return <div style={styles.container}><p>Document not found</p></div>

  const translatedCount = pages.filter(page => page.translated_markdown).length

  return (
    <div style={styles.wideContainer}>
      <section style={styles.pageHero}>
        <h2 style={styles.pageHeroTitle}>{doc.filename}</h2>
        <p style={styles.pageHeroText}>Inspect pipeline stage inputs and outputs, review content edits, access persisted artifacts, and compare the stored document against its Marqo index footprint.</p>
        <div style={styles.pageHeroMeta}>
          <span style={styles.metaPill}>Workflow {workflowId}</span>
          <span style={styles.metaPill}>{doc.page_count} pages</span>
          <span style={styles.metaPill}>{doc.chunk_count} chunks</span>
          <span style={styles.metaPill}>{(doc.artifacts || []).length} artifacts</span>
        </div>
      </section>

      <div style={styles.card}>
        <PipelineStepper currentStage={doc.stage} hasPages={doc.page_count > 0} hasChunks={doc.chunk_count > 0} />
      </div>

      {doc.stage === 'failed' && doc.error_message && (
        <div style={{ ...styles.card, background: '#fef2f2', border: '1px solid #fecaca' }}>
          <h3 style={{ color: '#991b1b', marginTop: 0 }}>Pipeline failed</h3>
          <p style={{ color: '#b91c1c', margin: 0, fontFamily: 'monospace', fontSize: '13px' }}>{doc.error_message}</p>
        </div>
      )}

      <div style={styles.opsLayout}>
        <SidePanel
          doc={doc}
          jobs={jobs}
          activeTab={activeTab}
          setActiveTab={setActiveTab}
          translatedCount={translatedCount}
          chunkCount={chunks.length}
          marqoCount={marqoChunks.length}
        />

        <div>
          <div style={{ ...styles.card, marginBottom: '16px' }}>
            <div style={{ ...styles.flex, justifyContent: 'space-between', flexWrap: 'wrap' }}>
              <div>
                <h3 style={{ margin: '0 0 6px' }}>Available actions</h3>
                <p style={{ margin: 0, color: '#64748b' }}>Stage approvals remain here; direct content edits happen inside the focused views below.</p>
              </div>
              <div style={{ ...styles.flex, flexWrap: 'wrap' }}>
                <button style={styles.buttonSecondary} onClick={reingestDocument} disabled={reingesting}>
                  {reingesting ? 'Starting reingest...' : 'Reingest to Marqo'}
                </button>
                {doc.stage === 'ocr_review' && <button style={styles.buttonSuccess} onClick={() => approve('approve-ocr')}>Approve OCR</button>}
                {doc.stage === 'translation_review' && <button style={styles.buttonSuccess} onClick={() => approve('approve-translation')}>Approve translation</button>}
                {doc.stage === 'chunk_review' && <button style={styles.buttonSuccess} onClick={() => approve('approve-chunks')}>Approve chunks</button>}
                {doc.stage === 'ready_for_ingestion' && <button style={styles.buttonSuccess} onClick={() => approve('approve-ingestion')}>Approve ingestion</button>}
              </div>
            </div>
            {actionMessage && <div style={{ marginTop: '12px', color: '#334155', fontSize: '14px' }}>{actionMessage}</div>}
          </div>

          {activeTab === 'overview' && (
            <div style={styles.card}>
              <h3 style={{ marginTop: 0 }}>Stage timeline</h3>
              <div style={{ display: 'grid', gap: '12px' }}>
                {doc.created_at && <div>Created: {new Date(doc.created_at).toLocaleString()}</div>}
                {doc.ocr_completed_at && <div>OCR completed: {new Date(doc.ocr_completed_at).toLocaleString()}</div>}
                {doc.translation_completed_at && <div>Translation completed: {new Date(doc.translation_completed_at).toLocaleString()}</div>}
                {doc.chunks_completed_at && <div>Chunking completed: {new Date(doc.chunks_completed_at).toLocaleString()}</div>}
                {doc.ingested_at && <div>Ingested: {new Date(doc.ingested_at).toLocaleString()}</div>}
              </div>
              {doc.error_message && <div style={{ marginTop: '20px', padding: '12px', background: '#fee2e2', borderRadius: '10px', color: '#991b1b' }}>Error: {doc.error_message}</div>}
            </div>
          )}

          {activeTab === 'pages' && (
            <div style={styles.splitPane}>
              <PdfViewer workflowId={workflowId} currentPage={currentPdfPage} onPageChange={setCurrentPdfPage} numPages={numPages} setNumPages={setNumPages} />
              <div>
                {pages.length === 0 ? (
                  <div style={{ ...styles.card, textAlign: 'center', color: '#64748b', padding: '40px' }}>No page data available for this document.</div>
                ) : (
                  pages.map(page => (
                    <PageCard key={page.page_number} page={page} workflowId={workflowId} onUpdate={fetchAll} isActive={page.page_number === currentPdfPage} onFocus={() => setCurrentPdfPage(page.page_number)} />
                  ))
                )}
              </div>
            </div>
          )}

          {activeTab === 'translations' && (
            <div style={styles.splitPane}>
              <PdfViewer workflowId={workflowId} currentPage={currentPdfPage} onPageChange={setCurrentPdfPage} numPages={numPages} setNumPages={setNumPages} />
              <div>
                {pages.length === 0 ? (
                  <div style={{ ...styles.card, textAlign: 'center', color: '#64748b', padding: '40px' }}>No translation data available for this document.</div>
                ) : (
                  pages.map(page => (
                    <TranslationCard key={page.page_number} page={page} workflowId={workflowId} onUpdate={fetchAll} isActive={page.page_number === currentPdfPage} onFocus={() => setCurrentPdfPage(page.page_number)} />
                  ))
                )}
              </div>
            </div>
          )}

          {activeTab === 'chunks' && (
            <div style={styles.splitPane}>
              <PdfViewer workflowId={workflowId} currentPage={currentPdfPage} onPageChange={setCurrentPdfPage} numPages={numPages} setNumPages={setNumPages} />
              <div>
                {chunks.length === 0 ? (
                  <div style={{ ...styles.card, textAlign: 'center', color: '#64748b', padding: '40px' }}>No chunk data available for this document.</div>
                ) : (
                  chunks.map(chunk => (
                    <ChunkCard key={chunk.chunk_number} chunk={chunk} workflowId={workflowId} onUpdate={fetchAll} onPageClick={setCurrentPdfPage} />
                  ))
                )}
              </div>
            </div>
          )}

          {activeTab === 'artifacts' && <ArtifactPanel doc={doc} workflowId={workflowId} />}
          {activeTab === 'marqo' && <MarqoPanel doc={doc} marqoChunks={marqoChunks} />}
          {activeTab === 'history' && <DocumentAuditLog workflowId={workflowId} />}
        </div>
      </div>
    </div>
  )
}
