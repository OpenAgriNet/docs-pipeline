import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Checkbox } from '../components/ui/checkbox'
import { Card, CardContent } from '../components/ui/card'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { StageBadge } from '../components/StageBadge'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../components/ui/alert-dialog'
import { NoticeCard } from '../components/NoticeCard'
import { fetchJson, getDocumentListLabel, summarizeAvailableAction, summarizeQueueReason } from '../lib/pipelineUi'
import { CheckCircle, ListTodo, MoreHorizontal, RefreshCw } from 'lucide-react'

const bulkActions = [
  { key: 'approve_ocr', label: 'Bulk approve OCR', path: '/documents/bulk/approve-ocr' },
  { key: 'approve_translation', label: 'Bulk approve translation', path: '/documents/bulk/approve-translation' },
  { key: 'approve_chunks', label: 'Bulk approve chunks', path: '/documents/bulk/approve-chunks' },
  { key: 'reingest_document', label: 'Bulk reindex', path: '/documents/bulk/reindex' }
]

export default function QueueView() {
  const navigate = useNavigate()
  const [queue, setQueue] = useState([])
  const [queueTotal, setQueueTotal] = useState(0)
  const [selected, setSelected] = useState(new Set())
  const [confirmAction, setConfirmAction] = useState(null)
  const [errorBanner, setErrorBanner] = useState('')

  useEffect(() => {
    load()
  }, [])

  async function load() {
    try {
      const pageSize = 200
      let offset = 0
      let allItems = []
      let total = 0
      while (true) {
        const data = await fetchJson(`/operations/queue?limit=${pageSize}&offset=${offset}`)
        const items = data.items || []
        allItems = allItems.concat(items)
        total = data.total || allItems.length
        if (items.length < pageSize) break
        offset += pageSize
      }
      setQueue(allItems)
      setQueueTotal(total)
      setErrorBanner('')
    } catch (error) {
      setErrorBanner(error.message)
    }
  }

  const toggleSelect = id => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  const toggleAll = () => {
    if (selected.size === queue.length) setSelected(new Set())
    else setSelected(new Set(queue.map(item => item.workflow_id)))
  }

  const selectedItems = queue.filter(item => selected.has(item.workflow_id))
  const commonActions = useMemo(() => {
    if (!selectedItems.length) return []
    return bulkActions.filter(action => selectedItems.every(item => (item.available_actions || []).includes(action.key)))
  }, [selectedItems])
  const partialActions = useMemo(() => {
    if (selectedItems.length <= 1) return []
    const union = [...new Set(selectedItems.flatMap(item => item.available_actions || []))]
    return union.filter(action => !commonActions.some(common => common.key === action))
  }, [selectedItems, commonActions])

  async function handleBulkAction(action) {
    setConfirmAction(action)
  }

  async function executeBulkAction() {
    if (!confirmAction) return
    try {
      await fetchJson(confirmAction.path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workflow_ids: Array.from(selected) })
      })
      setSelected(new Set())
      setConfirmAction(null)
      load()
    } catch (error) {
      setErrorBanner(error.message)
      setConfirmAction(null)
    }
  }

  async function handleRowAction(workflowId, action) {
    try {
      if (action === 'mark_reindex_required') {
        await fetchJson(`/documents/${workflowId}/mark-reindex-required`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'Marked manually from queue' })
        })
      } else if (action === 'clear_reindex_required') {
        await fetchJson(`/documents/${workflowId}/clear-reindex-required`, { method: 'POST' })
      } else if (action === 'reingest_document') {
        await fetchJson(`/documents/${workflowId}/reingest`, { method: 'POST' })
      } else {
        await fetchJson(`/documents/${workflowId}/${action.replace(/_/g, '-')}`, { method: 'POST' })
      }
      await load()
    } catch (error) {
      setErrorBanner(error.message)
    }
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-4">
      <div>
        <h1 className="font-serif text-2xl font-semibold text-foreground">Review Queue</h1>
        <p className="mt-1 text-sm text-muted-foreground">{queueTotal || queue.length} items awaiting operator action</p>
      </div>

      {errorBanner && (
        <div className="relative">
          <NoticeCard title="Queue Action Failed" detail={errorBanner} tone="destructive" className="rounded-2xl" />
          <button className="absolute right-4 top-4 text-xs text-destructive hover:underline" onClick={() => setErrorBanner('')}>
            Dismiss
          </button>
        </div>
      )}

      {selected.size > 0 && (
        <div className="action-bar sticky top-0 z-10">
          <span className="text-sm font-medium text-foreground">{selected.size} selected</span>
          {partialActions.length > 0 && selected.size > 1 && (
            <span className="text-xs text-muted-foreground">(some actions unavailable for mixed selection)</span>
          )}
          <div className="flex-1" />
          {commonActions.map(action => (
            <Button
              key={action.key}
              size="sm"
              variant={action.key.includes('approve') ? 'success' : action.key.includes('reindex') || action.key.includes('reingest') ? 'warning' : 'outline'}
              onClick={() => handleBulkAction(action)}
            >
              {action.key.includes('approve') ? <CheckCircle className="h-3.5 w-3.5 mr-1" /> : <RefreshCw className="h-3.5 w-3.5 mr-1" />}
              {action.label}
            </Button>
          ))}
          {partialActions.length > 0 && commonActions.length === 0 && (
            <span className="text-xs text-muted-foreground italic">No common actions available for this selection</span>
          )}
          <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear</Button>
        </div>
      )}

      <Card>
        <CardContent className="overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left">
                <th className="w-10 px-4 py-3">
                  <Checkbox checked={selected.size === queue.length && queue.length > 0} onCheckedChange={toggleAll} aria-label="Select all queue items" />
                </th>
                <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-muted-foreground">Document</th>
                <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-muted-foreground">Reason</th>
                <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-muted-foreground">Stage</th>
                <th className="px-4 py-3 text-xs font-medium uppercase tracking-wider text-muted-foreground">Actions</th>
              </tr>
            </thead>
            <tbody>
              {queue.map(item => (
                <tr key={item.workflow_id} className={`border-b border-border transition-colors hover:bg-accent/40 ${selected.has(item.workflow_id) ? 'bg-accent/40' : ''}`}>
                  <td className="px-4 py-3">
                    <Checkbox checked={selected.has(item.workflow_id)} onCheckedChange={() => toggleSelect(item.workflow_id)} aria-label={`Select ${item.display_name || item.filename || item.workflow_id}`} />
                  </td>
                  <td className="max-w-[220px] px-4 py-3">
                    <span className="block cursor-pointer truncate font-medium text-primary hover:underline" onClick={() => navigate(`/documents/${item.workflow_id}`)}>
                      {item.display_name || item.filename}
                    </span>
                    <div className="mt-0.5 truncate font-mono text-xs text-muted-foreground">{item.workflow_id}</div>
                  </td>
                  <td className="max-w-[240px] px-4 py-3 text-muted-foreground">
                    <span className="block truncate">{item.error_message || item.queue_reason || 'Awaiting manual action'}</span>
                  </td>
                  <td className="px-4 py-3"><StageBadge stage={item.stage} /></td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap items-center gap-1.5">
                      {(item.available_actions || []).slice(0, 2).map(action => (
                        <Button
                          key={action}
                          size="sm"
                          variant={action.includes('approve') ? 'success' : action.includes('reindex') || action.includes('reingest') ? 'warning' : 'outline'}
                          className="h-7 text-xs"
                          onClick={event => {
                            event.stopPropagation()
                            handleRowAction(item.workflow_id, action)
                          }}
                        >
                          {summarizeAvailableAction(action)}
                        </Button>
                      ))}
                      {(item.available_actions || []).length > 2 ? (
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button size="sm" variant="outline" className="h-7 text-xs">
                              <MoreHorizontal className="h-3.5 w-3.5" />
                              More
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            {(item.available_actions || []).slice(2).map(action => (
                              <DropdownMenuItem
                                key={action}
                                onClick={event => {
                                  event.stopPropagation()
                                  handleRowAction(item.workflow_id, action)
                                }}
                              >
                                {summarizeAvailableAction(action)}
                              </DropdownMenuItem>
                            ))}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      ) : null}
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 text-xs"
                        onClick={event => {
                          event.stopPropagation()
                          navigate(`/documents/${item.workflow_id}`)
                        }}
                      >
                        Open
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
              {!queue.length ? (
                <tr><td colSpan={5} className="px-4 py-16 text-center">
                  <ListTodo className="mx-auto mb-3 h-10 w-10 text-muted-foreground/30" />
                  <p className="text-sm font-medium text-foreground">Queue is empty</p>
                  <p className="mt-1 text-xs text-muted-foreground">All documents are progressing normally through the pipeline</p>
                </td></tr>
              ) : null}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <AlertDialog open={!!confirmAction} onOpenChange={() => setConfirmAction(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Confirm Bulk Action</AlertDialogTitle>
            <AlertDialogDescription>
              Apply <strong>{confirmAction?.label || ''}</strong> to{' '}
              <strong>{selected.size}</strong> item{selected.size !== 1 ? 's' : ''}?
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={executeBulkAction}>Confirm</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
