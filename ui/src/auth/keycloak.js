/**
 * Keycloak / OIDC integration for the docs-pipeline maintainer UI.
 *
 * SSO uses a pop-up + PKCE flow. Tokens are delivered via BOTH:
 *   1) window.postMessage (fast path)
 *   2) sessionStorage bridge (reliable if postMessage races with popup close)
 *
 * When VITE_AUTH_ENABLED is not "true", the app runs fully open.
 */

import Keycloak from 'keycloak-js'
import { appPath } from '../basePath'

const rawAuthEnabled = import.meta.env.VITE_AUTH_ENABLED
export const AUTH_ENABLED = String(rawAuthEnabled ?? 'false').toLowerCase() === 'true'

function normalizeKeycloakUrl(url) {
  return String(url || '').replace(/\/$/, '')
}

const keycloakUrl = normalizeKeycloakUrl(import.meta.env.VITE_KEYCLOAK_URL || '')
const keycloakRealm = import.meta.env.VITE_KEYCLOAK_REALM || ''
const keycloakClientId = import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'docs-pipeline-ui'
const keycloakIdpHint = import.meta.env.VITE_KEYCLOAK_IDP_HINT || 'google'

export const KEYCLOAK_CONFIG = {
  url: keycloakUrl,
  realm: keycloakRealm,
  clientId: keycloakClientId,
}

export const isKeycloakConfigured = Boolean(
  AUTH_ENABLED && keycloakUrl && keycloakRealm && keycloakClientId,
)

/** React Router paths (relative to APP_BASENAME). */
export const ROUTES = {
  LOGIN: '/login',
  AUTH_SSO_CALLBACK: '/auth/sso-callback',
  HOME: '/',
}

/** Full browser path including /docs-pipeline prefix in production. */
export function absoluteRoute(routePath) {
  return appPath(routePath)
}

const AUTH_ERROR_STORAGE_KEY = 'docs-pipeline.authError'
/** Written by the SSO callback popup; read by the opener as a reliable fallback. */
export const SSO_RESULT_STORAGE_KEY = 'docs-pipeline.ssoResult'
/** Persisted Keycloak tokens so a browser refresh keeps the session. */
const SESSION_STORAGE_KEY = 'docs-pipeline.keycloak.session'

const OAUTH_CALLBACK_PARAMS = [
  'error',
  'error_description',
  'error_uri',
  'state',
  'iss',
  'session_state',
  'code',
]

const MIN_TOKEN_VALIDITY_SECONDS = 30
const SSO_POPUP_NAME = 'docs-pipeline-sso'
const SSO_POPUP_FEATURES = 'popup,width=520,height=720,left=120,top=80'

export const KEYCLOAK_SSO_MESSAGE = {
  SUCCESS: 'KEYCLOAK_SSO_SUCCESS',
  ERROR: 'KEYCLOAK_SSO_ERROR',
}

let keycloak = null
let currentToken = null
let unauthorizedHandler = null
let initPromise = null
let sessionHandlersReady = false

function safeText(value) {
  if (value == null) return ''
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value instanceof Error && value.message) return value.message.trim()
  if (typeof value === 'object') {
    if (typeof value.error_description === 'string' && value.error_description.trim()) {
      return value.error_description.trim()
    }
    if (typeof value.error === 'string' && value.error.trim()) {
      return value.error.trim()
    }
    if (typeof value.message === 'string' && value.message.trim()) {
      return value.message.trim()
    }
  }
  return ''
}

