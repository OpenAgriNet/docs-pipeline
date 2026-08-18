/**
 * Keycloak / OIDC integration for the docs-pipeline maintainer UI.
 *
 * SSO is a full-page redirect with PKCE, but the authorization code is
 * exchanged for tokens by OUR BACKEND, not in the browser. That is the whole
 * design constraint here: the Keycloak client is confidential, because the
 * email-otp extension refuses public clients — so SSO and email-OTP can only
 * share one client id if the browser never calls the token endpoint.
 *
 * Consequently there is no keycloak-js adapter. It only drives public clients;
 * with a confidential one it can neither exchange a code nor refresh a token.
 * Session storage and renewal (/auth/session/refresh) replace everything it did.
 *
 * When VITE_AUTH_ENABLED is not "true", the app runs fully open.
 */

import { appPath } from '../basePath'
import { API_BASE } from '../config'

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
/**
 * Marks a session whose tokens were minted by our backend against the
 * confidential client. That is now every session, SSO and OTP alike; the value
 * exists so sessions written by an older public-client build are recognised as
 * unrenewable and discarded rather than half-restored.
 */
const BACKEND_SESSION = 'backend'
/** Where the PKCE verifier waits while the browser is away at Keycloak. */
const PKCE_STORAGE_KEY = 'docs-pipeline.sso.pkce'

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

let currentToken = null
let unauthorizedHandler = null

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
      // Anything not written by the current backend-exchange flow is a session
      // from the old public-client build. Its refresh token belongs to a
      // different client, so it can never be renewed — treat it as stale.
      via: parsed.via === BACKEND_SESSION ? BACKEND_SESSION : 'legacy',
    }
  } catch {
    return null
  }
}

