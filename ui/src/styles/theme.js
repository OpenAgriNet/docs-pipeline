import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

const THEME_STORAGE_KEY = 'docs-pipeline-theme'

/** Only two themes: light (login canopy) and dark. */
const themeOptions = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

const LEGACY_THEME_MAP = {
  warm: 'light',
  cool: 'light',
  canopy: 'light',
  mint: 'light',
  light: 'light',
  dark: 'dark',
}

function normalizeTheme(value) {
  if (!value) return 'light'
  const mapped = LEGACY_THEME_MAP[String(value).toLowerCase()]
  return mapped === 'dark' ? 'dark' : 'light'
}

function applyTheme(themeName) {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  const theme = normalizeTheme(themeName)
  root.classList.remove('cool', 'dark', 'light')
  if (theme === 'dark') {
    root.classList.add('dark')
  }
  root.style.colorScheme = theme === 'dark' ? 'dark' : 'light'
  root.dataset.theme = theme
}

const ThemeContext = createContext({
  themeName: 'light',
  setThemeName: () => {},
  toggleTheme: () => {},
  isDark: false,
})

export function ThemeProvider({ children }) {
  const [themeName, setThemeNameState] = useState(() => {
    if (typeof window === 'undefined') return 'light'
    return normalizeTheme(window.localStorage.getItem(THEME_STORAGE_KEY))
  })

  const setThemeName = useCallback((next) => {
    setThemeNameState(normalizeTheme(next))
  }, [])

  const toggleTheme = useCallback(() => {
    setThemeNameState((current) => (current === 'dark' ? 'light' : 'dark'))
  }, [])

  useEffect(() => {
    applyTheme(themeName)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(THEME_STORAGE_KEY, themeName)
    }
  }, [themeName])

  const value = useMemo(
    () => ({
      themeName,
      setThemeName,
      toggleTheme,
      isDark: themeName === 'dark',
    }),
    [themeName, setThemeName, toggleTheme],
  )

  return React.createElement(ThemeContext.Provider, { value }, children)
}

export function useTheme() {
  return useContext(ThemeContext)
}

export function getThemeOptions() {
  return themeOptions
}