export function getAuthErrorMessage(error, description) {
  const desc = safeText(description)
  const err = safeText(error)
  const normalized = `${err} ${desc}`.toLowerCase()

  if (err === 'access_denied' || normalized.includes('access denied')) {
    return "Sign-in was cancelled. You can try again when you're ready."
  }
  if (err === 'login_required') {
    return 'Your session has expired. Please sign in again.'
  }
  if (normalized.includes('redirect_uri') || normalized.includes('invalid parameter: redirect')) {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost:3001'
    return (
      `Keycloak rejected the redirect URL. On client "${keycloakClientId}" add Valid Redirect URIs: ` +
      `${origin}${appPath(ROUTES.LOGIN)} and ${origin}${appPath(ROUTES.AUTH_SSO_CALLBACK)}`
    )
  }
  if (normalized.includes('invalid_client') || normalized.includes('client not found')) {
    return `Keycloak client "${keycloakClientId}" was not found. Check VITE_KEYCLOAK_CLIENT_ID.`
  }
  if (
    normalized.includes('cors') ||
    normalized.includes('failed to fetch') ||
    err === 'token_exchange_failed'
  ) {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost:3001'
    return (
      `Keycloak token exchange failed. Ensure Web Origins includes "${origin}" ` +
      `(or "+") and Valid Redirect URIs include ${origin}${appPath(ROUTES.AUTH_SSO_CALLBACK)}.`
    )
  }
  if (desc && desc.toLowerCase() !== 'undefined') {
    return `Sign-in failed: ${desc}`
  }
  if (err && err !== 'authentication_failed' && err.toLowerCase() !== 'undefined') {
    return `Sign-in failed: ${err}`
  }
  return 'Sign-in could not be completed. Please try again.'
}

export function getStoredAuthError() {
  return sessionStorage.getItem(AUTH_ERROR_STORAGE_KEY)
}

export function clearStoredAuthError() {
  sessionStorage.removeItem(AUTH_ERROR_STORAGE_KEY)
}

function storeAuthError(message) {
  sessionStorage.setItem(AUTH_ERROR_STORAGE_KEY, message)
}

export function clearSsoResult() {
  try {
    sessionStorage.removeItem(SSO_RESULT_STORAGE_KEY)
  } catch {
    // ignore
  }
}

export function writeSsoResult(result) {
  try {
    sessionStorage.setItem(
      SSO_RESULT_STORAGE_KEY,
      JSON.stringify({ ...result, ts: Date.now() }),
    )
  } catch (err) {
    console.warn('Could not write SSO result to sessionStorage:', err)
  }
}

export function readSsoResult() {
  try {
    const raw = sessionStorage.getItem(SSO_RESULT_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    // Ignore stale results older than 2 minutes.
    if (!parsed?.ts || Date.now() - parsed.ts > 120_000) {
      sessionStorage.removeItem(SSO_RESULT_STORAGE_KEY)
      return null
    }
    return parsed
  } catch {
    return null
  }
}

function stripOAuthCallbackParams(url) {
  for (const param of OAUTH_CALLBACK_PARAMS) {
    url.searchParams.delete(param)
  }
  return `${url.pathname}${url.search}${url.hash}`
}

/**
 * Runs before React mounts. When Keycloak/Google returns an OAuth error on a
 * non-callback route, send the user to the app login page.
 */
export function handleOAuthCallbackRedirect() {
  if (typeof window === 'undefined') return
  if (window.location.pathname === appPath(ROUTES.AUTH_SSO_CALLBACK)) return

  const url = new URL(window.location.href)
  const error = url.searchParams.get('error')
  if (!error) return

  const description = url.searchParams.get('error_description')
  storeAuthError(getAuthErrorMessage(error, description))

  if (window.location.pathname !== appPath(ROUTES.LOGIN)) {
    window.location.replace(appPath(ROUTES.LOGIN))
    return
  }

  window.history.replaceState(window.history.state, '', stripOAuthCallbackParams(url))
}

/** Lazily construct the singleton Keycloak instance (null when auth is off). */
export function getKeycloak() {
  if (!isKeycloakConfigured) return null
  if (!keycloak) {
    keycloak = new Keycloak(KEYCLOAK_CONFIG)
  }
  return keycloak
}

/** Fresh instance for the popup callback only (never reuse main-window singleton). */
export function createKeycloakInstance() {
  if (!isKeycloakConfigured) return null
  return new Keycloak(KEYCLOAK_CONFIG)
}

export function setCurrentToken(token) {
  currentToken = token || null
}

export function getCurrentToken() {
  return currentToken
}

function isJwtExpired(token, skewSeconds = 30) {
  const claims = parseJwtPayload(token)
  if (!claims || typeof claims.exp !== 'number') return true
  return claims.exp * 1000 <= Date.now() + skewSeconds * 1000
}

export function loadPersistedSession() {
  try {
    const raw = localStorage.getItem(SESSION_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed?.token) return null
    return {
      token: typeof parsed.token === 'string' ? parsed.token : null,
      refreshToken: typeof parsed.refreshToken === 'string' ? parsed.refreshToken : null,
      idToken: typeof parsed.idToken === 'string' ? parsed.idToken : null,
    }
  } catch {
    return null
  }
}

export function persistSession(tokens) {
  if (!tokens?.token) return
  try {
    localStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({
        token: tokens.token,
        refreshToken: tokens.refreshToken || null,
        idToken: tokens.idToken || null,
        savedAt: Date.now(),
      }),
    )
  } catch (err) {
    console.warn('Could not persist auth session:', err)
  }
}

