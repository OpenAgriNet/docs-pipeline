import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'

import { AuthLoadingScreen, InlineSpinner } from '../components/AuthLoadingScreen'
import { HeroPanel } from '../components/HeroPanel'
import { PlatformLogoIcon } from '../components/PlatformLogoIcon'
import { Button } from '../components/ui/button'
import { useAuth } from '../auth/AuthProvider'
import { AUTH_ENABLED, ROUTES } from '../auth/keycloak'
import { APP_DESCRIPTION, APP_NAME } from '../lib/app-brand'

/** Google's brand mark — kept as the official four-colour glyph. */
function GoogleIcon({ className }) {
  return (
    <svg viewBox="0 0 24 24" className={className ?? 'size-5'} aria-hidden="true">
      <path
        fill="#4285F4"
        d="M23.52 12.27c0-.82-.07-1.6-.21-2.36H12v4.47h6.46a5.52 5.52 0 0 1-2.4 3.62v3h3.87c2.27-2.09 3.58-5.17 3.58-8.73Z"
      />
      <path
        fill="#34A853"
        d="M12 24c3.24 0 5.96-1.08 7.94-2.91l-3.88-3c-1.07.72-2.45 1.15-4.06 1.15-3.13 0-5.78-2.11-6.73-4.95H1.26v3.09A12 12 0 0 0 12 24Z"
      />
      <path
        fill="#FBBC05"
        d="M5.27 14.29a7.2 7.2 0 0 1 0-4.58V6.62H1.26a12 12 0 0 0 0 10.76l4.01-3.09Z"
      />
      <path
        fill="#EA4335"
        d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.44-3.44C17.95 1.18 15.23 0 12 0A12 12 0 0 0 1.26 6.62l4.01 3.09C6.22 6.86 8.87 4.75 12 4.75Z"
      />
    </svg>
  )
}

function MailIcon({ className }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className ?? 'size-5'} aria-hidden="true">
      <rect x="3" y="5" width="18" height="14" rx="2.5" stroke="currentColor" strokeWidth="1.75" />
      <path d="m4 7.5 7.1 5a1.6 1.6 0 0 0 1.8 0l7.1-5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  )
}

