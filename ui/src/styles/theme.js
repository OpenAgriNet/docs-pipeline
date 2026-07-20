import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'

const THEME_STORAGE_KEY = 'docs-pipeline-theme'

const themeOptions = [
  { value: 'warm', label: 'Warm' },
  { value: 'cool', label: 'Cool' },
  { value: 'dark', label: 'Dark' }
]

function applyTheme(themeName) {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  root.classList.remove('cool', 'dark')
  if (themeName === 'cool' || themeName === 'dark') {
    root.classList.add(themeName)
  }
  root.style.colorScheme = themeName === 'dark' ? 'dark' : 'light'
}

const ThemeContext = createContext({
  themeName: 'warm',
  setThemeName: () => {},
})

export function ThemeProvider({ children }) {
  const [themeName, setThemeName] = useState(() => {
    if (typeof window === 'undefined') return 'warm'
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY)
    return themeOptions.some(option => option.value === saved) ? saved : 'warm'
  })

  useEffect(() => {
    applyTheme(themeName)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(THEME_STORAGE_KEY, themeName)
    }
  }, [themeName])

  const value = useMemo(() => ({ themeName, setThemeName }), [themeName])

  return React.createElement(ThemeContext.Provider, { value }, children)
}

export function useTheme() {
  return useContext(ThemeContext)
}

export function getThemeOptions() {
  return themeOptions
}
