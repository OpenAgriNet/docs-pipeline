/**
 * Custom hooks for API interactions.
 *
 * TODO: Extract API calls from App.jsx components into reusable hooks.
 */

import { useState, useCallback } from 'react';
import { API_BASE } from '../config';
import { apiFetch } from '../auth/keycloak';

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
      const response = await apiFetch(`${API_BASE}${url}`, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...options.headers,
        },
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();
      return data;
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
    setDocuments(data);
    return data;
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
