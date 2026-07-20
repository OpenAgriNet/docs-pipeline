import React, { useState } from 'react'
import { RotateCcw, Save, SquareArrowOutUpRight, WandSparkles } from 'lucide-react'
import { API_BASE } from '../config'
import { apiFetch } from '../auth/keycloak'
import { NoticeCard } from './NoticeCard'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Card, CardContent, CardHeader, CardTitle } from './ui/card'
import { Textarea } from './ui/textarea'

function ReviewStatusBadge({ reviewed }) {
  return reviewed ? (
    <Badge variant="success" className="rounded-full px-3 py-1">Reviewed</Badge>
  ) : (
    <Badge variant="secondary" className="rounded-full px-3 py-1">Pending</Badge>
  )
}

async function requestJson(path, options = {}) {
  const response = await apiFetch(`${API_BASE}${path}`, options)
  const isJson = response.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await response.json() : null
  if (!response.ok) {
    throw new Error(data?.detail || `Request failed with ${response.status}`)
  }
  return data
}

export function PageCard({ page, workflowId, onUpdate, isActive, onFocus, reindexRequired = false }) {
  const [editing, setEditing] = useState(false)
  const [markdown, setMarkdown] = useState(page.edited_markdown || page.original_markdown)
  const isEdited = Boolean(page.edited_markdown && page.edited_markdown !== page.original_markdown)

  async function save() {
    await requestJson(`/documents/${workflowId}/pages/${page.page_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_markdown: markdown, is_reviewed: true })
    })
    setEditing(false)
    onUpdate()
  }

  async function resetPage(event) {
    event.stopPropagation()
    await requestJson(`/documents/${workflowId}/pages/${page.page_number}/reset`, { method: 'POST' })
    setEditing(false)
    onUpdate()
  }

  return (
    <Card
      className={`cursor-pointer shadow-sm transition-colors ${isActive ? 'border-primary/40 bg-primary/5' : 'bg-background/70'}`}
      onClick={() => !editing && onFocus()}
    >
      <CardHeader className="pb-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="font-sans text-lg">Page {page.page_number}</CardTitle>
              <Badge variant="secondary" className="rounded-full px-3 py-1">PDF Page {page.page_number}</Badge>
              <ReviewStatusBadge reviewed={page.is_reviewed} />
            </div>
            <p className="text-sm text-muted-foreground">Review OCR output in place, then persist authoritative edits.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {!editing ? (
              <>
                <Button variant="secondary" size="sm" className="rounded-lg" onClick={event => { event.stopPropagation(); setEditing(true) }}>
                  <WandSparkles className="h-4 w-4" />
                  Edit
                </Button>
                <Button variant="ghost" size="sm" className="rounded-lg" onClick={resetPage}>
                  <RotateCcw className="h-4 w-4" />
                  Reset
                </Button>
              </>
            ) : (
              <>
                <Button size="sm" className="rounded-lg" onClick={event => { event.stopPropagation(); save() }}>
                  <Save className="h-4 w-4" />
                  Save
                </Button>
                <Button variant="secondary" size="sm" className="rounded-lg" onClick={event => { event.stopPropagation(); setEditing(false) }}>
                  Cancel
                </Button>
              </>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {(isEdited || reindexRequired) ? (
          <NoticeCard
            title={reindexRequired ? 'Search is currently stale for this document.' : 'This page has been manually edited from the original OCR output.'}
            detail={reindexRequired ? 'Reindex after review is complete.' : undefined}
            tone="warning"
            className="rounded-2xl"
          />
        ) : null}
        {editing ? (
          <Textarea
            className="min-h-[320px] rounded-2xl bg-card font-mono text-xs leading-6"
            value={markdown}
            onChange={event => setMarkdown(event.target.value)}
            onClick={event => event.stopPropagation()}
          />
        ) : (
          <Card className="rounded-2xl shadow-none">
            <CardContent className="max-h-[420px] overflow-auto whitespace-pre-wrap p-4 font-mono text-xs leading-6 text-foreground">
              {page.edited_markdown || page.original_markdown}
            </CardContent>
          </Card>
        )}
      </CardContent>
    </Card>
  )
}

export function TranslationCard({ page, workflowId, onUpdate, isActive, onFocus, reindexRequired = false }) {
  const [editing, setEditing] = useState(false)
  const [translation, setTranslation] = useState(page.edited_translation || page.translated_markdown || '')
  const hasTranslation = page.translated_markdown || page.edited_translation
  const detectedLang = page.detected_language || 'en'
  const langNames = { en: 'English', hi: 'Hindi', gu: 'Gujarati', mr: 'Marathi', ta: 'Tamil', te: 'Telugu', kn: 'Kannada', ml: 'Malayalam', pa: 'Punjabi', bn: 'Bengali', or: 'Odia' }
  const isEdited = Boolean(page.edited_translation && page.edited_translation !== page.translated_markdown)

  async function save() {
    await requestJson(`/documents/${workflowId}/pages/${page.page_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_translation: translation, translation_reviewed: true })
    })
    setEditing(false)
    onUpdate()
  }

  return (
    <Card
      className={`cursor-pointer shadow-sm transition-colors ${isActive ? 'border-primary/40 bg-primary/5' : 'bg-background/70'}`}
      onClick={() => !editing && onFocus()}
    >
      <CardHeader className="pb-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="font-sans text-lg">Page {page.page_number}</CardTitle>
              <Badge variant={detectedLang === 'en' ? 'success' : 'info'} className="rounded-full px-3 py-1">
                {langNames[detectedLang] || detectedLang.toUpperCase()}
              </Badge>
              <ReviewStatusBadge reviewed={page.translation_reviewed} />
            </div>
            <p className="text-sm text-muted-foreground">Compare source text and translated text while keeping page context visible.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {hasTranslation && !editing ? (
              <Button variant="secondary" size="sm" className="rounded-lg" onClick={event => { event.stopPropagation(); setEditing(true) }}>
                <WandSparkles className="h-4 w-4" />
                Edit Translation
              </Button>
            ) : null}
            {editing ? (
              <>
                <Button size="sm" className="rounded-lg" onClick={event => { event.stopPropagation(); save() }}>
                  <Save className="h-4 w-4" />
                  Save
                </Button>
                <Button variant="secondary" size="sm" className="rounded-lg" onClick={event => { event.stopPropagation(); setEditing(false) }}>
                  Cancel
                </Button>
              </>
            ) : null}
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {(isEdited || reindexRequired) ? (
          <NoticeCard
            title={reindexRequired ? 'Translation edits have downstream search impact.' : 'This translation has been manually edited.'}
            detail={reindexRequired ? 'Reindex after approvals are complete.' : undefined}
            tone="warning"
            className="rounded-2xl"
          />
        ) : null}
        {detectedLang === 'en' ? (
          <NoticeCard
            title="This page is already in English."
            tone="success"
            className="rounded-2xl"
          />
        ) : hasTranslation ? (
          <div className="grid gap-4 xl:grid-cols-2">
            <div className="flex flex-col gap-2">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Original ({langNames[detectedLang] || detectedLang})</div>
              <Card className="rounded-2xl shadow-none">
                <CardContent className="max-h-[420px] overflow-auto whitespace-pre-wrap p-4 font-mono text-xs leading-6 text-foreground">
                  {page.edited_markdown || page.original_markdown}
                </CardContent>
              </Card>
            </div>
            <div className="flex flex-col gap-2">
              <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">English Translation</div>
              {editing ? (
                <Textarea
                  className="min-h-[320px] rounded-2xl bg-card font-mono text-xs leading-6"
                  value={translation}
                  onChange={event => setTranslation(event.target.value)}
                  onClick={event => event.stopPropagation()}
                />
              ) : (
                <Card className="rounded-2xl border-info/15 bg-info/5 shadow-none">
                  <CardContent className="max-h-[420px] overflow-auto whitespace-pre-wrap p-4 font-mono text-xs leading-6 text-foreground">
                    {page.edited_translation || page.translated_markdown}
                  </CardContent>
                </Card>
              )}
            </div>
          </div>
        ) : (
          <NoticeCard
            title="Translation pending."
            tone="warning"
            className="rounded-2xl"
          />
        )}
      </CardContent>
    </Card>
  )
}