export function clearPersistedSession() {
  try {
    localStorage.removeItem(SESSION_STORAGE_KEY)
  } catch {
    // ignore
  }
}

function persistFromKeycloak(kc) {
  if (!kc?.token) return
  persistSession({
    token: kc.token,
    refreshToken: kc.refreshToken,
    idToken: kc.idToken,
  })
}

/** Attach tokens to an already-constructed Keycloak instance (no re-init). */
function injectTokens(kc, tokens) {
  if (!kc || !tokens?.token) return false
  kc.token = tokens.token
  kc.refreshToken = tokens.refreshToken || undefined
  kc.idToken = tokens.idToken || undefined
  kc.authenticated = true
  try {
    const parsed = parseJwtPayload(tokens.token)
    if (parsed) kc.tokenParsed = parsed
  } catch {
    // ignore parse errors
  }
  setCurrentToken(tokens.token)
  return true
}

/**
 * Restore a usable access token from localStorage without requiring a successful
 * Keycloak network refresh. Used on refresh / React StrictMode remounts.
 */
function restoreTokenOnlySession() {
  const stored = loadPersistedSession()
  if (!stored?.token) return null
  // Prefer non-expired access token; allow small skew.
  if (!isJwtExpired(stored.token, 10)) {
    return stored
  }
  // Access expired — only usable if we still have a refresh token for later.
  if (stored.refreshToken) {
    return stored
  }
  return null
}

/**
 * Decode a JWT payload without verifying the signature (browser-side display only).
 * Returns null if the token is missing or not a JWT.
 */
export function parseJwtPayload(token) {
  if (!token || typeof token !== 'string') return null
  const parts = token.split('.')
  if (parts.length < 2) return null
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4)
    return JSON.parse(atob(padded))
  } catch {
    return null
  }
}

function collectRolesFromClaims(claims) {
  if (!claims || typeof claims !== 'object') return []
  const roles = new Set()

  const realmRoles = claims.realm_access?.roles
  if (Array.isArray(realmRoles)) {
    for (const role of realmRoles) {
      if (typeof role === 'string' && role.trim()) roles.add(role.trim())
    }
  }

  const resourceAccess = claims.resource_access
  if (resourceAccess && typeof resourceAccess === 'object') {
    for (const clientData of Object.values(resourceAccess)) {
      if (!clientData || typeof clientData !== 'object') continue
      if (Array.isArray(clientData.roles)) {
        for (const role of clientData.roles) {
          if (typeof role === 'string' && role.trim()) roles.add(role.trim())
        }
      }
    }
  }

  if (Array.isArray(claims.roles)) {
    for (const role of claims.roles) {
      if (typeof role === 'string' && role.trim()) roles.add(role.trim())
    }
  }

  // Drop noisy Keycloak defaults for display
  const ignore = new Set([
    'default-roles-bharat-vistaar',
    'offline_access',
    'uma_authorization',
    'account',
  ])
  return [...roles].filter((r) => !ignore.has(r) && !r.startsWith('default-roles-')).sort()
}

/**
 * Build a display profile from JWT claims (name, email, roles).
 * Used so the UI can show the real SSO identity even when AUTH_DISABLED=true
 * causes /auth/me to return the synthetic local-dev user.
 */
export function profileFromAccessToken(token) {
  const claims = parseJwtPayload(token)
  if (!claims) return null

  const email = typeof claims.email === 'string' ? claims.email.trim() : ''
  const preferred =
    typeof claims.preferred_username === 'string' ? claims.preferred_username.trim() : ''
  const fullName = typeof claims.name === 'string' ? claims.name.trim() : ''
  const given = typeof claims.given_name === 'string' ? claims.given_name.trim() : ''
  const family = typeof claims.family_name === 'string' ? claims.family_name.trim() : ''
  const composed = [given, family].filter(Boolean).join(' ').trim()

  const displayName = fullName || composed || preferred || email || ''
  const username = preferred || email || displayName || String(claims.sub || '')

  return {
    user_id: String(claims.sub || ''),
    username,
    name: displayName || username,
    email,
    roles: collectRolesFromClaims(claims),
    claims,
  }
}

