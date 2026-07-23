/**
 * App URL prefix from Vite `base` (Dockerfile.prod / VITE_BASE).
 * Production: '/docs-pipeline/' → all routes live under domain/docs-pipeline/*
 * Local Vite: '/' → no prefix
 */

export const VITE_BASE_URL = import.meta.env.BASE_URL || '/'

/** React Router `basename` — leading slash, no trailing slash (or '' at root). */
export const APP_BASENAME =
  VITE_BASE_URL === '/' ? '' : String(VITE_BASE_URL).replace(/\/$/, '')

/**
 * Absolute path within this app (includes basename).
 * appPath('/login') → '/docs-pipeline/login' in prod, '/login' locally.
 */
export function appPath(path = '/') {
  const normalized =
    !path || path === '/'
      ? '/'
      : path.startsWith('/')
        ? path
        : `/${path}`
  if (!APP_BASENAME) return normalized
  if (normalized === '/') return `${APP_BASENAME}/`
  return `${APP_BASENAME}${normalized}`
}

/** Public asset under Vite base (e.g. login-hero.png). */
export function assetUrl(file) {
  const name = String(file || '').replace(/^\//, '')
  return `${VITE_BASE_URL}${name}`
}
