import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { API_BASE } from '../config'
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
  loginWithKeycloakRedirect,
  logoutFromKeycloak,
  mergeUserWithJwtProfile,
  ROUTES,
  setCurrentToken,
  setUnauthorizedHandler,
} from './keycloak'

const AuthContext = createContext(null)
const REFRESH_INTERVAL_MS = 20000

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}

function AuthScreen({ title, message, action }) {
  return (
    <div className="flex min-h-svh items-center justify-center bg-background px-5 text-foreground">
      <div className="w-full max-w-md rounded-xl border border-border bg-card p-8 text-center shadow-sm">
        <h1 className="mb-3 text-xl font-semibold">{title}</h1>
        <p className="m-0 text-sm leading-relaxed text-muted-foreground">{message}</p>
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
      className="rounded-lg border-0 bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground cursor-pointer"
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

export function AuthProvider({ children }) {
  const [isInitializing, setIsInitializing] = useState(AUTH_ENABLED && isKeycloakConfigured)
  const [isAuthenticated, setIsAuthenticated] = useState(!AUTH_ENABLED)
  const [isSsoLoading, setIsSsoLoading] = useState(false)
  const [authError, setAuthError] = useState(() => getStoredAuthError())
  const [user, setUser] = useState(null)
  const clearAuthError = useCallback(() => {
    setAuthError(null)
    clearStoredAuthError()
  }, [])

  const syncUnauthenticated = useCallback(() => {
    setIsAuthenticated(false)
    setUser(null)
    setCurrentToken(null)
    clearPersistedSession()
  }, [])

  const applyAuthenticatedUser = useCallback(async (token) => {
    setCurrentToken(token)
    let backendUser = null
    try {
      backendUser = await loadBackendUser(token)
    } catch (err) {
      // Still sign in from JWT claims if /auth/me is down or misconfigured.
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
      return
    }

    if (!isKeycloakConfigured) {
      setIsInitializing(false)
      setIsAuthenticated(false)
      setAuthError(
        'Auth is enabled but Keycloak is not configured. Set VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, and VITE_KEYCLOAK_CLIENT_ID.',
      )
      return
    }

    // The SSO pop-up callback page owns keycloak.init() so the authorization
    // code is exchanged exactly once. Do not init here or we hit:
    // "A 'Keycloak' instance can only be initialized once."
    if (window.location.pathname === ROUTES.AUTH_SSO_CALLBACK) {
      setIsInitializing(false)
      setIsAuthenticated(false)
      return
    }

    setUnauthorizedHandler(() => {
      syncUnauthenticated()
      if (window.location.pathname !== ROUTES.LOGIN && window.location.pathname !== ROUTES.AUTH_SSO_CALLBACK) {
        window.location.replace(ROUTES.LOGIN)
      }
    })

    initKeycloak()
      .then(async (authenticated) => {
        if (!authenticated) {
          syncUnauthenticated()
          return
        }
        try {
          const kc = getKeycloak()
          await applyAuthenticatedUser(kc?.token)
        } catch (err) {
          console.error('Failed to load /auth/me after SSO check:', err)
          setAuthError('We could not verify your account permissions with the API.')
          syncUnauthenticated()
        }
      })
      .catch((error) => {
        console.error('Keycloak initialization failed:', error)
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
        syncUnauthenticated()
      })
      .finally(() => {
        const stored = getStoredAuthError()
        if (stored) {
          setAuthError(stored)
          clearStoredAuthError()
        }
        setIsInitializing(false)
      })
  }, [applyAuthenticatedUser, syncUnauthenticated])

  useEffect(() => {
    if (!AUTH_ENABLED || !isAuthenticated) return undefined
    const id = setInterval(() => {
      ensureFreshToken()
    }, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [isAuthenticated])

  /**
   * Start full-page Keycloak SSO. Usually navigates away to Keycloak.
   * Returns true only if a session was already available (no redirect).
   */
  const loginWithSso = useCallback(async () => {
    clearAuthError()
    setIsSsoLoading(true)

    try {
      const result = await loginWithKeycloakRedirect()

      // Already had a valid session — stay in-app.
      if (result.status === 'success' && result.tokens?.token) {
        try {
          await applyKeycloakSession(result.tokens)
          await applyAuthenticatedUser(result.tokens.token)
          return true
        } catch (err) {
          console.error('Failed to apply existing SSO session:', err)
          setAuthError(
            'Could not establish the session from Keycloak. Check AUTH_DISABLED / KEYCLOAK_* on the backend.',
          )
          syncUnauthenticated()
          return false
        }
      }

      // status === 'redirecting' — browser is leaving for Keycloak.
      return false
    } catch (error) {
      console.error('Keycloak SSO login failed:', error)
      const msg =
        error instanceof Error
          ? error.message
          : typeof error === 'string'
            ? error
            : ''
      setAuthError(
        msg && msg !== 'undefined'
          ? `Sign-in failed: ${msg}`
          : 'Sign-in could not be completed. Please try again.',
      )
      syncUnauthenticated()
      return false
    } finally {
      // If we redirected, this may not run; harmless if it does.
      setIsSsoLoading(false)
    }
  }, [applyAuthenticatedUser, clearAuthError, syncUnauthenticated])

  const logout = useCallback(async () => {
    try {
      await logoutFromKeycloak()
    } catch (error) {
      console.error('Keycloak logout failed:', error)
    } finally {
      syncUnauthenticated()
    }
  }, [syncUnauthenticated])

  const permissions = user?.permissions || []
  const roles = user?.roles || []
  const instances = user?.instances || []
  const displayName = user?.name || user?.username || user?.user_id || null
  const email = user?.email || null
  const primaryRole = roles[0] || null

  const hasPermission = useCallback(
    (perm) => (!AUTH_ENABLED ? true : permissions.includes(perm)),
    [permissions],
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
      isSsoLoading,
      authError,
      user,
      /** Prefer JWT full name / preferred_username */
      username: displayName,
      displayName,
      email,
      roles,
      primaryRole,
      permissions,
      instances,
      hasPermission,
      hasRole,
      loginWithSso,
      logout,
      clearAuthError,
    }),
    [
      isAuthenticated,
      isInitializing,
      isSsoLoading,
      authError,
      user,
      displayName,
      email,
      permissions,
      roles,
      primaryRole,
      instances,
      hasPermission,
      hasRole,
      loginWithSso,
      logout,
      clearAuthError,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/** Shown inside the protected shell when the account has no console permissions. */
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
 * Wraps authenticated app routes. Redirects to /login when auth is on and the
 * session is missing. SSO callback is never wrapped in this.
 */
export function RequireAuth({ children }) {
  const { authEnabled, isAuthenticated, isInitializing } = useAuth()
  const location = useLocation()

  if (!authEnabled) return children

  if (isInitializing) {
    return (
      <div className="flex min-h-svh items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Checking session…</p>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to={ROUTES.LOGIN} replace state={{ from: location.pathname }} />
  }

  return children
}
