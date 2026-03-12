import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../config'
import { styles } from '../styles/appStyles'

function Hero({ documents }) {
  const reviewQueue = documents.filter(doc => ['ocr_review', 'translation_review', 'chunk_review'].includes(doc.stage)).length
  const completed = documents.filter(doc => doc.stage === 'completed').length
  const failed = documents.filter(doc => doc.stage === 'failed').length

  return (
    <>
      <section style={styles.pageHero}>
        <h2 style={styles.pageHeroTitle}>Pipeline operations dashboard</h2>
        <p style={styles.pageHeroText}>Use this surface to triage review queues, inspect SQLite-backed document state, and jump into document-level OCR, translation, chunk, artifact, Temporal runtime, and Marqo index state.</p>
        <div style={styles.pageHeroMeta}>
          <span style={styles.metaPill}>{documents.length} SQLite-tracked documents</span>
          <span style={styles.metaPill}>{reviewQueue} awaiting review</span>
          <span style={styles.metaPill}>{completed} completed</span>
          <span style={styles.metaPill}>{failed} failed</span>
        </div>
      </section>
      <section style={{ ...styles.summaryGrid, marginBottom: '20px' }}>
        <div style={styles.statCard}>
          <div style={styles.statValue}>{documents.length}</div>
          <div style={styles.statLabel}>Total Documents</div>
        </div>
        <div style={styles.statCard}>
          <div style={styles.statValue}>{reviewQueue}</div>
          <div style={styles.statLabel}>Review Queue</div>
        </div>
        <div style={styles.statCard}>
          <div style={styles.statValue}>{completed}</div>
          <div style={styles.statLabel}>Completed</div>
        </div>
        <div style={styles.statCard}>
          <div style={styles.statValue}>{failed}</div>
          <div style={styles.statLabel}>Failed</div>
        </div>
      </section>
    </>
  )
}

function getDocumentLabel(doc) {
  return doc.display_name || doc.filename || doc.workflow_id
}

export default function DashboardView() {
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
      const response = await fetch(`${API_BASE}/documents?limit=500`)
      const data = await response.json()
      setDocuments(data)
    } catch (error) {
      console.error('Failed to fetch documents:', error)
    } finally {
      setLoading(false)
    }
  }

  if (loading) return <div style={styles.container}><p>Loading...</p></div>

  const reviewStages = ['ocr_review', 'translation_review', 'chunk_review']
  const groupedReviews = reviewStages.reduce((accumulator, stage) => {
    accumulator[stage] = documents.filter(doc => doc.stage === stage)
    return accumulator
  }, {})

  return (
    <div style={styles.container}>
      <Hero documents={documents} />

      <div style={{ ...styles.flex, marginBottom: '24px', justifyContent: 'space-between', flexWrap: 'wrap' }}>
        <div>
          <h3 style={{ margin: 0 }}>Review priority</h3>
          <p style={{ margin: '6px 0 0', color: '#64748b' }}>Documents blocked on human review are grouped first.</p>
        </div>
        <button style={styles.button} onClick={() => navigate('/new')}>+ New Document</button>
      </div>

      {reviewStages.map(stage => groupedReviews[stage]?.length > 0 && (
        <section key={stage} style={{ marginBottom: '28px' }}>
          <h3 style={{ marginBottom: '14px', color: '#7e22ce' }}>
            {stage.replace(/_/g, ' ')} ({groupedReviews[stage].length})
          </h3>
          <div style={styles.grid}>
            {groupedReviews[stage].map(doc => (
              <div key={doc.workflow_id} style={styles.card}>
                <div style={styles.flex}>
                  <span style={styles.badge(doc.stage)}>{doc.stage.replace(/_/g, ' ')}</span>
                </div>
                <h4 style={{ margin: '12px 0 8px' }}>{getDocumentLabel(doc)}</h4>
                <p style={{ color: '#64748b', fontSize: '14px', margin: 0 }}>
                  Pages: {doc.page_count} · Chunks: {doc.chunk_count}
                </p>
                <button style={{ ...styles.button, marginTop: '14px' }} onClick={() => navigate(`/documents/${doc.workflow_id}`)}>
                  Open document
                </button>
              </div>
            ))}
          </div>
        </section>
      ))}

      <section style={styles.card}>
        <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap' }}>
          <div>
            <h3 style={{ margin: 0 }}>All documents</h3>
            <p style={{ margin: '6px 0 0', color: '#64748b' }}>Operational list across pipeline stages.</p>
          </div>
        </div>
        <div style={{ overflowX: 'auto' }}>
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
                  <td style={styles.td}>{getDocumentLabel(doc)}</td>
                  <td style={styles.td}><span style={styles.badge(doc.stage)}>{doc.stage.replace(/_/g, ' ')}</span></td>
                  <td style={styles.td}>{doc.page_count}</td>
                  <td style={styles.td}>{doc.chunk_count}</td>
                  <td style={styles.td}>
                    <button style={styles.buttonSecondary} onClick={() => navigate(`/documents/${doc.workflow_id}`)}>Inspect</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
