import { APP_NAME } from '../lib/app-brand'
import { cn } from '../lib/utils'

/**
 * App mark — an abstract growth/connection glyph: three ascending bars
 * topped with a node, on a solid brand-color square. Purely geometric (no
 * literal leaf, book, or letterform) so it reads cleanly at any size.
 * Rendered as inline SVG so it stays crisp and always matches the live
 * accent palette instead of a baked-in image color.
 */
export function PlatformLogoIcon({ className, title = APP_NAME }) {
  return (
    <svg
      viewBox="0 0 40 40"
      role="img"
      aria-label={title}
      className={cn('size-10 shrink-0 rounded-xl', className)}
    >
      <title>{title}</title>
      <defs>
        <linearGradient id="platform-logo-bg" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#0f3d33" />
          <stop offset="100%" stopColor="#0f766e" />
        </linearGradient>
        <linearGradient id="platform-logo-bars" x1="20" y1="9" x2="20" y2="31" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#6ee7b7" />
          <stop offset="100%" stopColor="#059669" />
        </linearGradient>
      </defs>
      <rect width="40" height="40" rx="9" fill="url(#platform-logo-bg)" />
      {/* Three ascending bars — growth, abstracted */}
      <rect x="9" y="21" width="6" height="10" rx="2.2" fill="url(#platform-logo-bars)" opacity="0.75" />
      <rect x="17" y="16" width="6" height="15" rx="2.2" fill="url(#platform-logo-bars)" opacity="0.9" />
      <rect x="25" y="10" width="6" height="21" rx="2.2" fill="url(#platform-logo-bars)" />
      {/* Node — connection point above the tallest bar */}
      <circle cx="28" cy="8" r="2.4" fill="#ecfdf5" />
    </svg>
  )
}
