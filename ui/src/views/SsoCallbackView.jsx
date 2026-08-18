import { useEffect, useState } from 'react'

import { AuthLoadingScreen } from '../components/AuthLoadingScreen'
import { appPath } from '../basePath'
import {
  applyBackendSession,
  exchangeSsoCode,
  getAuthErrorMessage,
  getKeycloakSsoCallbackUri,
  readOAuthCallbackParams,
  ROUTES,
} from '../auth/keycloak'

/**
 * Module-level lock so React StrictMode does not run the exchange twice — an
 * authorization code is single-use, and the second attempt would fail and wipe
 * the session the first one just established.
 */
let ssoCallbackPromise = null

async function completeSsoOnce(onStatus) {
  const { error, errorDescription, code, state } = readOAuthCallbackParams()

  if (error) {
    onStatus(getAuthErrorMessage(error, errorDescription))
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

  onStatus('Exchanging sign-in code…')

  try {
    // The backend holds the client secret, so it does the exchange for us.
    const tokens = await exchangeSsoCode(code, { state })
    applyBackendSession(tokens)
    onStatus('Signed in — opening dashboard…')
    // Full page load so AuthProvider restores the persisted session cleanly.
    window.location.replace(appPath(ROUTES.HOME))
  } catch (callbackError) {
    console.error('SSO code exchange failed:', callbackError, window.location.href)
    const detail =
      callbackError instanceof Error
        ? callbackError.message
        : typeof callbackError === 'string'
          ? callbackError
          : 'Unable to complete sign-in.'
    onStatus(detail)
    window.setTimeout(() => {
      window.location.replace(`${appPath(ROUTES.LOGIN)}?sso_error=1`)
    }, 2500)
  }
}

/**
 * OAuth return page after Keycloak / Google sign-in (full-page redirect flow).
 * Hands the code to the backend, persists the tokens, then goes to dashboard.
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