/** Merge backend /auth/me with JWT display fields (JWT wins for identity labels). */
export function mergeUserWithJwtProfile(backendUser, token) {
  const profile = profileFromAccessToken(token)
  if (!backendUser && !profile) return null
  if (!profile) {
    return {
      ...backendUser,
      name: backendUser?.username || backendUser?.user_id || '',
    }
  }
  if (!backendUser) {
    return {
      user_id: profile.user_id,
      username: profile.username,
      name: profile.name,
      email: profile.email,
      roles: profile.roles,
      permissions: [],
      instances: [],
      envs: [],
      auth_disabled: false,
    }
  }

  // Prefer JWT identity labels; keep backend permissions (incl. bypass mode).
  const jwtRoles = profile.roles || []
  const backendRoles = Array.isArray(backendUser.roles) ? backendUser.roles : []
  const displayRoles = jwtRoles.length > 0 ? jwtRoles : backendRoles

  return {
    ...backendUser,
    user_id: profile.user_id || backendUser.user_id,
    username: profile.username || backendUser.username,
    name: profile.name || backendUser.username || backendUser.user_id || '',
    email: profile.email || backendUser.email || '',
    roles: displayRoles,
  }
}

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler
}

export function handleUnauthorized() {
  if (typeof unauthorizedHandler === 'function') unauthorizedHandler()
}

export function authHeaders() {
  if (!AUTH_ENABLED || !currentToken) return {}
  return { Authorization: `Bearer ${currentToken}` }
}

/**
 * @deprecated Tokens must be sent in the Authorization header only.
 * This helper is a no-op kept for any leftover call sites; it never appends a token.
 */
export function appendAccessToken(url) {
  return url
}

export async function ensureFreshToken() {
  if (!AUTH_ENABLED) return
  const kc = getKeycloak()
  const current = getCurrentToken() || loadPersistedSession()?.token

  // No Keycloak adapter / not initialized — keep using non-expired stored token.
  if (!kc?.didInitialize) {
    if (current && !isJwtExpired(current, MIN_TOKEN_VALIDITY_SECONDS)) {
      setCurrentToken(current)
      return
    }
    if (current && isJwtExpired(current, 0)) {
      clearPersistedSession()
      handleUnauthorized()
    }
    return
  }

  try {
    const refreshed = await kc.updateToken(MIN_TOKEN_VALIDITY_SECONDS)
    if (refreshed || kc.token) {
      setCurrentToken(kc.token)
      persistFromKeycloak(kc)
    }
  } catch {
    // Prefer staying signed-in on a still-valid access token.
    const fallback = getCurrentToken() || loadPersistedSession()?.token
    if (fallback && !isJwtExpired(fallback, 10)) {
      setCurrentToken(fallback)
      return
    }
    clearPersistedSession()
    handleUnauthorized()
  }
}

export async function apiFetch(url, options = {}) {
  await ensureFreshToken()
  const headers = { ...(options.headers || {}), ...authHeaders() }
  const response = await fetch(url, { ...options, headers })
  if (response.status === 401 && AUTH_ENABLED) {
    handleUnauthorized()
  }
  return response
}

export function getKeycloakRedirectUri() {
  return `${window.location.origin}${appPath(ROUTES.LOGIN)}`
}

export function getKeycloakSsoCallbackUri() {
  // Exact match required in Keycloak Valid Redirect URIs (no trailing slash on callback).
  return `${window.location.origin}${appPath(ROUTES.AUTH_SSO_CALLBACK)}`
}

/** Human-readable Keycloak admin checklist for local / prod. */
export function getKeycloakSetupHints() {
  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost:3001'
  return {
    clientId: keycloakClientId,
    realm: keycloakRealm,
    validRedirectUris: [
      `${origin}${appPath(ROUTES.LOGIN)}`,
      `${origin}${appPath(ROUTES.AUTH_SSO_CALLBACK)}`,
      `${origin}${appPath('/').replace(/\/$/, '') || ''}/*`,
    ],
    webOrigins: [origin, '+'],
    notes: [
      'Access type: public',
      'Standard flow: enabled',
      'Direct access grants: optional',
      'PKCE: S256 (required for public clients)',
      'Production UI is under /docs-pipeline/*',
    ],
  }
}

