/**
 * Keycloak / OIDC integration for the maintainer UI.
 *
 * All Keycloak specifics are read from Vite env vars so the same build can
 * target any deployment. When VITE_AUTH_ENABLED is not "true" (the default),
 * the app runs fully open with no login — preserving the local/dev experience.
 *
 * This module is intentionally React-free so plain fetch helpers can import the
 * token accessors without pulling in a React context.
 */

import Keycloak from 'keycloak-js'

const rawAuthEnabled = import.meta.env.VITE_AUTH_ENABLED
export const AUTH_ENABLED = String(rawAuthEnabled ?? 'false').toLowerCase() === 'true'

export const KEYCLOAK_CONFIG = {
  url: import.meta.env.VITE_KEYCLOAK_URL || '',
  realm: import.meta.env.VITE_KEYCLOAK_REALM || '',
  clientId: import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'docs-pipeline-ui',
}

// Refresh the token when it has fewer than this many seconds of validity left.
const MIN_TOKEN_VALIDITY_SECONDS = 30

let keycloak = null
let currentToken = null
let unauthorizedHandler = null

/** Lazily construct the singleton Keycloak instance (null when auth is off). */
export function getKeycloak() {
  if (!AUTH_ENABLED) return null
  if (!keycloak) {
    keycloak = new Keycloak(KEYCLOAK_CONFIG)
  }
  return keycloak
}

export function setCurrentToken(token) {
  currentToken = token || null
}

export function getCurrentToken() {
  return currentToken
}

/** Register the callback used to trigger a fresh login on 401 / refresh failure. */
export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler
}

export function handleUnauthorized() {
  if (typeof unauthorizedHandler === 'function') unauthorizedHandler()
}

/** Authorization header for fetch() calls (empty object when auth is off). */
export function authHeaders() {
  if (!AUTH_ENABLED || !currentToken) return {}
  return { Authorization: `Bearer ${currentToken}` }
}

/**
 * Append ?access_token= to URLs that the browser loads directly as an element
 * src/href (e.g. react-pdf's <Document file>), which cannot carry a header.
 */
export function appendAccessToken(url) {
  if (!AUTH_ENABLED || !currentToken) return url
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}access_token=${encodeURIComponent(currentToken)}`
}

/** Refresh the access token if it is close to expiry. Safe no-op when off. */
export async function ensureFreshToken() {
  if (!AUTH_ENABLED || !keycloak) return
  try {
    const refreshed = await keycloak.updateToken(MIN_TOKEN_VALIDITY_SECONDS)
    if (refreshed) setCurrentToken(keycloak.token)
  } catch {
    handleUnauthorized()
  }
}

/**
 * Drop-in fetch() wrapper: proactively refreshes the token, injects the bearer
 * header, and routes 401s to re-login. Used by every backend call site.
 */
export async function apiFetch(url, options = {}) {
  await ensureFreshToken()
  const headers = { ...(options.headers || {}), ...authHeaders() }
  const response = await fetch(url, { ...options, headers })
  if (response.status === 401 && AUTH_ENABLED) {
    handleUnauthorized()
  }
  return response
}
