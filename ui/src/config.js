// Prefer VITE_API_BASE at build time (production path prefix).
// Local Vite dev proxies /api → API container/host.
export const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')
