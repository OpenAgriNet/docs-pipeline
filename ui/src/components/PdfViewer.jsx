import React, { useState } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import { API_BASE } from '../config'
import { styles } from '../styles/appStyles'

pdfjs.GlobalWorkerOptions.workerSrc = `//cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`

export default function PdfViewer({ workflowId, currentPage, onPageChange, numPages, setNumPages }) {
  const [scale, setScale] = useState(1.0)
  const pdfUrl = `${API_BASE}/documents/${workflowId}/pdf`

  function onDocumentLoadSuccess({ numPages: loadedPageCount }) {
    setNumPages(loadedPageCount)
  }

  return (
    <div style={styles.pdfContainer}>
      <div style={styles.pdfControls}>
        <button
          style={{ ...styles.buttonSmall, background: '#e2e8f0', color: '#334155' }}
          onClick={() => onPageChange(Math.max(1, currentPage - 1))}
          disabled={currentPage <= 1}
        >
          Prev
        </button>
        <span style={{ fontSize: '14px', fontWeight: 600 }}>
          Page {currentPage} of {numPages || '?'}
        </span>
        <button
          style={{ ...styles.buttonSmall, background: '#e2e8f0', color: '#334155' }}
          onClick={() => onPageChange(Math.min(numPages || currentPage, currentPage + 1))}
          disabled={currentPage >= numPages}
        >
          Next
        </button>
        <span style={{ margin: '0 8px', color: '#94a3b8' }}>|</span>
        <button
          style={{ ...styles.buttonSmall, background: '#e2e8f0', color: '#334155' }}
          onClick={() => setScale(value => Math.max(0.5, value - 0.1))}
        >
          -
        </button>
        <span style={{ fontSize: '12px', minWidth: '44px', textAlign: 'center' }}>
          {Math.round(scale * 100)}%
        </span>
        <button
          style={{ ...styles.buttonSmall, background: '#e2e8f0', color: '#334155' }}
          onClick={() => setScale(value => Math.min(2, value + 0.1))}
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
        <Page pageNumber={currentPage} scale={scale} renderTextLayer renderAnnotationLayer />
      </Document>
    </div>
  )
}