export function persistSession(tokens) {
  if (!tokens?.token) return
  try {
    // Both SSO and OTP tokens come from the confidential client, so only the
    // backend (which holds the secret) can renew them — keycloak-js would get
    // invalid_client either way.
    const via = tokens.via || BACKEND_SESSION
    localStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({
        token: tokens.token,
        refreshToken: tokens.refreshToken || null,
        idToken: tokens.idToken || null,
        via,
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
  BH_VIEWER: 'bh_viewer',
  STATE_ADMIN: 'state_admin',
  STATE_APPROVER: 'state_approver',
  STATE_CONTRIBUTOR: 'state_contributor',
  STATE_VIEW: 'state_view',
}

// Mirrors _ROLE_ALIASES in pipeline/auth/groups.py.
// NOTE: `contributor` is the WEAKEST upload role, not state_admin.
const ROLE_ALIASES = {
  super_admin: UserRole.SUPER_ADMIN,
  'super-admin': UserRole.SUPER_ADMIN,
  superadmin: UserRole.SUPER_ADMIN,
  bh_viewer: UserRole.BH_VIEWER,
  'bh-viewer': UserRole.BH_VIEWER,
  bh_view: UserRole.BH_VIEWER,
  state_admin: UserRole.STATE_ADMIN,
  'state-admin': UserRole.STATE_ADMIN,
  admin: UserRole.STATE_ADMIN,
  state_approver: UserRole.STATE_APPROVER,
  'state-approver': UserRole.STATE_APPROVER,
  approver: UserRole.STATE_APPROVER,
  state_contributor: UserRole.STATE_CONTRIBUTOR,
  'state-contributor': UserRole.STATE_CONTRIBUTOR,
  contributor: UserRole.STATE_CONTRIBUTOR,
  state_view: UserRole.STATE_VIEW,
  'state-view': UserRole.STATE_VIEW,
  view: UserRole.STATE_VIEW,
}

const ROLE_RANK = {
  [UserRole.SUPER_ADMIN]: 100,
  [UserRole.STATE_ADMIN]: 60,
  [UserRole.STATE_APPROVER]: 40,
  [UserRole.STATE_CONTRIBUTOR]: 20,
  [UserRole.STATE_VIEW]: 10,
  [UserRole.BH_VIEWER]: 5,
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
  let isBhViewer = false
  const stateRoles = {}

  for (let path of paths) {
    if (!path.startsWith('/')) path = `/${path}`
    path = path.replace(/\/+$/, '') || '/'

    if (/^\/global\/(super[_-]?admin|superadmin)$/i.test(path)) {
      isSuperAdmin = true
      continue
    }
    if (/^\/global\/(bh[_-]?viewer|bh[_-]?view)$/i.test(path)) {
      isBhViewer = true
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
    if (role === UserRole.BH_VIEWER) {
      isBhViewer = true
      continue
    }
    const current = stateRoles[state]
    if (!current || (ROLE_RANK[role] || 0) > (ROLE_RANK[current] || 0)) {
      stateRoles[state] = role
    }
  }

  const roles = new Set()
  if (isSuperAdmin) roles.add(UserRole.SUPER_ADMIN)
  if (isBhViewer) roles.add(UserRole.BH_VIEWER)
  Object.values(stateRoles).forEach((r) => roles.add(r))

  return {
    groups: [...new Set(paths)].sort(),
    isSuperAdmin,
    isBhViewer,
    stateRoles,
    // super_admin and bh_viewer both span every state, so neither is scoped
    // by an instance list.
    instances: isSuperAdmin || isBhViewer ? [] : Object.keys(stateRoles).sort(),
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
    is_bh_viewer: groupAccess.isBhViewer,
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
      is_bh_viewer: Boolean(profile.is_bh_viewer),
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
    is_bh_viewer: Boolean(backendUser.is_bh_viewer ?? profile.is_bh_viewer),
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

/**
 * Renew the session through the backend.
 *
 * Tokens are issued to a confidential Keycloak client, so a browser-side
 * refresh has no secret to present and comes back invalid_client. The backend
 * holds the secret and does the refresh_token grant on our behalf. This is the
 * only renewal path now — SSO and OTP sessions are indistinguishable here.
 *
 * @returns the new access token, or null when the session is over.
 */
async function refreshBackendSession() {
  const stored = loadPersistedSession()
  if (!stored?.refreshToken) return null
  try {
    const response = await fetch(`${API_BASE}/auth/session/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: stored.refreshToken }),
    })
    if (!response.ok) return null
    const data = await response.json()
    if (!data?.access_token) return null
    const tokens = {
      token: data.access_token,
      refreshToken: data.refresh_token || stored.refreshToken,
      idToken: data.id_token || stored.idToken,
      via: BACKEND_SESSION,
    }
    persistSession(tokens)
    setCurrentToken(tokens.token)
    return tokens.token
  } catch (err) {
    console.warn('[auth] Session refresh failed:', err)
    return null
  }
}

export async function ensureFreshToken() {
  if (!AUTH_ENABLED) return

  // Every session is backend-managed now — keycloak-js cannot refresh a
  // confidential-client token, so it is never consulted here.
  const persisted = loadPersistedSession()
  if (!persisted || persisted.via !== BACKEND_SESSION) {
    if (persisted) clearPersistedSession()
    handleUnauthorized()
    return
  }

  const active = getCurrentToken() || persisted.token
  if (active && !isJwtExpired(active, MIN_TOKEN_VALIDITY_SECONDS)) {
    setCurrentToken(active)
    return
  }
  const renewed = await refreshBackendSession()
  if (renewed) return
  clearPersistedSession()
  handleUnauthorized()
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
      'Client authentication: ON (confidential) — required by the email-otp extension',
      'Standard flow: enabled (browser gets only the code; backend exchanges it)',
      'Direct access grants: enabled, with Direct grant flow = email-otp-direct-grant',
      'PKCE: S256',
      'Backend needs KEYCLOAK_CLIENT_ID + KEYCLOAK_CLIENT_SECRET for the same client',
      'Production UI is under /docs-pipeline/*',
    ],
  }
}

/**
 * Restore a session on page load.
 *
 * There is no keycloak-js init here any more. The adapter can only drive a
 * public client: with a confidential one it cannot exchange a code, refresh a
 * token, or validate a session. Everything it used to do now happens on the
 * backend, so all this has to do is rehydrate localStorage and renew if stale.
 */
export async function initKeycloak() {
  if (!isKeycloakConfigured) return false

  const persisted = loadPersistedSession()
  if (!persisted?.token) return false

  if (persisted.via !== BACKEND_SESSION) {
    // Written by a build that talked to the old public client — its refresh
    // token is for a different client and will never be accepted again.
    clearPersistedSession()
    return false
  }

  let token = persisted.token
  if (isJwtExpired(token, MIN_TOKEN_VALIDITY_SECONDS)) {
    token = await refreshBackendSession()
  }
  if (!token) {
    clearPersistedSession()
    return false
  }

  setCurrentToken(token)
  return true
}

/**
 * Adopt tokens the backend obtained from Keycloak — SSO code exchange or OTP.
 *
 * Deliberately does not touch the Keycloak adapter: these tokens belong to a
 * confidential client, so the adapter could neither refresh nor validate them.
 * The session lives in localStorage and renews through /auth/session/refresh.
 */
export function applyBackendSession(tokens) {
  if (!tokens?.token) return false
  const session = {
    token: tokens.token,
    refreshToken: tokens.refreshToken || null,
    idToken: tokens.idToken || null,
    via: BACKEND_SESSION,
  }
  persistSession(session)
  setCurrentToken(session.token)
  return true
}



/** RFC 7636 verifier: 43-128 chars from the unreserved set. */
function createCodeVerifier() {
  const bytes = new Uint8Array(32)
  window.crypto.getRandomValues(bytes)
  return base64UrlEncode(bytes.buffer)
}

function base64UrlEncode(buffer) {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i])
  return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function createCodeChallenge(verifier) {
  const digest = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return base64UrlEncode(digest)
}

/**
 * Stash the PKCE verifier for the round trip to Keycloak.
 *
 * sessionStorage, not localStorage: it must survive the redirect but die with
 * the tab, and it must not leak into other tabs mid-login.
 */
function stashPkce(verifier, state) {
  try {
    window.sessionStorage.setItem(PKCE_STORAGE_KEY, JSON.stringify({ verifier, state }))
  } catch (err) {
    console.warn('[auth] Could not stash the PKCE verifier:', err)
  }
}

function takePkce() {
  try {
    const raw = window.sessionStorage.getItem(PKCE_STORAGE_KEY)
    window.sessionStorage.removeItem(PKCE_STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

/**
 * Complete SSO by having the backend exchange the authorization code.
 *
 * The browser cannot do this itself — the token endpoint requires the
 * confidential client's secret. Sharing one client id between SSO and email-OTP
 * is exactly what that buys us.
 *
 * @returns the token set on success.
 */
export async function exchangeSsoCode(code, { state } = {}) {
  const stashed = takePkce()
  if (stashed?.state && state && stashed.state !== state) {
    throw new Error('Sign-in state did not match. Please start again.')
  }

  const response = await fetch(`${API_BASE}/auth/sso/exchange`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code,
      redirect_uri: getKeycloakSsoCallbackUri(),
      code_verifier: stashed?.verifier || null,
    }),
  })

  const data = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(data?.detail || data?.message || 'Could not complete sign-in.')
  }
  if (!data?.access_token) throw new Error('Keycloak returned no access token.')

  return {
    token: data.access_token,
    refreshToken: data.refresh_token || null,
    idToken: data.id_token || null,
  }
}

/**
 * Full-page Keycloak SSO.
 *
 * The authorize URL is built by hand rather than through keycloak-js: the
 * adapter insists on completing the token exchange in the browser, which a
 * confidential client rejects. Only the authorize leg happens here; the code
 * that comes back goes to the backend (see exchangeSsoCode).
 *
 * Navigates away on success; does not resolve.
 */
export async function loginWithKeycloakRedirect() {
  if (!isKeycloakConfigured) {
    throw new Error(
      'Keycloak is not configured. Set VITE_AUTH_ENABLED=true plus VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, and VITE_KEYCLOAK_CLIENT_ID.',
    )
  }

  clearSsoResult()

  // Reuse a live local session instead of bouncing through Keycloak.
  const existing = loadPersistedSession()
  if (existing?.token && existing.via === BACKEND_SESSION && !isJwtExpired(existing.token, 10)) {
    setCurrentToken(existing.token)
    return { status: 'success', tokens: existing }
  }

  const redirectUri = getKeycloakSsoCallbackUri()
  const verifier = createCodeVerifier()
  const state = createCodeVerifier()
  stashPkce(verifier, state)

  const params = new URLSearchParams({
    client_id: keycloakClientId,
    redirect_uri: redirectUri,
    response_type: 'code',
    scope: 'openid profile email',
    state,
    code_challenge: await createCodeChallenge(verifier),
    code_challenge_method: 'S256',
    prompt: 'select_account',
  })
  // Empty hint = show the Keycloak page (Google button + password form).
  // Set VITE_KEYCLOAK_IDP_HINT=google to jump straight to Google.
  if (keycloakIdpHint) params.set('kc_idp_hint', keycloakIdpHint)

  const authorizeUrl = `${keycloakUrl}/realms/${encodeURIComponent(
    keycloakRealm,
  )}/protocol/openid-connect/auth?${params.toString()}`

  console.info('[auth] Starting Keycloak SSO redirect', {
    redirectUri,
    realm: keycloakRealm,
    clientId: keycloakClientId,
    idpHint: keycloakIdpHint || '(none — Keycloak login page)',
  })

  window.location.assign(authorizeUrl)
  return { status: 'redirecting' }
}


export async function logoutFromKeycloak() {
  const stored = loadPersistedSession()
  setCurrentToken(null)
  clearSsoResult()
  clearPersistedSession()
  if (!isKeycloakConfigured) return

  // Built by hand for the same reason login is: the adapter is never initialized
  // against this confidential client, so kc.logout() has nothing to work with.
  const params = new URLSearchParams({
    post_logout_redirect_uri: `${window.location.origin}${appPath(ROUTES.LOGIN)}`,
    client_id: keycloakClientId,
  })
  // Keycloak requires either an id_token_hint or client_id to skip the
  // "do you want to log out?" confirmation page.
  if (stored?.idToken) params.set('id_token_hint', stored.idToken)

  window.location.assign(
    `${keycloakUrl}/realms/${encodeURIComponent(
      keycloakRealm,
    )}/protocol/openid-connect/logout?${params.toString()}`,
  )
}
