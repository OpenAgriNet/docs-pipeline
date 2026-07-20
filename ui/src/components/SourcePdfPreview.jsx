import React from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.js?url'
import { API_BASE } from '../config'
import { appendAccessToken } from '../auth/keycloak'

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker

export default function SourcePdfPreview({ workflowId, currentPage }) {
  if (!workflowId) return null

  // Query-param token: pdf.js loads this directly and can't attach a header.
  const pdfUrl = appendAccessToken(`${API_BASE}/documents/${workflowId}/pdf`)

  return (
    <div className="flex-1 min-h-0 overflow-auto bg-white flex justify-center p-2">
      <Document
        file={pdfUrl}
        loading={<p className="p-4 text-xs text-muted-foreground">Loading PDF…</p>}
        error={
          <p className="p-4 text-xs text-destructive text-center">
            Could not load PDF preview. The source file may be missing or unavailable.
          </p>
        }
      >
        <Page
          pageNumber={currentPage}
          width={340}
          renderTextLayer={false}
          renderAnnotationLayer={false}
        />
      </Document>
    </div>
  )
}
