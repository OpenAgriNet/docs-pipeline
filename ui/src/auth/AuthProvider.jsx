import React, { createContext, useContext, useEffect, useRef, useState } from 'react'
import { API_BASE } from '../config'
import {
  AUTH_ENABLED,
  ensureFreshToken,
  getKeycloak,
  setCurrentToken,
  setUnauthorizedHandler,
} from './keycloak'

const AuthContext = createContext(null)

// Standard Keycloak silent SSO check page (served from ui/public/).
const SILENT_SSO_URI = `${window.location.origin}/silent-check-sso.html`
// Nudge the refresh loop often; updateToken only calls the network when the
// token is actually near expiry (MIN_TOKEN_VALIDITY_SECONDS in keycloak.js).
const REFRESH_INTERVAL_MS = 20000

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}

function AuthScreen({ title, message, action }) {
  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: '#f9fafb',
      color: '#14213d',
      padding: '20px',
    }}>
      <div style={{
        maxWidth: '420px',
        width: '100%',
        background: 'white',
        borderRadius: '10px',
        padding: '32px',
        boxShadow: '0 2px 10px rgba(0,0,0,0.08)',
        border: '1px solid #e5e7eb',
        textAlign: 'center',
      }}>
        <h1 style={{ margin: '0 0 12px', fontSize: '20px', fontWeight: 600 }}>{title}</h1>
        <p style={{ margin: 0, color: '#6b7280', lineHeight: 1.5 }}>{message}</p>
        {action ? <div style={{ marginTop: '24px' }}>{action}</div> : null}
      </div>
    </div>
  )
}

function ScreenButton({ children, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '10px 18px',
        borderRadius: '8px',
        border: 'none',
        background: '#1d4ed8',
        color: 'white',
        fontSize: '14px',
        fontWeight: 600,
        cursor: 'pointer',
      }}
    >
      {children}
    </button>
  )
}

export function AuthProvider({ children }) {
  const [status, setStatus] = useState(AUTH_ENABLED ? 'loading' : 'ready')
  const [user, setUser] = useState(null)
  const initStarted = useRef(false)

  useEffect(() => {
    if (!AUTH_ENABLED) return
    if (initStarted.current) return // guard against StrictMode double-invoke
    initStarted.current = true

    const keycloak = getKeycloak()
    setUnauthorizedHandler(() => keycloak.login())

    keycloak
      .init({
        onLoad: 'login-required',
        pkceMethod: 'S256',
        checkLoginIframe: false,
        silentCheckSsoRedirectUri: SILENT_SSO_URI,
      })
      .then(async (authenticated) => {
        if (!authenticated) {
          keycloak.login()
          return
        }
        setCurrentToken(keycloak.token)
        keycloak.onTokenExpired = () => { ensureFreshToken() }
        try {
          const res = await fetch(`${API_BASE}/auth/me`, {
            headers: { Authorization: `Bearer ${keycloak.token}` },
          })
          if (!res.ok) {
            setStatus('error')
            return
          }
          setUser(await res.json())
          setStatus('ready')
        } catch {
          setStatus('error')
        }
      })
      .catch(() => setStatus('error'))
  }, [])

  // Proactive token-refresh loop while the app is live.
  useEffect(() => {
    if (!AUTH_ENABLED || status !== 'ready') return undefined
    const id = setInterval(() => { ensureFreshToken() }, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [status])

  const permissions = user?.permissions || []
  const roles = user?.roles || []
  const instances = user?.instances || []

  // When auth is disabled the app runs fully open — every gate passes.
  const hasPermission = (perm) => (!AUTH_ENABLED ? true : permissions.includes(perm))
  const hasRole = (role) => (!AUTH_ENABLED ? true : roles.includes(role))

  const logout = () => {
    const keycloak = getKeycloak()
    if (keycloak) keycloak.logout({ redirectUri: window.location.origin })
  }

  const value = {
    authEnabled: AUTH_ENABLED,
    status,
    user,
    username: user?.username || user?.user_id || null,
    permissions,
    roles,
    instances,
    hasPermission,
    hasRole,
    logout,
  }

  if (AUTH_ENABLED) {
    if (status === 'loading') {
      return <AuthScreen title="Signing in…" message="Redirecting to the identity provider." />
    }
    if (status === 'error') {
      return (
        <AuthScreen
          title="Sign-in failed"
          message="We could not verify your session. Please try signing in again."
          action={<ScreenButton onClick={() => getKeycloak()?.login()}>Retry sign-in</ScreenButton>}
        />
      )
    }
    // Signed in, but the account has no permissions for this console.
    if (permissions.length === 0) {
      return (
        <AuthContext.Provider value={value}>
          <AuthScreen
            title="Access not granted"
            message={`Signed in as ${value.username || 'this account'}, but you have no permissions for this console. Contact an administrator.`}
            action={<ScreenButton onClick={logout}>Sign out</ScreenButton>}
          />
        </AuthContext.Provider>
      )
    }
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
