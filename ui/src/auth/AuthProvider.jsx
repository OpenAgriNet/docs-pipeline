import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { API_BASE } from '../config'
import { appPath } from '../basePath'
import { AuthLoadingScreen } from '../components/AuthLoadingScreen'
import { setActiveInstanceValue } from '../lib/activeInstance'
import {
  AUTH_ENABLED,
  applyKeycloakSession,
  clearPersistedSession,
  clearStoredAuthError,
  ensureFreshToken,
  getAuthErrorMessage,
  getKeycloak,
  getStoredAuthError,
  initKeycloak,
  isKeycloakConfigured,
  loadPersistedSession,
  loginWithKeycloakRedirect,
  logoutFromKeycloak,
  mergeUserWithJwtProfile,
  ROUTES,
  setCurrentToken,
  setUnauthorizedHandler,
} from './keycloak'

const AuthContext = createContext(null)
const REFRESH_INTERVAL_MS = 20000

/** localStorage key remembering the caller's last-selected tenant instance. */
const ACTIVE_INSTANCE_KEY = 'docs-pipeline.activeInstance'

/** Every data-plane permission this console gates on (kept in sync with the sidebar/views). */
const ALL_PERMISSIONS = ['search', 'review', 'upload', 'pipeline', 'admin']

/**
 * Map a tenant role (state_admin | content_curator | viewer) to the data-plane
 * permissions it grants inside its instance. This is the only place the tenant
 * role vocabulary is translated into the console's existing permission strings.
 */
const TENANT_ROLE_PERMISSIONS = {
  viewer: ['search'],
  content_curator: ['search', 'review', 'upload', 'pipeline'],
  state_admin: ['search', 'review', 'upload', 'pipeline', 'admin'],
}

function readStoredActiveInstance() {
  if (typeof window === 'undefined') return null
  try {
    return localStorage.getItem(ACTIVE_INSTANCE_KEY) || null
  } catch {
    return null
  }
}

function persistActiveInstance(value) {
  if (typeof window === 'undefined') return
  try {
    if (value) localStorage.setItem(ACTIVE_INSTANCE_KEY, value)
    else localStorage.removeItem(ACTIVE_INSTANCE_KEY)
  } catch {
    // ignore storage failures
  }
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}

function AuthScreen({ title, message, action }) {
  return (
    <div className="flex min-h-svh items-center justify-center bg-[#f7faf8] px-5 text-[#14201b]">
      <div className="w-full max-w-md rounded-2xl border border-[#d5e0db] bg-white p-8 text-center shadow-sm">
        <h1 className="mb-3 text-xl font-semibold">{title}</h1>
        <p className="m-0 text-sm leading-relaxed text-[#5f7269]">{message}</p>
        {action ? <div className="mt-6">{action}</div> : null}
      </div>
    </div>
  )
}

