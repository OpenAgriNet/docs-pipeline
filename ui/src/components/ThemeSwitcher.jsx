import React from 'react'
import { Moon, Sun } from 'lucide-react'
import { useTheme } from '../styles/theme'
import { cn } from '../lib/utils'

/**
 * Compact light / dark toggle for the app header.
 */
export function ThemeSwitcher({ className }) {
  const { themeName, setThemeName, isDark } = useTheme()

  return (
    <div
      role="group"
      aria-label="Color theme"
      className={cn(
        'inline-flex items-center rounded-full border border-border bg-muted/50 p-0.5 shadow-sm',
        className,
      )}
    >
      <button
        type="button"
        onClick={() => setThemeName('light')}
        aria-pressed={!isDark}
        title="Light"
        className={cn(
          'inline-flex size-8 items-center justify-center rounded-full transition-all',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1',
          !isDark
            ? 'bg-card text-primary shadow-sm ring-1 ring-border'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <Sun className="size-3.5" strokeWidth={1.9} />
        <span className="sr-only">Light theme</span>
      </button>
      <button
        type="button"
        onClick={() => setThemeName('dark')}
        aria-pressed={isDark}
        title="Dark"
        className={cn(
          'inline-flex size-8 items-center justify-center rounded-full transition-all',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1',
          isDark
            ? 'bg-card text-primary shadow-sm ring-1 ring-border'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <Moon className="size-3.5" strokeWidth={1.9} />
        <span className="sr-only">Dark theme</span>
      </button>
      <span className="sr-only">Current: {themeName}</span>
    </div>
  )
}