function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
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

  const [email, setEmail] = useState('')
  const [emailError, setEmailError] = useState('')
  const [pendingProvider, setPendingProvider] = useState(null) // 'google' | 'email'

  // Still restoring session from storage — never flash the login form.
  if (AUTH_ENABLED && (!bootstrapped || isInitializing)) {
    return <AuthLoadingScreen title="Welcome back…" message="Restoring your session" />
  }

  if (AUTH_ENABLED && isAuthenticated) {
    return <Navigate to={ROUTES.HOME} replace />
  }

  if (isSsoLoading) {
    return (
      <AuthLoadingScreen
        title={pendingProvider === 'email' ? 'Continuing…' : 'Continuing with Google…'}
        message="Redirecting to your identity provider"
      />
    )
  }

  const handleSsoClick = async () => {
    setPendingProvider('google')
    const ok = await loginWithSso()
    if (ok) navigate(ROUTES.HOME, { replace: true })
  }

  // Email login also goes through Keycloak's hosted page (no idp hint, so it
  // shows the username/password + email-OTP form) rather than a REST call:
  // the email-OTP plugin only supports OTP delivery inside a real browser
  // session, not a headless/direct-grant API call.
  const handleEmailClick = async (event) => {
    event?.preventDefault()
    if (!isValidEmail(email)) {
      setEmailError('Enter a valid email address.')
      return
    }
    setEmailError('')
    setPendingProvider('email')
    const ok = await loginWithSso({ idpHint: '', loginHint: email.trim().toLowerCase() })
    if (ok) navigate(ROUTES.HOME, { replace: true })
  }

  const errorText = emailError || authError

  return (
    <div className="grid min-h-svh lg:grid-cols-[1fr_minmax(0,40rem)]">
      <HeroPanel />

      {/* One centred column shared by brand, form and footer, so all three keep
          the same left and right edge and the panel's padding stays symmetric. */}
      <main className="flex min-h-svh w-full flex-col bg-canopy-form-panel px-6 text-canopy-form-text sm:px-10">
        <div className="mx-auto flex w-full max-w-[26rem] flex-1 flex-col">
          {/* Brand sits inline at the top on small screens, where the hero
              panel is hidden and would otherwise leave it unbranded. */}
          <header className="flex items-center gap-3 py-8 lg:hidden">
            <PlatformLogoIcon className="size-9 rounded-xl shadow-sm" title={APP_NAME} />
            <span className="text-sm font-semibold tracking-tight">{APP_NAME}</span>
          </header>

          <div className="flex flex-1 flex-col justify-center py-10">
            <h1 className="text-[2.125rem] font-semibold leading-[1.15] tracking-tight text-canopy-form-text">
              Sign in
            </h1>
            <p className="mt-3 text-[0.9375rem] leading-relaxed text-canopy-form-muted">
              {APP_DESCRIPTION}
            </p>

            {errorText ? (
              <div
                role="alert"
                className="mt-6 border-l-2 border-red-500 bg-red-50/60 py-2.5 pl-4 pr-3 text-sm leading-relaxed text-red-700"
              >
                {errorText}
              </div>
            ) : null}

            <div className="mt-10">
              <Button
                type="button"
                size="lg"
                disabled={isSsoLoading}
                onClick={() => void handleSsoClick()}
                className="h-[3.25rem] w-full gap-3 rounded-lg border border-canopy-form-border bg-white text-[0.9375rem] font-medium text-[#3c4043] shadow-sm transition-colors hover:bg-neutral-50 disabled:opacity-70"
              >
                {isSsoLoading && pendingProvider === 'google' ? <InlineSpinner /> : <GoogleIcon className="size-5" />}
                {isSsoLoading && pendingProvider === 'google' ? 'Redirecting…' : 'Continue with Google'}
              </Button>

              <div className="my-7 flex items-center gap-4" aria-hidden="true">
                <span className="h-px flex-1 bg-canopy-form-border" />
                <span className="text-[0.6875rem] font-medium uppercase tracking-[0.14em] text-canopy-form-muted">
                  or
                </span>
                <span className="h-px flex-1 bg-canopy-form-border" />
              </div>

              <form onSubmit={handleEmailClick}>
                <label
                  htmlFor="login-email"
                  className="mb-2 block text-sm font-medium text-canopy-form-text"
                >
                  Work email
                </label>
                <div className="relative">
                  <MailIcon className="pointer-events-none absolute left-4 top-1/2 size-5 -translate-y-1/2 text-canopy-form-muted" />
                  <input
                    id="login-email"
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    placeholder="name@example.com"
                    value={email}
                    onChange={e => { setEmail(e.target.value); setEmailError('') }}
                    disabled={isSsoLoading}
                    className="h-[3.25rem] w-full rounded-lg border border-canopy-form-border bg-canopy-form-card pl-12 pr-4 text-[0.9375rem] text-canopy-form-text outline-none transition-colors placeholder:text-canopy-form-muted/60 focus:border-canopy-form-accent focus:ring-2 focus:ring-canopy-form-accent/20 disabled:opacity-70"
                  />
                </div>
                <Button
                  type="submit"
                  size="lg"
                  disabled={isSsoLoading || !email.trim()}
                  className="mt-4 h-[3.25rem] w-full gap-2 rounded-lg bg-canopy-form-accent text-[0.9375rem] font-medium text-canopy-form-accent-foreground shadow-sm transition-colors hover:bg-canopy-form-accent-hover disabled:opacity-60"
                >
                  {isSsoLoading && pendingProvider === 'email' ? <InlineSpinner className="text-canopy-form-accent-foreground" /> : null}
                  {isSsoLoading && pendingProvider === 'email' ? 'Continuing…' : 'Continue with email code'}
                </Button>
              </form>
            </div>
          </div>

          <footer className="py-8 text-xs leading-relaxed text-canopy-form-muted">
            Secured by your organization’s identity provider
          </footer>
        </div>
      </main>
    </div>
  )
}
