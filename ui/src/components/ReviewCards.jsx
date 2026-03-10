import React, { useState } from 'react'
import { API_BASE } from '../config'
import { styles } from '../styles/appStyles'

export function PageCard({ page, workflowId, onUpdate, isActive, onFocus }) {
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
    <div style={{ ...styles.card, border: isActive ? '2px solid #1d4ed8' : '2px solid transparent', cursor: 'pointer' }} onClick={() => !editing && onFocus()}>
      <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap' }}>
        <div style={styles.flex}>
          <h4 style={{ margin: 0 }}>Page {page.page_number}</h4>
          <span style={styles.pageIndicator}>PDF Page {page.page_number}</span>
        </div>
        <div style={styles.flex}>
          {page.is_reviewed && <span style={{ color: '#059669', fontSize: '14px' }}>Reviewed</span>}
          {!editing ? (
            <button style={styles.buttonSecondary} onClick={(event) => { event.stopPropagation(); setEditing(true) }}>Edit</button>
          ) : (
            <>
              <button style={styles.buttonSuccess} onClick={(event) => { event.stopPropagation(); save() }}>Save</button>
              <button style={styles.buttonSecondary} onClick={(event) => { event.stopPropagation(); setEditing(false) }}>Cancel</button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <textarea style={styles.textarea} value={markdown} onChange={event => setMarkdown(event.target.value)} onClick={event => event.stopPropagation()} />
      ) : (
        <pre style={{ background: '#f8fafc', padding: '16px', borderRadius: '10px', overflow: 'auto', maxHeight: '420px', whiteSpace: 'pre-wrap', fontSize: '13px' }}>
          {page.edited_markdown || page.original_markdown}
        </pre>
      )}
    </div>
  )
}

export function TranslationCard({ page, workflowId, onUpdate, isActive, onFocus }) {
  const [editing, setEditing] = useState(false)
  const [translation, setTranslation] = useState(page.edited_translation || page.translated_markdown || '')
  const hasTranslation = page.translated_markdown || page.edited_translation
  const detectedLang = page.detected_language || 'en'
  const langNames = { en: 'English', hi: 'Hindi', gu: 'Gujarati', mr: 'Marathi', ta: 'Tamil', te: 'Telugu', kn: 'Kannada', ml: 'Malayalam', pa: 'Punjabi', bn: 'Bengali', or: 'Odia' }

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
    <div style={{ ...styles.card, border: isActive ? '2px solid #1d4ed8' : '2px solid transparent', cursor: 'pointer' }} onClick={() => !editing && onFocus()}>
      <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap' }}>
        <div style={styles.flex}>
          <h4 style={{ margin: 0 }}>Page {page.page_number}</h4>
          <span style={{ ...styles.pageIndicator, background: detectedLang === 'en' ? '#dcfce7' : '#e0e7ff', color: detectedLang === 'en' ? '#166534' : '#3730a3' }}>
            {langNames[detectedLang] || detectedLang.toUpperCase()}
          </span>
          {page.translation_reviewed && <span style={{ color: '#059669', fontSize: '14px' }}>Reviewed</span>}
        </div>
        <div style={styles.flex}>
          {hasTranslation && !editing && <button style={styles.buttonSecondary} onClick={(event) => { event.stopPropagation(); setEditing(true) }}>Edit Translation</button>}
          {editing && (
            <>
              <button style={styles.buttonSuccess} onClick={(event) => { event.stopPropagation(); save() }}>Save</button>
              <button style={styles.buttonSecondary} onClick={(event) => { event.stopPropagation(); setEditing(false) }}>Cancel</button>
            </>
          )}
        </div>
      </div>
      {detectedLang === 'en' ? (
        <div style={{ padding: '16px', background: '#f0fdf4', borderRadius: '10px', color: '#166534' }}>This page is already in English.</div>
      ) : hasTranslation ? (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
          <div>
            <div style={{ fontSize: '12px', fontWeight: '700', color: '#64748b', marginBottom: '8px' }}>Original ({langNames[detectedLang] || detectedLang})</div>
            <pre style={{ background: '#f8fafc', padding: '12px', borderRadius: '10px', overflow: 'auto', maxHeight: '320px', whiteSpace: 'pre-wrap', fontSize: '12px' }}>
              {page.edited_markdown || page.original_markdown}
            </pre>
          </div>
          <div>
            <div style={{ fontSize: '12px', fontWeight: '700', color: '#64748b', marginBottom: '8px' }}>English Translation</div>
            {editing ? (
              <textarea style={{ ...styles.textarea, minHeight: '320px' }} value={translation} onChange={event => setTranslation(event.target.value)} onClick={event => event.stopPropagation()} />
            ) : (
              <pre style={{ background: '#eff6ff', padding: '12px', borderRadius: '10px', overflow: 'auto', maxHeight: '320px', whiteSpace: 'pre-wrap', fontSize: '12px' }}>
                {page.edited_translation || page.translated_markdown}
              </pre>
            )}
          </div>
        </div>
      ) : (
        <div style={{ padding: '16px', background: '#fffbeb', borderRadius: '10px', color: '#92400e' }}>Translation pending...</div>
      )}
    </div>
  )
}

export function ChunkCard({ chunk, workflowId, onUpdate, onPageClick }) {
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
    <div style={{ ...styles.card, opacity: chunk.is_excluded ? 0.58 : 1 }}>
      <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap' }}>
        <div style={styles.flex}>
          <h4 style={{ margin: 0 }}>Chunk {chunk.chunk_number}</h4>
          <span style={{ fontSize: '12px', color: '#64748b' }}>{chunk.token_count} tokens</span>
          <button style={{ ...styles.pageIndicator, cursor: 'pointer', background: '#059669' }} onClick={() => onPageClick(pageStart)} title={`Go to ${pageRange}`}>
            {pageRange}
          </button>
        </div>
        <div style={styles.flex}>
          {chunk.is_reviewed && <span style={{ color: '#059669', fontSize: '14px' }}>Reviewed</span>}
          <button style={{ ...styles.buttonSecondary, background: chunk.is_excluded ? '#fee2e2' : '#e2e8f0' }} onClick={toggleExclude}>
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
        <textarea style={styles.textarea} value={text} onChange={event => setText(event.target.value)} />
      ) : (
        <pre style={{ background: '#f8fafc', padding: '16px', borderRadius: '10px', overflow: 'auto', maxHeight: '320px', whiteSpace: 'pre-wrap', fontSize: '13px' }}>
          {chunk.edited_text || chunk.original_text}
        </pre>
      )}
    </div>
  )
}
