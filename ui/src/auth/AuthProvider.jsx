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

// sessionStorage flag used by the redirect-loop breaker. It is set right before
// the first login redirect and cleared once we are genuinely authenticated. If
// we come back from the IdP carrying an OIDC callback but the adapter still
// reports us as unauthenticated (e.g. an adapter that cannot consume the code),
// this flag lets us show the error card ONCE instead of bouncing back to login
// forever.
const LOGIN_ATTEMPT_KEY = 'kc-login-attempt'

// True when the current URL fragment/query already carries an OIDC auth-code
// callback (or an error) from the identity provider.
function hasOidcCallback() {
  const haystack = `${window.location.hash || ''}${window.location.search || ''}`
  return /(?:^|[#&?])(code|error)=/.test(haystack)
}

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

    // Set the loop-breaker flag right before every explicit login redirect we
    // trigger ourselves (the !authenticated branch below and 401/refresh-fail
    // re-logins). We deliberately do NOT set it for onLoad:'login-required's own
    // initial redirect on a cold load — that first bounce is always legitimate.
    const login = () => {
      sessionStorage.setItem(LOGIN_ATTEMPT_KEY, '1')
      keycloak.login()
    }
    setUnauthorizedHandler(login)

    // Redirect-loop breaker: if we have come back from the IdP carrying an OIDC
    // callback AND we already flagged a login attempt, then a previous init()
    // failed to consume the code and bounced us to login — landing us right back
    // here. Break the cycle: show the error card once instead of looping.
    if (hasOidcCallback() && sessionStorage.getItem(LOGIN_ATTEMPT_KEY)) {
      setStatus('error')
      return
    }

    keycloak
      .init({
        onLoad: 'login-required',
        pkceMethod: 'S256',
        checkLoginIframe: false,
        silentCheckSsoRedirectUri: SILENT_SSO_URI,
        // Surface adapter errors in the browser console. Off by default in
        // keycloak-js, which is why a callback it cannot process failed silently.
        enableLogging: true,
      })
      .then(async (authenticated) => {
        if (!authenticated) {
          login()
          return
        }
        // Genuinely signed in — the code was consumed. Clear the loop flag so a
        // future failure starts from a clean slate.
        sessionStorage.removeItem(LOGIN_ATTEMPT_KEY)
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

  // Control-plane super-admin: manages tenants only, holds no data permissions.
  // Recognised so a pure platform admin is not treated as "no access" and lands
  // on the Tenants console instead of an empty data dashboard.
  const isPlatformAdmin = !AUTH_ENABLED || roles.includes('master_admin')

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
    isPlatformAdmin,
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
          action={<ScreenButton onClick={() => {
            // Clear the loop-breaker flag so the retry is a genuine fresh
            // attempt rather than being short-circuited straight back to error.
            sessionStorage.removeItem(LOGIN_ATTEMPT_KEY)
            getKeycloak()?.login()
          }}>Retry sign-in</ScreenButton>}
        />
      )
    }
    // Signed in, but the account has no permissions AND is not a control-plane
    // platform admin — genuinely nothing to show. A pure master_admin (no data
    // permissions) is allowed through: it lands on the Tenants console.
    if (permissions.length === 0 && !isPlatformAdmin) {
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
