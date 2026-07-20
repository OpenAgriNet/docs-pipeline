import React, { useState } from 'react'
import { ChevronLeft, ChevronRight, Minus, Plus } from 'lucide-react'
import { Document, Page, pdfjs } from 'react-pdf'
import { API_BASE } from '../config'
import { appendAccessToken } from '../auth/keycloak'
import { NoticeCard } from './NoticeCard'
import { Button } from './ui/button'
import { Card, CardContent, CardHeader, CardTitle } from './ui/card'

pdfjs.GlobalWorkerOptions.workerSrc = `//cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`

export default function PdfViewer({ workflowId, currentPage, onPageChange, numPages, setNumPages }) {
  const [scale, setScale] = useState(1.0)
  // Loaded as an <embed>-style element by pdf.js, so it can't send a header —
  // pass the token as a query param instead.
  const pdfUrl = appendAccessToken(`${API_BASE}/documents/${workflowId}/pdf`)

  function onDocumentLoadSuccess({ numPages: loadedPageCount }) {
    setNumPages(loadedPageCount)
  }

  return (
    <Card className="sticky top-20 overflow-hidden shadow-sm">
      <CardHeader className="border-b border-border/80 pb-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="font-sans text-lg">Source PDF</CardTitle>
            <p className="mt-2 text-sm text-muted-foreground">Keep page context pinned while reviewing edits and chunks.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" size="sm" className="rounded-lg" onClick={() => onPageChange(Math.max(1, currentPage - 1))} disabled={currentPage <= 1}>
              <ChevronLeft className="h-4 w-4" />
              Prev
            </Button>
            <div className="min-w-[110px] text-center text-sm font-medium text-foreground">
              Page {currentPage} of {numPages || '?'}
            </div>
            <Button variant="secondary" size="sm" className="rounded-lg" onClick={() => onPageChange(Math.min(numPages || currentPage, currentPage + 1))} disabled={currentPage >= numPages}>
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
            <div className="mx-1 h-6 w-px bg-border" />
            <Button variant="ghost" size="icon" className="h-8 w-8 rounded-lg" onClick={() => setScale(value => Math.max(0.5, value - 0.1))}>
              <Minus className="h-4 w-4" />
            </Button>
            <div className="min-w-[50px] text-center text-xs text-muted-foreground">{Math.round(scale * 100)}%</div>
            <Button variant="ghost" size="icon" className="h-8 w-8 rounded-lg" onClick={() => setScale(value => Math.min(2, value + 0.1))}>
              <Plus className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="max-h-[calc(100vh-11rem)] overflow-auto bg-muted/30 p-4">
        <div className="flex justify-center">
          <Document
            file={pdfUrl}
            onLoadSuccess={onDocumentLoadSuccess}
            loading={
              <div className="w-full max-w-md py-12">
                <NoticeCard title="Loading PDF" detail="Fetching the source document for in-context review." className="rounded-2xl" />
              </div>
            }
            error={
              <div className="w-full max-w-md py-12">
                <NoticeCard title="Failed to load PDF" detail="The source preview is temporarily unavailable." tone="destructive" className="rounded-2xl" />
              </div>
            }
          >
            <Page pageNumber={currentPage} scale={scale} renderTextLayer renderAnnotationLayer />
          </Document>
        </div>
      </CardContent>
    </Card>
  )
}
