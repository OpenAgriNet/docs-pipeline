import { Eye, EyeOff } from 'lucide-react'
import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'

import { AuthLoadingScreen, InlineSpinner } from '../components/AuthLoadingScreen'
import { HeroPanel } from '../components/HeroPanel'
import { PlatformLogoIcon } from '../components/PlatformLogoIcon'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Separator } from '../components/ui/separator'
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
  const [showPassword, setShowPassword] = useState(false)
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
    <div className="flex min-h-svh">
      <HeroPanel />

      <div className="flex w-full flex-col bg-canopy-form-panel text-canopy-form-text lg:ml-auto lg:w-[54%] lg:max-w-[680px] lg:shrink-0">
        <div className="flex flex-1 flex-col justify-center px-8 py-10 lg:px-12">
          <div className="mx-auto w-full max-w-[440px]">
            <div className="mb-8">
              <div className="mb-6 flex items-center gap-3">
                <PlatformLogoIcon className="size-12 rounded-2xl shadow-sm" title={APP_NAME} />
                <span className="text-lg font-semibold tracking-tight text-canopy-form-text">
                  {APP_NAME}
                </span>
              </div>
              <h1 className="text-3xl font-semibold tracking-tight text-canopy-form-text">
                Welcome back
              </h1>
              <p className="mt-2 text-sm leading-relaxed text-canopy-form-muted">
                {APP_DESCRIPTION}
              </p>
            </div>

            <div className="space-y-6">
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
                variant="outline"
                size="lg"
                disabled={isSsoLoading}
                onClick={() => void handleSsoClick()}
                className="group h-11 w-full gap-3 rounded-xl border-[#b8c9c0] bg-[#d5e0db] text-sm font-medium text-canopy-form-text shadow-none hover:border-canopy-form-accent/45 hover:bg-[#c8d6cf] hover:!text-canopy-form-text disabled:opacity-70"
              >
                {isSsoLoading ? (
                  <InlineSpinner className="text-canopy-form-accent" />
                ) : (
                  <SsoIcon className="size-5 text-canopy-form-accent transition-colors group-hover:text-canopy-form-accent-hover" />
                )}
                {isSsoLoading ? 'Redirecting to sign-in…' : 'Continue with SSO'}
              </Button>

              <div className="relative flex items-center gap-4">
                <Separator className="flex-1 bg-canopy-form-border" />
                <span className="shrink-0 text-xs text-canopy-form-muted">
                  or sign in with email
                </span>
                <Separator className="flex-1 bg-canopy-form-border" />
              </div>

              <form
                className="space-y-5"
                onSubmit={(event) => event.preventDefault()}
              >
                <div className="space-y-2">
                  <Label htmlFor="email" className="text-sm font-medium text-canopy-form-text">
                    Email
                  </Label>
                  <Input
                    id="email"
                    type="email"
                    placeholder="Enter your email"
                    autoComplete="email"
                    className="h-11 rounded-xl border-canopy-form-border bg-canopy-form-input px-4 text-sm text-canopy-form-text shadow-none placeholder:text-canopy-form-muted focus-visible:border-canopy-form-accent/50 focus-visible:ring-canopy-form-accent/20"
                  />
                </div>

                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label htmlFor="password" className="text-sm font-medium text-canopy-form-text">
                      Password
                    </Label>
                    <button
                      type="button"
                      className="text-sm font-medium text-canopy-form-accent transition-colors hover:text-canopy-form-accent-hover"
                    >
                      Forgot password?
                    </button>
                  </div>
                  <div className="relative">
                    <Input
                      id="password"
                      type={showPassword ? 'text' : 'password'}
                      placeholder="Enter your password"
                      autoComplete="current-password"
                      className="h-11 rounded-xl border-canopy-form-border bg-canopy-form-input px-4 pr-16 text-sm text-canopy-form-text shadow-none placeholder:text-canopy-form-muted focus-visible:border-canopy-form-accent/50 focus-visible:ring-canopy-form-accent/20"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((value) => !value)}
                      aria-label={showPassword ? 'Hide password' : 'Show password'}
                      className="absolute top-1/2 right-4 -translate-y-1/2 text-canopy-form-muted transition-colors hover:text-canopy-form-text"
                    >
                      {showPassword ? (
                        <EyeOff className="size-4" />
                      ) : (
                        <Eye className="size-4" />
                      )}
                    </button>
                  </div>
                </div>

                <Button
                  type="submit"
                  size="lg"
                  className="h-11 w-full rounded-xl bg-canopy-form-accent text-sm font-medium text-canopy-form-accent-foreground shadow-sm hover:bg-canopy-form-accent-hover"
                >
                  Sign in
                </Button>
              </form>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
