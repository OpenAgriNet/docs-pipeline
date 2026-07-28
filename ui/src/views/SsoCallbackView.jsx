import { useEffect, useState } from 'react'

import { AuthLoadingScreen } from '../components/AuthLoadingScreen'
import { appPath } from '../basePath'
import {
  getAuthErrorMessage,
  getKeycloak,
  getKeycloakSsoCallbackUri,
  persistSession,
  readOAuthCallbackParams,
  resetKeycloakInit,
  ROUTES,
  setCurrentToken,
} from '../auth/keycloak'

/**
 * Module-level lock so React StrictMode does not run keycloak.init twice
 * (second init would fail and drop the session).
 */
let ssoCallbackPromise = null

async function completeSsoOnce(onStatus) {
  const keycloak = getKeycloak()
  if (!keycloak) {
    onStatus('Keycloak is not configured.')
    window.setTimeout(() => {
      window.location.replace(appPath(ROUTES.LOGIN))
    }, 1200)
    return
  }

  const { error, errorDescription, code, responseMode } = readOAuthCallbackParams()

  if (error) {
    const msg = getAuthErrorMessage(error, errorDescription)
    onStatus(msg)
    window.setTimeout(() => {
      window.location.replace(`${appPath(ROUTES.LOGIN)}?sso_error=1`)
    }, 1500)
    return
  }

  if (!code) {
    console.warn('SSO callback: no authorization code in URL', window.location.href)
    onStatus(
      'No authorization code returned from Keycloak. Check Valid Redirect URIs include ' +
        getKeycloakSsoCallbackUri(),
    )
    window.setTimeout(() => {
      window.location.replace(appPath(ROUTES.LOGIN))
    }, 2000)
    return
  }

  const redirectUri = getKeycloakSsoCallbackUri()
  onStatus('Exchanging sign-in code…')

  try {
    let authenticated = false

    if (keycloak.didInitialize && keycloak.authenticated && keycloak.token) {
      authenticated = true
    } else if (keycloak.didInitialize) {
      // Adapter was init'd earlier without the OAuth code (should be rare on this route).
      // Cannot re-init the same instance — fall through to error with guidance.
      console.warn(
        '[auth] Keycloak already initialized without tokens on callback page',
        window.location.href,
      )
      authenticated = Boolean(keycloak.token)
    } else {
      // Primary path: process ?code= / #code= and exchange for tokens.
      authenticated = await keycloak.init({
        pkceMethod: 'S256',
        checkLoginIframe: false,
        flow: 'standard',
        responseMode,
        // Must match the redirectUri used when starting login()
        redirectUri,
      })
      // Mark global init promise so AuthProvider does not re-init wrongly.
      resetKeycloakInit()
    }

    if (authenticated && keycloak.token) {
      setCurrentToken(keycloak.token)
      persistSession({
        token: keycloak.token,
        refreshToken: keycloak.refreshToken,
        idToken: keycloak.idToken,
      })
      onStatus('Signed in — opening dashboard…')
      // Full page load so AuthProvider restores the persisted session cleanly.
      window.location.replace(appPath(ROUTES.HOME))
      return
    }

    onStatus(
      'Sign-in did not return an access token. Confirm Keycloak client is public, Standard flow + PKCE is on, Web Origins includes ' +
        window.location.origin +
        ', and Valid Redirect URIs include ' +
        redirectUri,
    )
    window.setTimeout(() => {
      window.location.replace(appPath(ROUTES.LOGIN))
    }, 2500)
  } catch (callbackError) {
    console.error('SSO callback keycloak.init failed:', callbackError, window.location.href)

    // keycloak-js sometimes throws after a successful token exchange.
    if (keycloak?.token) {
      setCurrentToken(keycloak.token)
      persistSession({
        token: keycloak.token,
        refreshToken: keycloak.refreshToken,
        idToken: keycloak.idToken,
      })
      onStatus('Signed in — opening dashboard…')
      window.location.replace(appPath(ROUTES.HOME))
      return
    }

    const detail =
      callbackError == null
        ? `Token exchange failed for redirect ${redirectUri}. In Keycloak client "${import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'bharat-vistaar'}" set Valid Redirect URIs to include that exact URL, Web Origins to include ${window.location.origin} (or +), Access Type = public, Standard flow + PKCE enabled.`
        : typeof callbackError === 'string'
          ? callbackError
          : callbackError instanceof Error
            ? callbackError.message
            : callbackError?.error_description ||
              callbackError?.error ||
              'Unable to complete sign-in.'

    onStatus(getAuthErrorMessage(callbackError?.error || 'token_exchange_failed', detail))
    window.setTimeout(() => {
      window.location.replace(appPath(ROUTES.LOGIN))
    }, 2500)
  }
}

/**
 * OAuth return page after Keycloak / Google sign-in (full-page redirect flow).
 * Completes the code exchange, persists tokens, then hard-navigates to dashboard.
 */
export default function SsoCallbackView() {
  const [status, setStatus] = useState('Completing sign-in…')

  useEffect(() => {
    if (!ssoCallbackPromise) {
      ssoCallbackPromise = completeSsoOnce(setStatus).finally(() => {
        ssoCallbackPromise = null
      })
    } else {
      ssoCallbackPromise.then(() => {}).catch(() => {})
    }
  }, [])

  return (
    <AuthLoadingScreen
      title="Signing you in"
      message={status}
    />
  )
}
