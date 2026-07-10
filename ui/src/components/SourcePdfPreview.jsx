import React from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.js?url'
import { API_BASE } from '../config'

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker

export default function SourcePdfPreview({ workflowId, currentPage }) {
  if (!workflowId) return null

  return (
    <div className="flex-1 min-h-0 overflow-auto bg-white flex justify-center p-2">
      <Document
        file={`${API_BASE}/documents/${workflowId}/pdf`}
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
