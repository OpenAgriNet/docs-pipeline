import React, { useMemo, useState } from 'react'
import { ArrowLeft, Info, User } from 'lucide-react'
import { formatInstanceLabel, instanceBadgeTitle } from '../lib/instanceLabels'
import {
  formatCompactDateTime,
  getDocumentFileLabel,
  getDocumentListLabel,
  getDocumentMetaLabel,
} from '../lib/pipelineUi'
import { normalizeProductRole, roleLabel } from '../lib/roleCapabilities'
import { InstanceBadge } from './InstanceBadge'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from './ui/sheet'
import { cn } from '../lib/utils'

/** Prefer product roles (contributor / reviewer / super_admin); hide Keycloak noise. */
export function productRoleLabels(roles = []) {
  const seen = new Set()
  const labels = []
  for (const raw of roles || []) {
    const id = normalizeProductRole(raw)
    if (!id || seen.has(id)) continue
    seen.add(id)
    labels.push(roleLabel(id))
  }
  return labels
}

function uploaderName(doc) {
  return doc?.uploaded_by_email || doc?.uploaded_by_username || null
}

function DetailRow({ label, children, mono = false }) {
  if (children == null || children === '') return null
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-2 border-b border-border/70 py-2.5 last:border-0">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd
        className={cn(
          'min-w-0 break-words text-sm text-foreground',
          mono && 'font-mono text-xs',
        )}
      >
        {children}
      </dd>
    </div>
  )
}

/**
 * Compact document title + uploader/state/role line, with optional Details sheet
 * for workflow id, file path, page/chunk counts, etc.
 */
export function DocumentHeaderSummary({
  doc,
  reviewedPages = 0,
  reviewedChunks = 0,
  pageCount,
  chunkCount,
  badges = null,
  onBack,
  className,
}) {
  const [detailsOpen, setDetailsOpen] = useState(false)

  const pages = pageCount ?? doc?.page_count ?? 0
  const chunks = chunkCount ?? doc?.chunk_count ?? 0
  const uploader = uploaderName(doc)
  const stateLabel = formatInstanceLabel(doc?.instance)
  const roleLabels = useMemo(
    () => productRoleLabels(doc?.uploaded_by_roles || []),
    [doc?.uploaded_by_roles],
  )
  const primaryRole = roleLabels[0] || null
  const uploadedAt = doc?.created_at ? formatCompactDateTime(doc.created_at) : null

  const summaryParts = []
  if (uploader) summaryParts.push(`Uploaded by ${uploader}`)
  if (stateLabel) summaryParts.push(stateLabel)
  if (primaryRole) summaryParts.push(primaryRole)
  if (uploadedAt) summaryParts.push(uploadedAt)

  return (
    <>
      <div className={cn('flex items-start gap-3', className)}>
        {onBack ? (
          <Button variant="ghost" size="icon" className="mt-0.5 shrink-0" onClick={onBack}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
        ) : null}

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="max-w-[min(100%,28rem)] truncate text-lg font-serif font-semibold text-foreground">
              {getDocumentListLabel(doc)}
            </h1>
            <InstanceBadge instance={doc?.instance} />
            {badges}
          </div>

          {summaryParts.length > 0 ? (
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
              <User className="hidden h-3.5 w-3.5 shrink-0 sm:inline" aria-hidden />
              {summaryParts.map((part, i) => (
                <React.Fragment key={`${part}-${i}`}>
                  {i > 0 ? <span className="text-border" aria-hidden>·</span> : null}
                  <span className={i === 0 ? 'min-w-0 truncate' : undefined}>{part}</span>
                </React.Fragment>
              ))}
            </p>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">Upload details unavailable</p>
          )}
        </div>

        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 shrink-0 gap-1.5 text-xs"
          onClick={() => setDetailsOpen(true)}
        >
          <Info className="h-3.5 w-3.5" />
          Details
        </Button>
      </div>

      <Sheet open={detailsOpen} onOpenChange={setDetailsOpen}>
        <SheetContent side="right" className="w-full sm:max-w-md">
          <SheetHeader className="text-left">
            <SheetTitle className="pr-8 font-serif">{getDocumentListLabel(doc)}</SheetTitle>
            <SheetDescription>
              Document identity, uploader, and pipeline counts.
            </SheetDescription>
          </SheetHeader>

          <dl className="mt-6">
            <DetailRow label="Uploaded by">
              {uploader || '—'}
            </DetailRow>
            <DetailRow label="State">
              {stateLabel ? (
                <span title={instanceBadgeTitle(doc?.instance)}>{stateLabel}</span>
              ) : (
                '—'
              )}
            </DetailRow>
            <DetailRow label="Role">
              {roleLabels.length > 0 ? roleLabels.join(', ') : '—'}
            </DetailRow>
            <DetailRow label="Uploaded">
              {uploadedAt || '—'}
            </DetailRow>
            <DetailRow label="Workflow ID" mono>
              {getDocumentMetaLabel(doc)}
            </DetailRow>
            <DetailRow label="Source file">
              {getDocumentFileLabel(doc)}
            </DetailRow>
            <DetailRow label="Pages">
              {pages} ({reviewedPages} reviewed)
            </DetailRow>
            <DetailRow label="Chunks">
              {chunks} ({reviewedChunks} reviewed)
            </DetailRow>
            {(doc?.uploaded_by_roles || []).length > 0 ? (
              <DetailRow label="All roles">
                {(doc.uploaded_by_roles || []).join(', ')}
              </DetailRow>
            ) : null}
          </dl>

          {doc?.authoritative != null || doc?.stage ? (
            <div className="mt-4 flex flex-wrap gap-1.5">
              {doc?.authoritative != null ? (
                <Badge variant={doc.authoritative ? 'default' : 'secondary'} className="text-[10px]">
                  {doc.authoritative ? 'Authoritative' : 'Legacy'}
                </Badge>
              ) : null}
              {doc?.stage ? (
                <Badge variant="outline" className="text-[10px] capitalize">
                  {String(doc.stage).replace(/_/g, ' ')}
                </Badge>
              ) : null}
            </div>
          ) : null}
        </SheetContent>
      </Sheet>
    </>
  )
}

export default DocumentHeaderSummary