export function resetKeycloakInit() {
  initPromise = null
}

function setupKeycloakSessionHandlers() {
  const kc = getKeycloak()
  if (!kc || sessionHandlersReady) return

  kc.onTokenExpired = () => {
    kc.updateToken(30)
      .then(() => {
        setCurrentToken(kc.token)
        persistFromKeycloak(kc)
      })
      .catch(() => {
        clearPersistedSession()
        handleUnauthorized()
      })
  }

  sessionHandlersReady = true
}

/**
 * Initialize the main-window Keycloak adapter.
 * Restores tokens from localStorage so a page refresh stays signed in.
 *
 * Important: React StrictMode remounts effects in dev. keycloak-js only allows
 * one init(); after the first init, we MUST re-attach stored tokens instead of
 * treating the remount as logged-out (that was wiping the session on refresh).
 */
export async function initKeycloak() {
  const kc = getKeycloak()
  if (!kc) return false

  setupKeycloakSessionHandlers()

  // Already initialized (StrictMode remount / second caller).
  if (kc.didInitialize) {
    if (kc.authenticated && kc.token && !isJwtExpired(kc.token, 10)) {
      setCurrentToken(kc.token)
      return true
    }
    // Re-hydrate from localStorage instead of failing closed.
    const stored = restoreTokenOnlySession()
    if (stored?.token && !isJwtExpired(stored.token, 10)) {
      injectTokens(kc, stored)
      return true
    }
    if (stored?.refreshToken && kc.refreshToken) {
      try {
        await kc.updateToken(-1)
        setCurrentToken(kc.token)
        persistFromKeycloak(kc)
        return Boolean(kc.token)
      } catch {
        // fall through
      }
    }
    if (stored?.token && !isJwtExpired(stored.token, 10)) {
      setCurrentToken(stored.token)
      return true
    }
    return Boolean(getCurrentToken() && !isJwtExpired(getCurrentToken(), 10))
  }

  if (!initPromise) {
    initPromise = (async () => {
      const stored = loadPersistedSession()
      const accessStillValid = Boolean(stored?.token && !isJwtExpired(stored.token, 10))
      const canTryRefresh = Boolean(stored?.refreshToken)

      try {
        if (stored?.token && (accessStillValid || canTryRefresh)) {
          let authenticated = false
          try {
            authenticated = await kc.init({
              token: stored.token,
              refreshToken: stored.refreshToken || undefined,
              idToken: stored.idToken || undefined,
              pkceMethod: 'S256',
              checkLoginIframe: false,
              flow: 'standard',
              responseMode: 'fragment',
              redirectUri: getKeycloakRedirectUri(),
            })
          } catch (initErr) {
            console.warn('[auth] Keycloak init with stored tokens failed:', initErr)
            // If access token is still valid, keep a token-only session.
            if (accessStillValid) {
              // init may have flipped didInitialize; inject if possible
              if (kc.didInitialize) {
                injectTokens(kc, stored)
              } else {
                setCurrentToken(stored.token)
              }
              return true
            }
            return false
          }

          if (authenticated && kc.token) {
            try {
              await kc.updateToken(MIN_TOKEN_VALIDITY_SECONDS)
            } catch (refreshErr) {
              // Do NOT clear session if access token is still usable.
              console.warn('[auth] Token refresh failed; keeping access token if valid:', refreshErr)
              if (isJwtExpired(kc.token || stored.token, 10)) {
                clearPersistedSession()
                return false
              }
            }
            setCurrentToken(kc.token || stored.token)
            persistFromKeycloak(kc)
            return true
          }

          // keycloak said not authenticated — still use non-expired access token.
          if (accessStillValid) {
            injectTokens(kc, stored)
            return true
          }

          clearPersistedSession()
          return false
        }

        // Cold start — no stored session.
        const authenticated = await kc.init({
          pkceMethod: 'S256',
          checkLoginIframe: false,
          flow: 'standard',
          responseMode: 'fragment',
          redirectUri: getKeycloakRedirectUri(),
        })
        if (authenticated && kc.token) {
          setCurrentToken(kc.token)
          persistFromKeycloak(kc)
          return true
        }
        return false
      } catch (error) {
        // Soft-fail: keep a non-expired stored access token rather than logging out.
        const fallback = restoreTokenOnlySession()
        if (fallback?.token && !isJwtExpired(fallback.token, 10)) {
          console.warn('[auth] Keycloak init error; using stored access token:', error)
          if (kc.didInitialize) {
            injectTokens(kc, fallback)
          } else {
            setCurrentToken(fallback.token)
          }
          return true
        }
        initPromise = null
        console.warn('[auth] Keycloak init failed with no usable stored token:', error)
        return false
      }
    })()
  }

  return initPromise
}

