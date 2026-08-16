/**
 * Custom hooks for API interactions.
 *
 * JSON transport goes through ``fetchJson`` in ``pipelineUi`` so FastAPI
 * ``detail`` formatting stays consistent with the rest of the console.
 */

import { useState, useCallback } from 'react';
import { fetchJson } from '../lib/pipelineUi';

/**
 * Generic fetch hook with loading and error states.
 */
export function useFetch() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async (url, options = {}) => {
    setLoading(true);
    setError(null);

    try {
      const headers = { ...options.headers };
      if (options.body != null && !headers['Content-Type'] && !headers['content-type']) {
        headers['Content-Type'] = 'application/json';
      }
      return await fetchJson(url, { ...options, headers });
    } catch (err) {
      setError(err.message);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  return { fetchData, loading, error };
}

/**
 * Hook for fetching documents list.
 */
export function useDocuments() {
  const [documents, setDocuments] = useState([]);
  const { fetchData, loading, error } = useFetch();

  const loadDocuments = useCallback(async (stage = null) => {
    const url = stage ? `/documents?stage=${stage}` : '/documents';
    const data = await fetchData(url);
    const items = Array.isArray(data?.items) ? data.items : [];
    setDocuments(items);
    return items;
  }, [fetchData]);

  return { documents, loadDocuments, loading, error };
}

/**
 * Hook for fetching single document details.
 */
export function useDocument(workflowId) {
  const [document, setDocument] = useState(null);
  const { fetchData, loading, error } = useFetch();

  const loadDocument = useCallback(async () => {
    if (!workflowId) return null;
    const data = await fetchData(`/documents/${workflowId}`);
    setDocument(data);
    return data;
  }, [workflowId, fetchData]);

  return { document, loadDocument, loading, error };
}

/**
 * Hook for document pages.
 */
export function usePages(workflowId) {
  const [pages, setPages] = useState([]);
  const { fetchData, loading, error } = useFetch();

  const loadPages = useCallback(async () => {
    if (!workflowId) return [];
    const data = await fetchData(`/documents/${workflowId}/pages`);
    setPages(data);
    return data;
  }, [workflowId, fetchData]);

  const updatePage = useCallback(async (pageNum, updates) => {
    const data = await fetchData(`/documents/${workflowId}/pages/${pageNum}`, {
      method: 'PATCH',
      body: JSON.stringify(updates),
    });
    setPages(prev => prev.map(p => p.page_number === pageNum ? data : p));
    return data;
  }, [workflowId, fetchData]);

  return { pages, loadPages, updatePage, loading, error };
}

/**
 * Hook for document chunks.
 */
export function useChunks(workflowId) {
  const [chunks, setChunks] = useState([]);
  const { fetchData, loading, error } = useFetch();

  const loadChunks = useCallback(async (includeExcluded = false) => {
    if (!workflowId) return [];
    const url = `/documents/${workflowId}/chunks?include_excluded=${includeExcluded}`;
    const data = await fetchData(url);
    setChunks(data);
    return data;
  }, [workflowId, fetchData]);

  const updateChunk = useCallback(async (chunkNum, updates) => {
    const data = await fetchData(`/documents/${workflowId}/chunks/${chunkNum}`, {
      method: 'PATCH',
      body: JSON.stringify(updates),
    });
    setChunks(prev => prev.map(c => c.chunk_number === chunkNum ? data : c));
    return data;
  }, [workflowId, fetchData]);

  return { chunks, loadChunks, updateChunk, loading, error };
}
