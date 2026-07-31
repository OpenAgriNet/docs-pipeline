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
// Empty/unset = show Keycloak login form (username/password + social IdPs).
// Set VITE_KEYCLOAK_IDP_HINT=google only when you want to skip the form.
const keycloakIdpHint = String(import.meta.env.VITE_KEYCLOAK_IDP_HINT || '').trim()

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
    normalized.includes('unauthorized_client') ||
    normalized.includes('invalid client credentials') ||
    normalized.includes('invalid_client')
  ) {
    return (
      `Keycloak rejected client "${keycloakClientId}" (not a public browser client or wrong client id). ` +
      `In Keycloak Admin → Clients → ${keycloakClientId}: set Client authentication = OFF (public), ` +
      `Standard flow = ON, and PKCE S256. Confidential clients need a secret and cannot be used from the UI.`
    )
  }
  if (
    normalized.includes('cors') ||
    normalized.includes('failed to fetch') ||
    err === 'token_exchange_failed'
  ) {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost:3001'
    return (
      `Keycloak token exchange failed. Most common causes: (1) Client authentication must be OFF (public) — ` +
      `not just Redirect URIs; (2) Web Origins must include "${origin}" or "+" (no path); ` +
      `(3) Valid Redirect URIs must include exactly ${origin}${appPath(ROUTES.AUTH_SSO_CALLBACK)}.`
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
 * Detect OAuth callback params in query or hash (Keycloak response modes).
 */
export function readOAuthCallbackParams(href = window.location.href) {
  const url = new URL(href)
  const hash = url.hash.startsWith('#') ? url.hash.slice(1) : url.hash
  const fromHash = new URLSearchParams(hash)
  const get = (key) => url.searchParams.get(key) || fromHash.get(key) || null
  return {
    error: get('error'),
    errorDescription: get('error_description'),
    code: get('code'),
    state: get('state'),
    // Prefer query when code is in search — more reliable with SPA routers.
    responseMode: url.searchParams.has('code') || url.searchParams.has('error') ? 'query' : 'fragment',
  }
}

/**
 * Runs before React mounts.
 * - Forwards authorization codes that landed on the wrong path to /auth/sso-callback
 * - On OAuth error params outside the callback route, sends the user to /login
 */
export function handleOAuthCallbackRedirect() {
  if (typeof window === 'undefined') return

  const callbackPath = appPath(ROUTES.AUTH_SSO_CALLBACK)
  const url = new URL(window.location.href)
  const oauth = readOAuthCallbackParams(url.href)

  // Keycloak sometimes returns to a broader Valid Redirect URI (e.g. /login or /*).
  // Always complete the code exchange on the dedicated callback route.
  if (oauth.code && url.pathname !== callbackPath) {
    const target = new URL(callbackPath, window.location.origin)
    // Preserve whichever form Keycloak used (query or fragment).
    if (url.search && url.search.length > 1) {
      target.search = url.search
    }
    if (url.hash && url.hash.length > 1) {
      target.hash = url.hash
    }
    // If params were only in hash, keep hash; if only query, keep query.
    console.info('[auth] Forwarding OAuth code to SSO callback', {
      from: url.pathname,
      to: target.pathname,
    })
    window.location.replace(target.pathname + target.search + target.hash)
    return
  }

  if (url.pathname === callbackPath) return

  if (!oauth.error) return

  const description = oauth.errorDescription
  storeAuthError(getAuthErrorMessage(oauth.error, description))

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

/** Canonical product roles (must match Keycloak group leaves / realm roles). */
export const UserRole = {
  SUPER_ADMIN: 'super_admin',
  STATE_ADMIN: 'state_admin',
  STATE_VIEW: 'state_view',
  CONTRIBUTOR: 'state_admin',
  REVIEWER: 'state_view',
}

const ROLE_ALIASES = {
  super_admin: UserRole.SUPER_ADMIN,
  'super-admin': UserRole.SUPER_ADMIN,
  superadmin: UserRole.SUPER_ADMIN,
  master_admin: UserRole.SUPER_ADMIN,
  'master-admin': UserRole.SUPER_ADMIN,
  state_admin: UserRole.STATE_ADMIN,
  'state-admin': UserRole.STATE_ADMIN,
  admin: UserRole.STATE_ADMIN,
  contributor: UserRole.STATE_ADMIN,
  content_curator: UserRole.STATE_ADMIN,
  curator: UserRole.STATE_ADMIN,
  state_view: UserRole.STATE_VIEW,
  'state-view': UserRole.STATE_VIEW,
  view: UserRole.STATE_VIEW,
  viewer: UserRole.STATE_VIEW,
  reviewer: UserRole.STATE_VIEW,
}

const ROLE_RANK = {
  [UserRole.SUPER_ADMIN]: 100,
  [UserRole.STATE_ADMIN]: 50,
  [UserRole.STATE_VIEW]: 10,
}

function normalizeRoleName(value) {
  if (!value || typeof value !== 'string') return null
  const key = value.trim().toLowerCase().replace(/\s+/g, '_').replace(/-/g, '_')
  if (ROLE_ALIASES[key]) return ROLE_ALIASES[key]
  if (ROLE_ALIASES[value.trim().toLowerCase()]) return ROLE_ALIASES[value.trim().toLowerCase()]
  return null
}

/**
 * Parse Keycloak group membership paths from the JWT ``groups`` claim.
 * Paths: /global/super-admin, /states/{STATE}/{role}
 */
export function parseGroupsClaim(claims) {
  const raw = claims?.groups ?? claims?.group
  let paths = []
  if (typeof raw === 'string') {
    paths = raw
      .split(/[,;]/)
      .map((p) => p.trim())
      .filter(Boolean)
  } else if (Array.isArray(raw)) {
    paths = raw.map((p) => String(p).trim()).filter(Boolean)
  }

  let isSuperAdmin = false
  const stateRoles = {}

  for (let path of paths) {
    if (!path.startsWith('/')) path = `/${path}`
    path = path.replace(/\/+$/, '') || '/'

    const globalMatch = path.match(/^\/global\/(super[_-]?admin|master[_-]?admin)$/i)
    if (globalMatch) {
      isSuperAdmin = true
      continue
    }

    const stateMatch = path.match(/^\/states\/([A-Za-z0-9_-]+)\/([A-Za-z0-9_-]+)$/i)
    if (!stateMatch) continue
    const state = stateMatch[1].toLowerCase()
    const role = normalizeRoleName(stateMatch[2])
    if (!state || !role) continue
    if (role === UserRole.SUPER_ADMIN) {
      isSuperAdmin = true
      continue
    }
    const current = stateRoles[state]
    if (!current || (ROLE_RANK[role] || 0) > (ROLE_RANK[current] || 0)) {
      stateRoles[state] = role
    }
  }

  const roles = new Set()
  if (isSuperAdmin) roles.add(UserRole.SUPER_ADMIN)
  Object.values(stateRoles).forEach((r) => roles.add(r))

  return {
    groups: [...new Set(paths)].sort(),
    isSuperAdmin,
    stateRoles,
    instances: isSuperAdmin ? [] : Object.keys(stateRoles).sort(),
    roles: [...roles].sort((a, b) => (ROLE_RANK[b] || 0) - (ROLE_RANK[a] || 0)),
  }
}

function collectRolesFromClaims(claims) {
  if (!claims || typeof claims !== 'object') return []
  const roles = new Set()

  // Prefer product roles derived from group paths
  const fromGroups = parseGroupsClaim(claims)
  for (const role of fromGroups.roles) roles.add(role)

  const realmRoles = claims.realm_access?.roles
  if (Array.isArray(realmRoles)) {
    for (const role of realmRoles) {
      const canon = normalizeRoleName(role)
      if (canon) roles.add(canon)
      else if (typeof role === 'string' && role.trim()) roles.add(role.trim())
    }
  }

  const resourceAccess = claims.resource_access
  if (resourceAccess && typeof resourceAccess === 'object') {
    for (const clientData of Object.values(resourceAccess)) {
      if (!clientData || typeof clientData !== 'object') continue
      if (Array.isArray(clientData.roles)) {
        for (const role of clientData.roles) {
          const canon = normalizeRoleName(role)
          if (canon) roles.add(canon)
          else if (typeof role === 'string' && role.trim()) roles.add(role.trim())
        }
      }
    }
  }

  if (Array.isArray(claims.roles)) {
    for (const role of claims.roles) {
      const canon = normalizeRoleName(role)
      if (canon) roles.add(canon)
      else if (typeof role === 'string' && role.trim()) roles.add(role.trim())
    }
  }

  // Drop noisy Keycloak defaults for display
  const ignore = new Set([
    'default-roles-bharat-vistaar',
    'default-roles-docs-pipeline',
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
  const groupAccess = parseGroupsClaim(claims)

  // Legacy multivalued instances claim when groups are absent
  let instances = groupAccess.instances
  if (!groupAccess.isSuperAdmin && instances.length === 0) {
    const raw = claims.instances ?? claims.tenants ?? claims.tenant
    if (Array.isArray(raw)) {
      instances = raw.map((i) => String(i).trim().toLowerCase()).filter(Boolean)
    } else if (typeof raw === 'string' && raw.trim()) {
      instances = raw
        .split(/[,;]/)
        .map((i) => i.trim().toLowerCase())
        .filter(Boolean)
    }
  }

  return {
    user_id: String(claims.sub || ''),
    username,
    name: displayName || username,
    email,
    roles: collectRolesFromClaims(claims),
    groups: groupAccess.groups,
    state_roles: groupAccess.stateRoles,
    instances,
    is_super_admin: groupAccess.isSuperAdmin,
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
      instances: profile.instances || [],
      envs: [],
      groups: profile.groups || [],
      state_roles: profile.state_roles || {},
      is_super_admin: Boolean(profile.is_super_admin),
      auth_disabled: false,
    }
  }

  // Prefer JWT identity labels; keep backend permissions (incl. bypass mode).
  const jwtRoles = profile.roles || []
  const backendRoles = Array.isArray(backendUser.roles) ? backendUser.roles : []
  const displayRoles = jwtRoles.length > 0 ? jwtRoles : backendRoles

  const backendInstances = Array.isArray(backendUser.instances) ? backendUser.instances : []
  const jwtInstances = Array.isArray(profile.instances) ? profile.instances : []
  const instances =
    backendInstances.length > 0
      ? backendInstances
      : jwtInstances

  return {
    ...backendUser,
    user_id: profile.user_id || backendUser.user_id,
    username: profile.username || backendUser.username,
    name: profile.name || backendUser.username || backendUser.user_id || '',
    email: profile.email || backendUser.email || '',
    roles: displayRoles,
    instances,
    groups: backendUser.groups?.length ? backendUser.groups : profile.groups || [],
    state_roles:
      backendUser.state_roles && Object.keys(backendUser.state_roles).length > 0
        ? backendUser.state_roles
        : profile.state_roles || {},
    is_super_admin: Boolean(
      backendUser.is_super_admin ?? profile.is_super_admin,
    ),
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
              responseMode: 'query',
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
          responseMode: 'query',
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
 * Prepare Keycloak for an interactive login without treating a cold start as
 * "already handled". Uses the SSO callback as redirectUri so PKCE + return
 * URL stay aligned with /auth/sso-callback.
 */
async function ensureKeycloakReadyForLogin() {
  const kc = getKeycloak()
  if (!kc) return null

  setupKeycloakSessionHandlers()

  // Already have a live session — caller can skip redirect.
  if (kc.didInitialize && kc.authenticated && kc.token && !isJwtExpired(kc.token, 10)) {
    setCurrentToken(kc.token)
    return kc
  }

  // Adapter already init'd (e.g. AuthProvider on /login) but not signed in.
  if (kc.didInitialize) {
    return kc
  }

  // Cold init for login click — bind redirect to the callback route.
  try {
    await kc.init({
      pkceMethod: 'S256',
      checkLoginIframe: false,
      flow: 'standard',
      // Query mode survives SPA routers and proxies better than hash fragments.
      responseMode: 'query',
      redirectUri: getKeycloakSsoCallbackUri(),
    })
  } catch (err) {
    console.warn('[auth] Keycloak init before login failed; will still try login()', err)
  }
  return kc
}

/**
 * Full-page Keycloak SSO (preferred).
 *
 * Navigates away to Keycloak; does not resolve on success (page unloads).
 * On return, /auth/sso-callback persists tokens and sends the user to dashboard.
 */
export async function loginWithKeycloakRedirect() {
  if (!isKeycloakConfigured) {
    throw new Error(
      'Keycloak is not configured. Set VITE_AUTH_ENABLED=true plus VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, and VITE_KEYCLOAK_CLIENT_ID.',
    )
  }

  clearSsoResult()

  // Prefer restoring an existing local session without bouncing to Keycloak.
  const existing = loadPersistedSession()
  if (existing?.token && !isJwtExpired(existing.token, 10)) {
    const ready = await initKeycloak()
    const kc = getKeycloak()
    if (ready && kc?.token) {
      return {
        status: 'success',
        tokens: {
          token: kc.token,
          refreshToken: kc.refreshToken || existing.refreshToken,
          idToken: kc.idToken || existing.idToken,
        },
      }
    }
    // Token still usable even if adapter restore was flaky.
    setCurrentToken(existing.token)
    return { status: 'success', tokens: existing }
  }

  const kc = await ensureKeycloakReadyForLogin()
  if (!kc) {
    throw new Error(
      'Keycloak is not configured. Set VITE_AUTH_ENABLED=true plus VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, and VITE_KEYCLOAK_CLIENT_ID.',
    )
  }

  if (kc.authenticated && kc.token && !isJwtExpired(kc.token, 10)) {
    setCurrentToken(kc.token)
    persistFromKeycloak(kc)
    return {
      status: 'success',
      tokens: { token: kc.token, refreshToken: kc.refreshToken, idToken: kc.idToken },
    }
  }

  const redirectUri = getKeycloakSsoCallbackUri()
  console.info('[auth] Starting Keycloak SSO redirect', {
    redirectUri,
    realm: keycloakRealm,
    clientId: keycloakClientId,
    idpHint: keycloakIdpHint,
    hints: getKeycloakSetupHints(),
  })

  // Full-page navigation to Keycloak (optional Google IdP hint).
  // This call does not return if the browser navigates away.
  await kc.login({
    redirectUri,
    idpHint: keycloakIdpHint || undefined,
    prompt: 'select_account',
  })

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
