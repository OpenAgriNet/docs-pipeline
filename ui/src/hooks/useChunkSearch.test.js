import assert from 'node:assert/strict'
import { test } from 'node:test'
import React from 'react'
import { act, create } from 'react-test-renderer'
import useChunkSearch from './useChunkSearch.js'

async function harness(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const requests = []
  // Deliberately ignore AbortSignal: stale responses must still be harmless.
  const fetcher = (path, { signal }) => new Promise((resolve, reject) => {
    requests.push({ params: new URL(path, 'http://test').searchParams, signal, resolve, reject })
  })
  let filters = { query: '', stage: 'all', includeExcluded: false, selectedTags: [] }
  let state
  function Probe() {
    state = useChunkSearch(filters, fetcher)
    return null
  }
  let root
  await act(async () => { root = create(React.createElement(Probe)) })
  t.after(async () => {
    await act(async () => root.unmount())
    t.mock.timers.reset()
  })
  return {
    requests,
    get state() { return state },
    async change(patch) {
      filters = { ...filters, ...patch }
      await act(async () => root.update(React.createElement(Probe)))
    },
    async tick(ms) { await act(async () => t.mock.timers.tick(ms)) },
    async respond(index, text = 'result', total = 1) {
      await act(async () => requests[index].resolve({ items: [{ text }], total }))
    },
    async fail(index) { await act(async () => requests[index].reject(new Error('Search unavailable'))) },
    async page(offset) { await act(async () => state.requestPage(offset)) },
    async refresh() { await act(async () => state.refresh()) },
    async apply(patch) {
      await act(async () => {
        filters = { ...filters, ...patch }
        state.requestPage(0)
        state.refresh()
        root.update(React.createElement(Probe))
      })
    },
    async unmount() { await act(async () => root.unmount()) },
  }
}

test('typing waits for a 400ms pause and blocks export throughout the delay', async t => {
  const h = await harness(t)
  assert.equal(h.requests.length, 1) // Initial load is immediate.
  await h.respond(0)
  await h.change({ query: 'mil' })
  assert.equal(h.state.searching, true)
  assert.equal(h.state.appliedExportParams, null)
  await h.tick(300)
  await h.change({ query: 'milk' })
  await h.tick(399)
  assert.equal(h.requests.length, 1)
  await h.tick(1)
  assert.equal(h.requests.length, 2)
  assert.equal(h.requests[1].params.get('q'), 'milk')
  await h.respond(1, 'milk')
  assert.equal(h.state.searching, false)
  assert.equal(h.state.chunks[0].text, 'milk')
  assert.equal(new URLSearchParams(h.state.appliedExportParams).get('q'), 'milk')
  assert.equal(new URLSearchParams(h.state.appliedExportParams).has('limit'), false)
})

test('an older response cannot replace the latest results even if abort is ignored', async t => {
  const h = await harness(t)
  await h.respond(0)
  await h.change({ query: 'milk' })
  await h.tick(400)
  await h.change({ query: 'water' })
  assert.equal(h.requests[1].signal.aborted, true)
  await h.tick(400)
  await h.respond(2, 'water')
  await h.respond(1, 'milk')
  assert.equal(h.state.chunks[0].text, 'water')
  assert.equal(new URLSearchParams(h.state.appliedExportParams).get('q'), 'water')
})

test('an older failure cannot unlock export or show errors while a new request runs', async t => {
  const h = await harness(t)
  await h.respond(0)
  await h.change({ query: 'milk' })
  await h.tick(400)
  await h.change({ query: 'water' })
  await h.tick(400)
  await h.fail(1)
  assert.equal(h.state.searching, true)
  assert.equal(h.state.error, '')
  assert.equal(h.state.appliedExportParams, null)
  await h.respond(2, 'water')
  assert.equal(h.state.searching, false)
})

test('discrete filters apply immediately and cancel a pending typing delay', async t => {
  const h = await harness(t)
  await h.respond(0)
  await h.change({ query: 'water' })
  await h.tick(100)
  await h.change({ stage: 'completed', includeExcluded: true, selectedTags: ['topic:milk'] })
  assert.equal(h.requests.length, 2)
  assert.equal(h.requests[1].params.get('q'), 'water')
  assert.equal(h.requests[1].params.get('stage'), 'completed')
  assert.equal(h.requests[1].params.get('include_excluded'), 'true')
  assert.deepEqual(h.requests[1].params.getAll('tags'), ['topic:milk'])
  await h.tick(400)
  assert.equal(h.requests.length, 2)
})

test('pagination is immediate and filter changes reset it, including reverting a filter', async t => {
  const h = await harness(t)
  await h.respond(0, 'page 1', 120)
  await h.page(50)
  assert.equal(h.requests[1].params.get('offset'), '50')
  assert.equal(h.state.appliedExportParams, null)
  await h.respond(1, 'page 2', 120)
  await h.change({ query: 'water' })
  assert.equal(h.state.offset, 0)
  await h.tick(400)
  assert.equal(h.requests[2].params.get('offset'), '0')
  await h.respond(2)
  await h.change({ query: '' })
  await h.tick(400)
  assert.equal(h.requests[3].params.get('offset'), '0')
  assert.equal(h.state.offset, 0)
})

test('current failures block downloads and explicit refresh retries immediately', async t => {
  const h = await harness(t)
  await h.fail(0)
  assert.equal(h.state.error, 'Search unavailable')
  assert.equal(h.state.loading, false)
  assert.equal(h.state.searching, false)
  assert.equal(h.state.appliedExportParams, null)
  await h.refresh()
  assert.equal(h.requests.length, 2)
  assert.equal(h.state.searching, true)
  await h.respond(1, 'recovered')
  assert.equal(h.state.error, '')
  assert.equal(h.state.appliedExportParams, '')
})

test('Clear and presets refresh once immediately, using their new filters from page one', async t => {
  const h = await harness(t)
  await h.respond(0, 'page 1', 120)
  await h.page(50)
  await h.respond(1, 'page 2', 120)
  await h.apply({ query: 'water', stage: 'completed' })
  assert.equal(h.requests.length, 3)
  assert.equal(h.requests[2].params.get('offset'), '0')
  assert.equal(h.requests[2].params.get('q'), 'water')
  assert.equal(h.requests[2].params.get('stage'), 'completed')
  await h.respond(2)
  await h.apply({ query: '', stage: 'all' })
  assert.equal(h.requests.length, 4)
  assert.equal(h.requests[3].params.get('q'), null)
  assert.equal(h.requests[3].params.get('stage'), null)
  await h.tick(400)
  assert.equal(h.requests.length, 4)
})

test('unmount cancels the typing timer and filter changes abort an active request', async t => {
  const h = await harness(t)
  const initialSignal = h.requests[0].signal
  await h.change({ query: 'milk' })
  assert.equal(initialSignal.aborted, true)
  await h.unmount()
  await h.tick(500)
  assert.equal(h.requests.length, 1)
})
