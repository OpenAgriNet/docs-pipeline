import { Navigate, useNavigate } from 'react-router-dom'

import { AuthLoadingScreen, InlineSpinner } from '../components/AuthLoadingScreen'
import { HeroPanel } from '../components/HeroPanel'
import { PlatformLogoIcon } from '../components/PlatformLogoIcon'
import { Button } from '../components/ui/button'
import { useAuth } from '../auth/AuthProvider'
import { AUTH_ENABLED, ROUTES } from '../auth/keycloak'
import { APP_DESCRIPTION, APP_NAME } from '../lib/app-brand'

function SsoIcon({ className }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={className ?? 'size-5 text-canopy-form-muted'}
      aria-hidden="true"
    >
      <path
        d="M12 2 4 5v6c0 5.5 3.5 10 8 11 4.5-1 8-5.5 8-11V5l-8-3Z"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="11" r="2" stroke="currentColor" strokeWidth="1.75" />
      <path
        d="M12 13v3"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  )
}

export default function LoginView() {
  const navigate = useNavigate()
  const {
    isAuthenticated,
    isInitializing,
    bootstrapped,
    isSsoLoading,
    authError,
    loginWithSso,
  } = useAuth()

  // Still restoring session from storage — never flash the login form.
  if (AUTH_ENABLED && (!bootstrapped || isInitializing)) {
    return (
      <AuthLoadingScreen
        title="Welcome back…"
        message="Restoring your session"
      />
    )
  }

  // Already signed in → dashboard.
  if (AUTH_ENABLED && isAuthenticated) {
    return <Navigate to={ROUTES.HOME} replace />
  }

  // Full-page overlay while browser is about to leave for Keycloak.
  if (isSsoLoading) {
    return (
      <AuthLoadingScreen
        title="Continuing with SSO…"
        message="Redirecting to your identity provider"
      />
    )
  }

  const handleSsoClick = async () => {
    const ok = await loginWithSso()
    if (ok) {
      navigate(ROUTES.HOME, { replace: true })
    }
  }

  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <HeroPanel />

      <div className="flex w-full flex-col items-center justify-center bg-canopy-form-panel px-6 py-10 text-canopy-form-text lg:px-12">
        <div className="w-full max-w-[400px] rounded-2xl border border-canopy-form-border bg-canopy-form-card p-8 shadow-xl shadow-black/5 sm:p-10">
          <div className="mb-8 flex flex-col items-center text-center">
            <PlatformLogoIcon className="size-14 rounded-2xl shadow-sm" title={APP_NAME} />
            <span className="mt-4 text-base font-semibold tracking-tight text-canopy-form-text">
              {APP_NAME}
            </span>
            <h1 className="mt-5 text-2xl font-semibold tracking-tight text-canopy-form-text">
              Welcome back
            </h1>
            <p className="mt-2 text-sm leading-relaxed text-canopy-form-muted">
              {APP_DESCRIPTION}
            </p>
          </div>

          <div className="space-y-4">
            {authError ? (
              <div
                role="alert"
                className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm leading-relaxed text-red-700"
              >
                {authError}
              </div>
            ) : null}

            <Button
              type="button"
              size="lg"
              disabled={isSsoLoading}
              onClick={() => void handleSsoClick()}
              className="group h-12 w-full gap-3 rounded-xl bg-canopy-form-accent text-sm font-medium text-canopy-form-accent-foreground shadow-sm transition-colors hover:bg-canopy-form-accent-hover disabled:opacity-70"
            >
              {isSsoLoading ? (
                <InlineSpinner className="text-canopy-form-accent-foreground" />
              ) : (
                <SsoIcon className="size-5 text-canopy-form-accent-foreground" />
              )}
              {isSsoLoading ? 'Redirecting to sign-in…' : 'Continue with SSO'}
            </Button>
          </div>

          <p className="mt-6 text-center text-xs leading-relaxed text-canopy-form-muted">
            Secured by your organization's identity provider
          </p>
        </div>
      </div>
    </div>
  )
}
