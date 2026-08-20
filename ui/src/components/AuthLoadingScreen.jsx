import React from 'react'
import { PlatformLogoIcon } from './PlatformLogoIcon'
import { APP_NAME } from '../lib/app-brand'
import { cn } from '../lib/utils'

/**
 * Full-screen canopy-themed loader used during session restore and SSO redirect.
 * Keeps the login form from flashing on refresh when a session already exists.
 */
export function AuthLoadingScreen({
  title = 'Signing you in…',
  message = 'Restoring your session',
  className,
}) {
  return (
    <div
      className={cn(
        'flex min-h-svh flex-col items-center justify-center bg-canopy-form-panel px-6',
        className,
      )}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <div className="flex w-full max-w-sm flex-col items-center rounded-2xl border border-canopy-form-border bg-canopy-form-card px-8 py-10 text-center shadow-xl shadow-black/5">
        <PlatformLogoIcon className="mb-5 size-12 rounded-xl shadow-sm" title={APP_NAME} />
        <div className="mb-4 size-8 animate-spin rounded-full border-2 border-canopy-form-border border-t-canopy-form-accent" />
        <p className="text-sm font-semibold text-canopy-form-text">{title}</p>
        <p className="mt-1.5 text-xs leading-relaxed text-canopy-form-muted">{message}</p>
      </div>
    </div>
  )
}

export function InlineSpinner({ className }) {
  return (
    <span
      className={cn(
        'inline-block size-4 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent opacity-80',
        className,
      )}
      aria-hidden
    />
  )
}