export async function applyKeycloakSession(tokens) {
  const kc = getKeycloak()
  if (!kc) return false

  // Prefer injecting tokens when already initialized (avoids double-init error).
  if (kc.didInitialize) {
    kc.token = tokens.token
    kc.refreshToken = tokens.refreshToken
    kc.idToken = tokens.idToken
    kc.authenticated = Boolean(tokens.token)
    if (tokens.token) {
      try {
        const parsed = parseJwtPayload(tokens.token)
        if (parsed) kc.tokenParsed = parsed
      } catch {
        // display/profile helpers parse independently
      }
    }
    setCurrentToken(kc.token)
    persistSession(tokens)
    return Boolean(kc.authenticated)
  }

  resetKeycloakInit()

  const authenticated = await kc.init({
    token: tokens.token,
    refreshToken: tokens.refreshToken,
    idToken: tokens.idToken,
    checkLoginIframe: false,
    pkceMethod: 'S256',
    flow: 'standard',
    responseMode: 'fragment',
  })

  if (authenticated) {
    setCurrentToken(kc.token)
    persistSession({
      token: kc.token || tokens.token,
      refreshToken: kc.refreshToken || tokens.refreshToken,
      idToken: kc.idToken || tokens.idToken,
    })
  }
  return Boolean(authenticated)
}

/**
 * Full-page Keycloak SSO (preferred).
 *
 * Popup flows often fail token exchange (PKCE/localStorage races, X-Frame
 * issues). Full-page login keeps authorize + code exchange in the same tab so
 * PKCE verifiers stay aligned.
 *
 * Navigates away to Keycloak; does not resolve on success (page unloads).
 * On return, /auth/sso-callback persists tokens and sends the user home.
 */
export async function loginWithKeycloakRedirect() {
  const kc = getKeycloak()
  if (!kc) {
    throw new Error(
      'Keycloak is not configured. Set VITE_AUTH_ENABLED=true plus VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, and VITE_KEYCLOAK_CLIENT_ID.',
    )
  }

  clearSsoResult()

  // Ensure adapter is initialized before login() (sets pkceMethod / endpoints).
  const ready = await initKeycloak()
  if (ready && kc.authenticated && kc.token) {
    // Already signed in (e.g. restored session) — no redirect needed.
    return { status: 'success', tokens: { token: kc.token, refreshToken: kc.refreshToken, idToken: kc.idToken } }
  }

  const redirectUri = getKeycloakSsoCallbackUri()
  console.info('[auth] Starting Keycloak SSO redirect', {
    redirectUri,
    realm: keycloakRealm,
    clientId: keycloakClientId,
    idpHint: keycloakIdpHint,
  })

  // Full-page navigation to Keycloak (and optional Google IdP hint).
  await kc.login({
    redirectUri,
    idpHint: keycloakIdpHint || undefined,
    prompt: 'select_account',
  })

  // Unreachable if login() redirects; kept for type completeness.
  return { status: 'redirecting' }
}

/**
 * @deprecated Use loginWithKeycloakRedirect — kept as an alias for callers.
 */
export async function loginWithKeycloakPopup() {
  return loginWithKeycloakRedirect()
}

export async function logoutFromKeycloak() {
  const kc = getKeycloak()
  setCurrentToken(null)
  clearSsoResult()
  clearPersistedSession()
  if (!kc) return
  // If Keycloak was never fully initialized (or session is local-only), just clear local state.
  if (!kc.didInitialize) return
  try {
    await kc.logout({ redirectUri: `${window.location.origin}${appPath(ROUTES.LOGIN)}` })
  } catch (err) {
    console.warn('Keycloak logout redirect failed; local session already cleared:', err)
  }
}