function ScreenButton({ children, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border-0 bg-[#059669] px-4 py-2.5 text-sm font-semibold text-white cursor-pointer hover:bg-[#047857]"
    >
      {children}
    </button>
  )
}

async function loadBackendUser(token) {
  const res = await fetch(`${API_BASE}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!res.ok) {
    throw new Error(`auth/me failed: ${res.status}`)
  }
  return res.json()
}

function isSsoCallbackPath() {
  return typeof window !== 'undefined' && window.location.pathname === appPath(ROUTES.AUTH_SSO_CALLBACK)
}

/** True when localStorage already has tokens — used to avoid login flash on refresh. */
function hasSessionHint() {
  if (typeof window === 'undefined') return false
  try {
    const stored = loadPersistedSession()
    return Boolean(stored?.token)
  } catch {
    return false
  }
}

export function AuthProvider({ children }) {
  const sessionHint = hasSessionHint()
  const [isInitializing, setIsInitializing] = useState(
    AUTH_ENABLED && isKeycloakConfigured && !isSsoCallbackPath(),
  )
  // If we already have tokens in storage, treat as "pending restore" not logged-out.
  const [isAuthenticated, setIsAuthenticated] = useState(!AUTH_ENABLED)
  const [isSsoLoading, setIsSsoLoading] = useState(false)
  const [authError, setAuthError] = useState(() => getStoredAuthError())
  const [user, setUser] = useState(null)
  // Keep true when a session hint exists so UI never assumes "logged out" mid-boot.
  const [bootstrapped, setBootstrapped] = useState(
    !(AUTH_ENABLED && isKeycloakConfigured) || isSsoCallbackPath(),
  )
  // Active tenant instance selection, persisted across reloads. Reconciled against
  // the caller's actual instances once /auth/me resolves (see effect below).
  const [activeInstance, setActiveInstanceState] = useState(() => readStoredActiveInstance())

  const clearAuthError = useCallback(() => {
    setAuthError(null)
    clearStoredAuthError()
  }, [])

  const syncUnauthenticated = useCallback((options = {}) => {
    const { clearStorage = true } = options
    setIsAuthenticated(false)
    setUser(null)
    setCurrentToken(null)
    if (clearStorage) clearPersistedSession()
  }, [])

  const applyAuthenticatedUser = useCallback(async (token) => {
    setCurrentToken(token)
    let backendUser = null
    try {
      backendUser = await loadBackendUser(token)
    } catch (err) {
      console.warn('loadBackendUser failed; using JWT profile only:', err)
    }
    const merged = mergeUserWithJwtProfile(backendUser, token)
    if (!merged) {
      throw new Error('No user profile from JWT or API')
    }
    setUser(merged)
    setIsAuthenticated(true)
    return merged
  }, [])

  useEffect(() => {
    if (!AUTH_ENABLED) {
      setIsInitializing(false)
      setIsAuthenticated(true)
      setBootstrapped(true)
      return
    }

    if (!isKeycloakConfigured) {
      setIsInitializing(false)
      setIsAuthenticated(false)
      setBootstrapped(true)
      setAuthError(
        'Auth is enabled but Keycloak is not configured. Set VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, and VITE_KEYCLOAK_CLIENT_ID.',
      )
      return
    }

    // Callback page owns the OAuth code exchange.
    if (isSsoCallbackPath()) {
      setIsInitializing(false)
      setIsAuthenticated(false)
      setBootstrapped(true)
      return
    }

    setUnauthorizedHandler(() => {
      syncUnauthenticated({ clearStorage: true })
      if (
        window.location.pathname !== appPath(ROUTES.LOGIN) &&
        window.location.pathname !== appPath(ROUTES.AUTH_SSO_CALLBACK)
      ) {
        window.location.replace(appPath(ROUTES.LOGIN))
      }
    })

    let cancelled = false

    ;(async () => {
      try {
        const authenticated = await initKeycloak()
        if (cancelled) return

        if (!authenticated) {
          // No usable session after restore attempt.
          setIsAuthenticated(false)
          setUser(null)
          setCurrentToken(null)
          return
        }

        const kc = getKeycloak()
        const token = kc?.token || loadPersistedSession()?.token
        if (!token) {
          setIsAuthenticated(false)
          setUser(null)
          return
        }
        await applyAuthenticatedUser(token)
      } catch (error) {
        if (cancelled) return
        console.error('Keycloak initialization failed:', error)
        // Last chance: non-expired token in localStorage (ignore Keycloak adapter glitches).
        const stored = loadPersistedSession()
        if (stored?.token) {
          try {
            await applyAuthenticatedUser(stored.token)
            return
          } catch (err2) {
            console.warn('Token-only restore failed:', err2)
          }
        }
        if (error && typeof error === 'object' && 'error' in error && typeof error.error === 'string') {
          setAuthError(
            getAuthErrorMessage(
              error.error,
              'error_description' in error && typeof error.error_description === 'string'
                ? error.error_description
                : null,
            ),
          )
        }
        // Do not wipe storage on cancelled StrictMode races; only clear on hard failure.
        if (!cancelled) {
          syncUnauthenticated({ clearStorage: true })
        }
      } finally {
        if (cancelled) return
        const storedErr = getStoredAuthError()
        if (storedErr) {
          setAuthError(storedErr)
          clearStoredAuthError()
        }
        setIsInitializing(false)
        setBootstrapped(true)
      }
    })()

    // Do NOT cancel in-flight restore on unmount — React StrictMode remounts
    // would otherwise race and leave auth half-cleared. The `cancelled` flag
    // only skips setState after unmount; init itself may finish and cache tokens.
    return () => {
      cancelled = true
    }
  }, [applyAuthenticatedUser, syncUnauthenticated])

  useEffect(() => {
    if (!AUTH_ENABLED || !isAuthenticated) return undefined
    const id = setInterval(() => {
      ensureFreshToken()
    }, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [isAuthenticated])

  const loginWithSso = useCallback(async () => {
    clearAuthError()
    setIsSsoLoading(true)

    try {
      const result = await loginWithKeycloakRedirect()

      if (result.status === 'success' && result.tokens?.token) {
        try {
          await applyKeycloakSession(result.tokens)
          await applyAuthenticatedUser(result.tokens.token)
          setIsSsoLoading(false)
          return true
        } catch (err) {
          console.error('Failed to apply existing SSO session:', err)
          setAuthError(
            'Could not establish the session from Keycloak. Check AUTH_DISABLED / KEYCLOAK_* on the backend.',
          )
          syncUnauthenticated({ clearStorage: true })
          setIsSsoLoading(false)
          return false
        }
      }

      // Redirecting to Keycloak — keep button loading until the page unloads.
      return false
    } catch (error) {
      console.error('Keycloak SSO login failed:', error)
      const msg =
        error instanceof Error ? error.message : typeof error === 'string' ? error : ''
      setAuthError(
        msg && msg !== 'undefined'
          ? `Sign-in failed: ${msg}`
          : 'Sign-in could not be completed. Please try again.',
      )
      syncUnauthenticated({ clearStorage: false })
      setIsSsoLoading(false)
      return false
    }
  }, [applyAuthenticatedUser, clearAuthError, syncUnauthenticated])

  const logout = useCallback(async () => {
    try {
      await logoutFromKeycloak()
    } catch (error) {
      console.error('Keycloak logout failed:', error)
    } finally {
      syncUnauthenticated({ clearStorage: true })
    }
  }, [syncUnauthenticated])

  const permissions = user?.permissions || []
  const roles = user?.roles || []
  const instances = user?.instances || []
  const tenantRoles = user?.tenant_roles || {}
  const isSuperadmin = Boolean(user?.is_superadmin)
  // A control-plane platform admin manages tenants; it holds no data-plane
  // permissions of its own. `is_superadmin` is the data-unrestricted variant.
  const isPlatformAdmin = !AUTH_ENABLED || Boolean(user?.is_platform_admin) || isSuperadmin
  // "Multi-tenant token": the caller was issued explicit tenant context. Legacy
  // single-tenant tokens carry neither, and must keep behaving exactly as today.
  const hasTenantContext = instances.length > 0 || Object.keys(tenantRoles).length > 0
  const displayName = user?.name || user?.username || user?.user_id || null
  const email = user?.email || null
  const primaryRole = roles[0] || null

  // Reconcile the persisted selection against the caller's real instances, then
  // mirror the resolved value into the module-level holder that fetchJson reads.
  useEffect(() => {
    let next = null
    if (activeInstance && instances.includes(activeInstance)) next = activeInstance
    else if (instances.length > 0) next = instances[0]
    else next = null // pure platform admin / legacy single-tenant → no instance param
    if (next !== activeInstance) {
      setActiveInstanceState(next)
      persistActiveInstance(next)
    }
    setActiveInstanceValue(next)
  }, [activeInstance, instances])

  const setActiveInstance = useCallback(
    (next) => {
      const value = next && instances.includes(next) ? next : null
      setActiveInstanceState(value)
      persistActiveInstance(value)
      setActiveInstanceValue(value)
    },
    [instances],
  )

  // Permissions the caller holds *in the active instance*. Single source of truth
  // for every data-plane gate (nav + in-view controls).
  const permissionsInActiveInstance = useCallback(() => {
    if (!AUTH_ENABLED) return [...ALL_PERMISSIONS]
    if (isSuperadmin) return [...ALL_PERMISSIONS]
    if (hasTenantContext) {
      const grantedRoles = (activeInstance && tenantRoles[activeInstance]) || []
      const granted = new Set()
      for (const role of grantedRoles) {
        for (const perm of TENANT_ROLE_PERMISSIONS[role] || []) granted.add(perm)
      }
      return [...granted]
    }
    // Pure control-plane platform admin (no tenant membership) sees no data pages.
    if (isPlatformAdmin) return []
    // Legacy single-tenant token: original flat permissions + search safety net so
    // an authenticated SSO user is never left with a blank sidebar after refresh.
    const legacy = new Set(permissions)
    if (isAuthenticated) legacy.add('search')
    return [...legacy]
  }, [isSuperadmin, hasTenantContext, activeInstance, tenantRoles, isPlatformAdmin, permissions, isAuthenticated])

  const hasPermission = useCallback(
    (perm) => (!AUTH_ENABLED ? true : permissionsInActiveInstance().includes(perm)),
    [permissionsInActiveInstance],
  )
  const hasRole = useCallback(
    (role) => (!AUTH_ENABLED ? true : roles.includes(role)),
    [roles],
  )

  const value = useMemo(
    () => ({
      authEnabled: AUTH_ENABLED,
      isAuthenticated: AUTH_ENABLED ? isAuthenticated : true,
      isInitializing,
      bootstrapped,
      sessionHint,
      isSsoLoading,
      authError,
      user,
      username: displayName,
      displayName,
      email,
      roles,
      primaryRole,
      permissions,
      instances,
      tenantRoles,
      isPlatformAdmin,
      isSuperadmin,
      hasTenantContext,
      activeInstance,
      setActiveInstance,
      permissionsInActiveInstance,
      hasPermission,
      hasRole,
      loginWithSso,
      logout,
      clearAuthError,
    }),
    [
      isAuthenticated,
      isInitializing,
      bootstrapped,
      sessionHint,
      isSsoLoading,
      authError,
      user,
      displayName,
      email,
      permissions,
      roles,
      primaryRole,
      instances,
      tenantRoles,
      isPlatformAdmin,
      isSuperadmin,
      hasTenantContext,
      activeInstance,
      setActiveInstance,
      permissionsInActiveInstance,
      hasPermission,
      hasRole,
      loginWithSso,
      logout,
      clearAuthError,
    ],
  )

  // Global bootstrap gate: never paint login/app routes until session restore finishes.
  // SSO callback is excluded so the OAuth return page can run.
  if (AUTH_ENABLED && isKeycloakConfigured && !bootstrapped && !isSsoCallbackPath()) {
    return (
      <AuthContext.Provider value={value}>
        <AuthLoadingScreen
          title={sessionHint ? 'Welcome back…' : 'Loading…'}
          message={sessionHint ? 'Restoring your session' : 'Preparing secure sign-in'}
        />
      </AuthContext.Provider>
    )
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function NoPermissionScreen() {
  const { username, logout } = useAuth()
  return (
    <AuthScreen
      title="Access not granted"
      message={`Signed in as ${username || 'this account'}, but you have no permissions for this console. Contact an administrator.`}
      action={<ScreenButton onClick={() => void logout()}>Sign out</ScreenButton>}
    />
  )
}

/**
 * Wraps authenticated app routes. Shows a loader while restoring session;
 * only redirects to /login after bootstrap knows there is no session.
 */
export function RequireAuth({ children }) {
  const { authEnabled, isAuthenticated, isInitializing, bootstrapped } = useAuth()
  const location = useLocation()

  if (!authEnabled) return children

  if (!bootstrapped || isInitializing) {
    return (
      <AuthLoadingScreen
        title="Welcome back…"
        message="Restoring your session"
      />
    )
  }

  if (!isAuthenticated) {
    return <Navigate to={ROUTES.LOGIN} replace state={{ from: location.pathname }} />
  }

  return children
}
