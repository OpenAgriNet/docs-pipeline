import React, { useMemo } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import { API_BASE } from '../config'
import { authHeaders, getCurrentToken } from '../auth/keycloak'

pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`

function buildAuthedPdfFile(workflowId) {
  if (!workflowId) return null
  const url = `${API_BASE}/documents/${workflowId}/pdf`
  const headers = authHeaders()
  const token = getCurrentToken()
  if (headers.Authorization) {
    return { url, httpHeaders: headers, withCredentials: false }
  }
  if (token) {
    return {
      url,
      httpHeaders: { Authorization: `Bearer ${token}` },
      withCredentials: false,
    }
  }
  return url
}

export default function SourcePdfPreview({ workflowId, currentPage }) {
  const file = useMemo(() => buildAuthedPdfFile(workflowId), [workflowId])

  if (!workflowId || !file) return null

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col bg-muted/40">
      <div className="min-h-0 flex-1 overflow-auto overscroll-contain">
        <div className="flex min-h-full justify-center p-3">
          <Document
            file={file}
            loading={
              <div className="flex h-48 w-full items-center justify-center">
                <p className="text-xs text-muted-foreground">Loading PDF…</p>
              </div>
            }
            error={
              <div className="flex h-48 max-w-[280px] items-center justify-center px-4 text-center">
                <p className="text-xs text-destructive">
                  Could not load PDF preview. The source file may be missing or unavailable.
                </p>
              </div>
            }
            className="shadow-sm"
          >
            <Page
              pageNumber={currentPage || 1}
              width={340}
              renderTextLayer={false}
              renderAnnotationLayer={false}
              className="overflow-hidden rounded-md border border-border bg-white shadow-sm"
            />
          </Document>
        </div>
      </div>
    </div>
  )
}
