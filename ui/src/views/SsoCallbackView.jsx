import { useEffect, useState } from 'react'

import {
  getAuthErrorMessage,
  getKeycloak,
  getKeycloakSsoCallbackUri,
  persistSession,
  ROUTES,
  setCurrentToken,
} from '../auth/keycloak'

/** Read OAuth params from query or hash (keycloak-js defaults to fragment mode). */
function readOAuthParams() {
  const url = new URL(window.location.href)
  const hash = url.hash.startsWith('#') ? url.hash.slice(1) : url.hash
  const fromHash = new URLSearchParams(hash)

  const get = (key) => url.searchParams.get(key) || fromHash.get(key) || null

  return {
    error: get('error'),
    errorDescription: get('error_description'),
    code: get('code'),
  }
}

/**
 * Module-level lock so React StrictMode does not run keycloak.init twice.
 */
let ssoCallbackPromise = null

async function completeSsoOnce(onStatus) {
  const keycloak = getKeycloak()
  if (!keycloak) {
    onStatus('Keycloak is not configured.')
    window.setTimeout(() => {
      window.location.replace(ROUTES.LOGIN)
    }, 1200)
    return
  }

  const { error, errorDescription, code } = readOAuthParams()

  if (error) {
    const msg = getAuthErrorMessage(error, errorDescription)
    onStatus(msg)
    window.setTimeout(() => {
      window.location.replace(`${ROUTES.LOGIN}?sso_error=1`)
    }, 1500)
    return
  }

  if (!code && !window.location.hash.includes('code=') && !window.location.search.includes('code=')) {
    console.warn('SSO callback: no authorization code in URL', window.location.href)
  }

  try {
    let authenticated = false

    if (keycloak.didInitialize) {
      authenticated = Boolean(keycloak.authenticated && keycloak.token)
    } else {
      authenticated = await keycloak.init({
        pkceMethod: 'S256',
        checkLoginIframe: false,
        flow: 'standard',
        responseMode: 'fragment',
        // Must match the redirectUri used when starting login()
        redirectUri: getKeycloakSsoCallbackUri(),
      })
    }

    if (authenticated && keycloak.token) {
      setCurrentToken(keycloak.token)
      persistSession({
        token: keycloak.token,
        refreshToken: keycloak.refreshToken,
        idToken: keycloak.idToken,
      })
      onStatus('Signed in — opening dashboard…')
      // Full page: land on dashboard with persisted session.
      window.location.replace(ROUTES.HOME)
      return
    }

    onStatus(
      'Sign-in did not return an access token. Confirm Keycloak client is public, Standard flow + PKCE is on, and Valid Redirect URIs include this callback URL.',
    )
    window.setTimeout(() => {
      window.location.replace(ROUTES.LOGIN)
    }, 2000)
  } catch (callbackError) {
    console.error('SSO callback keycloak.init failed:', callbackError, window.location.href)

    if (keycloak?.token) {
      setCurrentToken(keycloak.token)
      persistSession({
        token: keycloak.token,
        refreshToken: keycloak.refreshToken,
        idToken: keycloak.idToken,
      })
      onStatus('Signed in — opening dashboard…')
      window.location.replace(ROUTES.HOME)
      return
    }

    // keycloak-js often rejects with no args when the token POST fails.
    const detail =
      callbackError == null
        ? `Token exchange failed for redirect ${getKeycloakSsoCallbackUri()}. In Keycloak client "${import.meta.env.VITE_KEYCLOAK_CLIENT_ID || 'bharat-vistaar'}" set Valid Redirect URIs to include that exact URL, Web Origins to include ${window.location.origin} (or +), Access Type = public, Standard flow + PKCE enabled.`
        : typeof callbackError === 'string'
          ? callbackError
          : callbackError instanceof Error
            ? callbackError.message
            : callbackError?.error_description ||
              callbackError?.error ||
              'Unable to complete sign-in.'

    onStatus(getAuthErrorMessage(callbackError?.error || 'token_exchange_failed', detail))
    window.setTimeout(() => {
      window.location.replace(ROUTES.LOGIN)
    }, 2500)
  }
}

/**
 * OAuth return page after Keycloak / Google sign-in (full-page redirect flow).
 */
export default function SsoCallbackView() {
  const [status, setStatus] = useState('Completing sign-in…')

  useEffect(() => {
    if (!ssoCallbackPromise) {
      ssoCallbackPromise = completeSsoOnce(setStatus)
    }
  }, [])

  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-3 bg-[#f7faf8] px-6">
      <div className="w-full max-w-md rounded-2xl border border-[#d5e0db] bg-white p-8 text-center shadow-sm">
        <div className="mx-auto mb-4 size-10 animate-pulse rounded-full bg-[#d5e0db]" />
        <p className="text-sm font-medium text-[#14201b]">{status}</p>
        <p className="mt-2 text-xs text-[#5f7269]">
          Please wait — you will be redirected automatically.
        </p>
      </div>
    </div>
  )
}
