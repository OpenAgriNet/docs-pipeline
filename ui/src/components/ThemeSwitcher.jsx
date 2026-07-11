import React from 'react'
import { Moon, Snowflake, Sun } from 'lucide-react'
import { Button } from './ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from './ui/dropdown-menu'
import { getThemeOptions, useTheme } from '../styles/theme'

const themeIconMap = {
  warm: Sun,
  cool: Snowflake,
  dark: Moon,
}

export function ThemeSwitcher() {
  const { themeName, setThemeName } = useTheme()
  const options = getThemeOptions()
  const CurrentIcon = themeIconMap[themeName] || Sun

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="h-8 w-8">
          <CurrentIcon className="h-4 w-4" />
          <span className="sr-only">Switch theme</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {options.map(option => {
          const Icon = themeIconMap[option.value] || Sun
          return (
            <DropdownMenuItem
              key={option.value}
              onClick={() => setThemeName(option.value)}
              className={themeName === option.value ? 'font-medium bg-accent' : ''}
            >
              <Icon className="h-4 w-4 mr-2" />
              {option.label}
            </DropdownMenuItem>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
