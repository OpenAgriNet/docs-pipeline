import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../config'
import { styles } from '../styles/appStyles'

const ACCEPTED_EXTENSIONS = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.csv', '.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff']
const ACCEPTED_FILE_TYPES = ACCEPTED_EXTENSIONS.join(',')

export default function NewDocumentView() {
  const [file, setFile] = useState(null)
  const [autoApprove, setAutoApprove] = useState(false)
  const [loading, setLoading] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const navigate = useNavigate()

  async function handleSubmit(event) {
    event.preventDefault()
    if (!file) {
      alert('Please select a supported document file')
      return
    }

    setLoading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const response = await fetch(`${API_BASE}/upload?auto_approve=${autoApprove}`, { method: 'POST', body: formData })
      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to upload and start workflow')
      }
      navigate('/')
    } catch (error) {
      alert(`Failed to upload: ${error.message}`)
    } finally {
      setLoading(false)
    }
  }

  function handleDrag(event) {
    event.preventDefault()
    event.stopPropagation()
    if (event.type === 'dragenter' || event.type === 'dragover') setDragActive(true)
    if (event.type === 'dragleave') setDragActive(false)
  }

  function handleDrop(event) {
    event.preventDefault()
    event.stopPropagation()
    setDragActive(false)
    if (event.dataTransfer.files && event.dataTransfer.files[0]) {
      const droppedFile = event.dataTransfer.files[0]
      const lowerName = droppedFile.name.toLowerCase()
      if (ACCEPTED_EXTENSIONS.some(extension => lowerName.endsWith(extension))) setFile(droppedFile)
      else alert('Unsupported file type')
    }
  }

  return (
    <div style={styles.container}>
      <section style={styles.pageHero}>
        <h2 style={styles.pageHeroTitle}>Ingest a new document</h2>
        <p style={styles.pageHeroText}>Start a new Temporal pipeline run from the operator console. Original uploads and normalized artifacts will persist into MinIO-backed storage as the workflow advances.</p>
      </section>
      <div style={styles.card}>
        <form onSubmit={handleSubmit}>
          <div
            style={{
              border: `2px dashed ${dragActive ? '#1d4ed8' : '#cbd5e1'}`,
              borderRadius: '18px',
              padding: '52px 20px',
              textAlign: 'center',
              marginBottom: '20px',
              background: dragActive ? '#eff6ff' : '#f8fafc',
              cursor: 'pointer',
              transition: 'all 0.2s ease'
            }}
            onDragEnter={handleDrag}
            onDragLeave={handleDrag}
            onDragOver={handleDrag}
            onDrop={handleDrop}
            onClick={() => document.getElementById('fileInput')?.click()}
          >
            <input
              id="fileInput"
              type="file"
              accept={ACCEPTED_FILE_TYPES}
              style={{ display: 'none' }}
              onChange={(event) => setFile(event.target.files?.[0] || null)}
            />
            {file ? (
              <div>
                <div style={{ fontSize: '48px', marginBottom: '12px' }}>{file.name.split('.').pop()?.toUpperCase() || 'FILE'}</div>
                <div style={{ fontWeight: 700 }}>{file.name}</div>
                <div style={{ fontSize: '14px', color: '#64748b' }}>{(file.size / 1024 / 1024).toFixed(2)} MB</div>
              </div>
            ) : (
              <div>
                <div style={{ fontSize: '48px', marginBottom: '12px', opacity: 0.5 }}>+</div>
                <div style={{ fontWeight: 700 }}>Drop a document here or click to select</div>
                <div style={{ fontSize: '14px', color: '#64748b', marginTop: '8px' }}>Supported: PDF, Office docs, spreadsheets, and common image formats.</div>
              </div>
            )}
          </div>

          <label style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '20px' }}>
            <input type="checkbox" checked={autoApprove} onChange={event => setAutoApprove(event.target.checked)} />
            Auto-approve review stages
          </label>
          <div style={styles.flex}>
            <button type="submit" style={styles.button} disabled={loading || !file}>{loading ? 'Uploading...' : 'Upload & Start Pipeline'}</button>
            <button type="button" style={styles.buttonSecondary} onClick={() => navigate('/')}>Cancel</button>
          </div>
        </form>
      </div>
    </div>
  )
}
