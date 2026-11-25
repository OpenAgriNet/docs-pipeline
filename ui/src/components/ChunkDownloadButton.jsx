import React, { useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import { Button } from './ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'
import { downloadFile } from '../lib/pipelineUi'

export default function ChunkDownloadButton({ path, label = 'Download chunks', disabled = false }) {
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState('')

  async function download(format) {
    if (downloading) return
    setDownloading(true)
    setError('')
    try {
      const separator = path.includes('?') ? '&' : '?'
      await downloadFile(`${path}${separator}format=${format}`, `chunks.${format}`)
    } catch (failure) {
      setError(failure.message || 'Unable to download chunks.')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" disabled={disabled || downloading}>
            {downloading ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Download className="h-4 w-4 mr-2" />}
            {downloading ? 'Downloading…' : label}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => download('json')}>JSON</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => download('csv')}>CSV</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {error && <p role="alert" className="text-xs text-destructive max-w-sm">{error}</p>}
    </div>
  )
}
