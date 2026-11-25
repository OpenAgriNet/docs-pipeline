import { useEffect, useRef, useState } from 'react'

export const CHUNK_PAGE_SIZE = 50
const TYPING_DELAY_MS = 400

// fetcher is the authenticated fetchJson helper in the view. Keeping it explicit
// also lets tests control response ordering without a real server.
export default function useChunkSearch({ query, stage, includeExcluded, selectedTags }, fetcher) {
  const filters = new URLSearchParams()
  if (stage && stage !== 'all') filters.set('stage', stage)
  if (includeExcluded) filters.set('include_excluded', 'true')
  ;[...new Set(selectedTags)].sort().forEach(tag => filters.append('tags', tag))
  const immediateKey = filters.toString()
  if (query.trim()) filters.set('q', query.trim())
  const filterKey = filters.toString()

  const [page, setPage] = useState({ filterKey, offset: 0 })
  const [refreshId, setRefreshId] = useState(0)
  // A changed filter resets pagination on the same render, before any request.
  const offset = page.filterKey === filterKey ? page.offset : 0
  const requestKey = JSON.stringify([filterKey, offset, refreshId])
  const [result, setResult] = useState({
    requestKey: null, chunks: [], total: 0, error: '', pending: true, loaded: false,
  })
  const generation = useRef(0)
  const previous = useRef(null)

  useEffect(() => {
    const id = ++generation.current
    const controller = new AbortController()
    const last = previous.current
    // Initial load, discrete filters, pagination and explicit refresh are
    // immediate. Only changes to the text query wait for a typing pause.
    const delay = last && last.immediateKey === immediateKey
      && last.filterKey !== filterKey && last.refreshId === refreshId
      ? TYPING_DELAY_MS : 0
    previous.current = { filterKey, immediateKey, refreshId }
    setPage(current => current.filterKey === filterKey ? current : { filterKey, offset: 0 })
    setResult(current => ({ ...current, pending: true }))

    async function search() {
      const params = new URLSearchParams(filterKey)
      params.set('limit', String(CHUNK_PAGE_SIZE))
      params.set('offset', String(offset))
      try {
        const response = await fetcher(`/chunks/search?${params}`, { signal: controller.signal })
        // Abort saves work, but the generation check is essential if a transport
        // ignores cancellation or an earlier promise has already resolved.
        if (generation.current !== id) return
        setResult({
          requestKey,
          chunks: Array.isArray(response.items) ? response.items : [],
          total: Number(response.total || 0), error: '', pending: false, loaded: true,
        })
      } catch (error) {
        if (generation.current !== id) return
        setResult({
          requestKey, chunks: [], total: 0,
          error: error.message || 'Unable to search chunks.', pending: false, loaded: true,
        })
      }
    }

    const timer = delay ? setTimeout(search, delay) : null
    if (!delay) search()
    return () => {
      ++generation.current
      if (timer !== null) clearTimeout(timer)
      controller.abort()
    }
  }, [filterKey, immediateKey, offset, refreshId, requestKey, fetcher])

  // Comparing keys disables export immediately on input, including the debounce
  // interval before the effect has started its network request.
  const searching = result.pending || result.requestKey !== requestKey
  return {
    chunks: result.chunks,
    total: result.total,
    error: result.requestKey === requestKey ? result.error : '',
    loading: !result.loaded,
    searching,
    appliedExportParams: !searching && !result.error ? filterKey : null,
    offset,
    requestPage: nextOffset => setPage({ filterKey, offset: Math.max(0, nextOffset) }),
    refresh: () => setRefreshId(current => current + 1),
  }
}
