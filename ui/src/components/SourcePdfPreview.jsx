import React, { useEffect, useMemo, useRef, useState } from 'react'
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
  const containerRef = useRef(null)
  const [pageWidth, setPageWidth] = useState(240)

  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return undefined

    const update = () => {
      const next = Math.floor((el.clientWidth || 260) - 20)
      setPageWidth(Math.max(180, Math.min(next, 360)))
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  if (!workflowId || !file) return null

  return (
    <div ref={containerRef} className="flex h-full min-h-0 w-full flex-col bg-muted/30">
      <div className="flex min-h-0 flex-1 justify-center overflow-auto p-2">
        <Document
          file={file}
          loading={
            <div className="flex h-40 w-full items-center justify-center">
              <p className="text-xs text-muted-foreground">Loading PDF…</p>
            </div>
          }
          error={
            <div className="flex h-40 max-w-[220px] items-center justify-center px-3 text-center">
              <p className="text-xs text-destructive">
                Could not load PDF preview. The source file may be missing or unavailable.
              </p>
            </div>
          }
          className="shadow-sm"
        >
          <Page
            pageNumber={currentPage || 1}
            width={pageWidth}
            renderTextLayer={false}
            renderAnnotationLayer={false}
            className="overflow-hidden rounded-md border border-border bg-white shadow-sm"
          />
        </Document>
      </div>
    </div>
  )
}