export function ChunkCard({ chunk, workflowId, onUpdate, onPageClick, reindexRequired = false }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(chunk.edited_text || chunk.original_text)
  const pageStart = chunk.page_start || 1
  const pageEnd = chunk.page_end || 1
  const pageRange = pageStart === pageEnd ? `Page ${pageStart}` : `Pages ${pageStart}-${pageEnd}`
  const isEdited = Boolean(chunk.edited_text && chunk.edited_text !== chunk.original_text)

  async function save() {
    await requestJson(`/documents/${workflowId}/chunks/${chunk.chunk_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_text: text, is_reviewed: true })
    })
    setEditing(false)
    onUpdate()
  }

  async function toggleExclude() {
    await requestJson(`/documents/${workflowId}/chunks/${chunk.chunk_number}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_excluded: !chunk.is_excluded })
    })
    onUpdate()
  }

  async function resetChunk() {
    await requestJson(`/documents/${workflowId}/chunks/${chunk.chunk_number}/reset`, { method: 'POST' })
    setEditing(false)
    onUpdate()
  }

  return (
    <Card className={`shadow-sm ${chunk.is_excluded ? 'opacity-70' : 'bg-background/70'}`}>
      <CardHeader className="pb-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="font-sans text-lg">Chunk {chunk.chunk_number}</CardTitle>
              <Badge variant="secondary" className="rounded-full px-3 py-1">{chunk.token_count} tokens</Badge>
              <Button variant="ghost" size="sm" className="h-7 rounded-full px-3 text-xs" onClick={() => onPageClick(pageStart)} title={`Go to ${pageRange}`}>
                <SquareArrowOutUpRight className="h-3.5 w-3.5" />
                {pageRange}
              </Button>
              <ReviewStatusBadge reviewed={chunk.is_reviewed} />
            </div>
            <p className="text-sm text-muted-foreground">Review or trim search units while keeping the source page range linked.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              variant={chunk.is_excluded ? 'warning' : 'secondary'}
              size="sm"
              className="rounded-lg"
              onClick={toggleExclude}
            >
              {chunk.is_excluded ? 'Include' : 'Exclude'}
            </Button>
            {!editing ? (
              <>
                <Button variant="secondary" size="sm" className="rounded-lg" onClick={() => setEditing(true)}>
                  <WandSparkles className="h-4 w-4" />
                  Edit
                </Button>
                <Button variant="ghost" size="sm" className="rounded-lg" onClick={resetChunk}>
                  <RotateCcw className="h-4 w-4" />
                  Reset
                </Button>
              </>
            ) : (
              <>
                <Button size="sm" className="rounded-lg" onClick={save}>
                  <Save className="h-4 w-4" />
                  Save
                </Button>
                <Button variant="secondary" size="sm" className="rounded-lg" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {(isEdited || reindexRequired || chunk.is_excluded) ? (
          <NoticeCard
            title={
              chunk.is_excluded
                ? 'This chunk is excluded from indexing.'
                : reindexRequired
                  ? 'Chunk edits require reindexing to keep search results trustworthy.'
                  : 'This chunk has been manually edited.'
            }
            tone={chunk.is_excluded ? 'default' : 'warning'}
            className="rounded-2xl"
          />
        ) : null}
        {editing ? (
          <Textarea className="min-h-[280px] rounded-2xl bg-card font-mono text-xs leading-6" value={text} onChange={event => setText(event.target.value)} />
        ) : (
          <Card className="rounded-2xl shadow-none">
            <CardContent className="max-h-[420px] overflow-auto whitespace-pre-wrap p-4 font-mono text-xs leading-6 text-foreground">
              {chunk.edited_text || chunk.original_text}
            </CardContent>
          </Card>
        )}
      </CardContent>
    </Card>
  )
}
