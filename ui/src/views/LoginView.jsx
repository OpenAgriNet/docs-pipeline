import { useEffect, useRef, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'

import { AuthLoadingScreen, InlineSpinner } from '../components/AuthLoadingScreen'
import { HeroPanel } from '../components/HeroPanel'
import { PlatformLogoIcon } from '../components/PlatformLogoIcon'
import { Button } from '../components/ui/button'
import { useAuth } from '../auth/AuthProvider'
import { AUTH_ENABLED, ROUTES } from '../auth/keycloak'
import { API_BASE } from '../config'
import { APP_DESCRIPTION, APP_NAME } from '../lib/app-brand'

const OTP_LENGTH = 6
/** Fallback only — the send response carries the real cooldown. */
const RESEND_SECONDS = 30

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

function BackArrow({ className }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className ?? 'size-4'} aria-hidden="true">
      <path d="M15 5l-7 7 7 7" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
}

/** Unauthenticated POST — the session does not exist yet, so no bearer token. */
async function postJson(path, body) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const isJson = response.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await response.json() : null
  if (!response.ok) {
    const detail = data?.detail
    throw new Error(
      typeof detail === 'string' ? detail : `Request failed (${response.status})`,
    )
  }
  return data
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
    loginWithOtpTokens,
  } = useAuth()

  const [step, setStep] = useState('choose') // choose | code
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [otpBusy, setOtpBusy] = useState(false)
  const [otpError, setOtpError] = useState('')
  const [secondsLeft, setSecondsLeft] = useState(0)
  const codeInputRef = useRef(null)

  // Resend cooldown, so a stuck email can't be hammered.
  useEffect(() => {
    if (secondsLeft <= 0) return undefined
    const timer = setTimeout(() => setSecondsLeft(s => s - 1), 1000)
    return () => clearTimeout(timer)
  }, [secondsLeft])

  useEffect(() => {
    if (step === 'code') codeInputRef.current?.focus()
  }, [step])

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
        title="Continuing with Google…"
        message="Redirecting to your identity provider"
      />
    )
  }

  const handleSsoClick = async () => {
    const ok = await loginWithSso()
    if (ok) navigate(ROUTES.HOME, { replace: true })
  }

  async function sendCode(event) {
    event?.preventDefault()
    if (!isValidEmail(email)) {
      setOtpError('Enter a valid email address.')
      return
    }
    try {
      setOtpBusy(true)
      setOtpError('')
      const sent = await postJson('/auth/otp/request', { email: email.trim().toLowerCase() })
      setStep('code')
      setCode('')
      // Backend owns the cooldown; resending earlier is a silent no-op there.
      setSecondsLeft(Number(sent?.resend_after_seconds) || RESEND_SECONDS)
    } catch (err) {
      setOtpError(err.message)
    } finally {
      setOtpBusy(false)
    }
  }

  async function verifyCode(event) {
    event?.preventDefault()
    if (code.length !== OTP_LENGTH) {
      setOtpError(`Enter the ${OTP_LENGTH}-digit code.`)
      return
    }
    try {
      setOtpBusy(true)
      setOtpError('')
      const tokens = await postJson('/auth/otp/verify', {
        email: email.trim().toLowerCase(),
        code,
      })
      // Same token set SSO produces, so the session behaves identically from here.
      await loginWithOtpTokens({
        token: tokens.access_token,
        refreshToken: tokens.refresh_token,
        idToken: tokens.id_token,
      })
      navigate(ROUTES.HOME, { replace: true })
    } catch (err) {
      setOtpError(err.message)
      setCode('')
      codeInputRef.current?.focus()
    } finally {
      setOtpBusy(false)
    }
  }

  const errorText = otpError || authError
  const emailLabel = email.trim().toLowerCase()

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
            {step === 'code' ? (
              <button
                type="button"
                onClick={() => { setStep('choose'); setCode(''); setOtpError('') }}
                className="mb-8 -ml-1 inline-flex items-center gap-1.5 text-sm font-medium text-canopy-form-muted transition-colors hover:text-canopy-form-text"
              >
                <BackArrow className="size-4" />
                Back
              </button>
            ) : null}

            <h1 className="text-[2.125rem] font-semibold leading-[1.15] tracking-tight text-canopy-form-text">
              {step === 'code' ? 'Check your email' : 'Sign in'}
            </h1>
            <p className="mt-3 text-[0.9375rem] leading-relaxed text-canopy-form-muted">
              {step === 'code' ? (
                <>
                  We sent a {OTP_LENGTH}-digit code to{' '}
                  <span className="font-medium text-canopy-form-text">{emailLabel}</span>. It
                  expires in a few minutes.
                </>
              ) : (
                APP_DESCRIPTION
              )}
            </p>

            {errorText ? (
              <div
                role="alert"
                className="mt-6 border-l-2 border-red-500 bg-red-50/60 py-2.5 pl-4 pr-3 text-sm leading-relaxed text-red-700"
              >
                {errorText}
              </div>
            ) : null}

            {step === 'choose' ? (
              <div className="mt-10">
                <Button
                  type="button"
                  size="lg"
                  disabled={isSsoLoading || otpBusy}
                  onClick={() => void handleSsoClick()}
                  className="h-[3.25rem] w-full gap-3 rounded-lg border border-canopy-form-border bg-white text-[0.9375rem] font-medium text-[#3c4043] shadow-sm transition-colors hover:bg-neutral-50 disabled:opacity-70"
                >
                  {isSsoLoading ? <InlineSpinner /> : <GoogleIcon className="size-5" />}
                  {isSsoLoading ? 'Redirecting…' : 'Continue with Google'}
                </Button>

                <div className="my-7 flex items-center gap-4" aria-hidden="true">
                  <span className="h-px flex-1 bg-canopy-form-border" />
                  <span className="text-[0.6875rem] font-medium uppercase tracking-[0.14em] text-canopy-form-muted">
                    or
                  </span>
                  <span className="h-px flex-1 bg-canopy-form-border" />
                </div>

                <form onSubmit={sendCode}>
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
                      onChange={e => { setEmail(e.target.value); setOtpError('') }}
                      disabled={otpBusy}
                      className="h-[3.25rem] w-full rounded-lg border border-canopy-form-border bg-canopy-form-card pl-12 pr-4 text-[0.9375rem] text-canopy-form-text outline-none transition-colors placeholder:text-canopy-form-muted/60 focus:border-canopy-form-accent focus:ring-2 focus:ring-canopy-form-accent/20 disabled:opacity-70"
                    />
                  </div>
                  <Button
                    type="submit"
                    size="lg"
                    disabled={otpBusy || !email.trim()}
                    className="mt-4 h-[3.25rem] w-full gap-2 rounded-lg bg-canopy-form-accent text-[0.9375rem] font-medium text-canopy-form-accent-foreground shadow-sm transition-colors hover:bg-canopy-form-accent-hover disabled:opacity-60"
                  >
                    {otpBusy ? <InlineSpinner className="text-canopy-form-accent-foreground" /> : null}
                    {otpBusy ? 'Sending code…' : 'Email me a login code'}
                  </Button>
                </form>
              </div>
            ) : (
              <form onSubmit={verifyCode} className="mt-10">
                <label htmlFor="login-otp" className="sr-only">
                  {OTP_LENGTH}-digit code
                </label>
                <input
                  id="login-otp"
                  ref={codeInputRef}
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={OTP_LENGTH}
                  placeholder="000000"
                  value={code}
                  onChange={e => {
                    setCode(e.target.value.replace(/\D/g, '').slice(0, OTP_LENGTH))
                    setOtpError('')
                  }}
                  disabled={otpBusy}
                  className="h-16 w-full rounded-lg border border-canopy-form-border bg-canopy-form-card text-center font-mono text-[1.75rem] tracking-[0.45em] text-canopy-form-text outline-none transition-colors placeholder:text-canopy-form-muted/30 focus:border-canopy-form-accent focus:ring-2 focus:ring-canopy-form-accent/20 disabled:opacity-70"
                />

                <Button
                  type="submit"
                  size="lg"
                  disabled={otpBusy || code.length !== OTP_LENGTH}
                  className="mt-4 h-[3.25rem] w-full gap-2 rounded-lg bg-canopy-form-accent text-[0.9375rem] font-medium text-canopy-form-accent-foreground shadow-sm transition-colors hover:bg-canopy-form-accent-hover disabled:opacity-60"
                >
                  {otpBusy ? <InlineSpinner className="text-canopy-form-accent-foreground" /> : null}
                  {otpBusy ? 'Verifying…' : 'Verify and sign in'}
                </Button>

                <p className="mt-5 text-sm text-canopy-form-muted">
                  Didn’t get it?{' '}
                  <button
                    type="button"
                    disabled={secondsLeft > 0 || otpBusy}
                    onClick={() => void sendCode()}
                    className="font-medium text-canopy-form-accent underline-offset-4 hover:underline disabled:text-canopy-form-muted disabled:no-underline"
                  >
                    {secondsLeft > 0 ? `Resend in ${secondsLeft}s` : 'Resend code'}
                  </button>
                </p>
              </form>
            )}
          </div>

          <footer className="py-8 text-xs leading-relaxed text-canopy-form-muted">
            Secured by your organization’s identity provider
          </footer>
        </div>
      </main>
    </div>
  )
}
