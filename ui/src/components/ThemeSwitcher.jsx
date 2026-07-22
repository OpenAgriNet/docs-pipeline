import React from 'react'
import { Leaf, Moon, Trees } from 'lucide-react'
import { Button } from './ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'
import { getThemeOptions, useTheme } from '../styles/theme'
import { cn } from '../lib/utils'

const themeIconMap = {
  warm: Leaf,
  cool: Trees,
  dark: Moon,
}

export function ThemeSwitcher() {
  const { themeName, setThemeName } = useTheme()
  const options = getThemeOptions()
  const CurrentIcon = themeIconMap[themeName] || Leaf

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 text-[#5f7269] hover:bg-[#f7faf8] hover:text-[#14201b]"
        >
          <CurrentIcon className="h-4 w-4" strokeWidth={1.75} />
          <span className="sr-only">Switch theme</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="rounded-xl border border-[#d5e0db] bg-white p-1 shadow-lg"
      >
        {options.map((option) => {
          const Icon = themeIconMap[option.value] || Leaf
          const active = themeName === option.value
          return (
            <DropdownMenuItem
              key={option.value}
              onClick={() => setThemeName(option.value)}
              className={cn(
                'cursor-pointer rounded-lg text-sm text-[#14201b]',
                'focus:bg-[#d5e0db]/70 focus:text-[#14201b]',
                active && 'bg-[#d5e0db] font-medium',
              )}
            >
              <Icon className="mr-2 h-4 w-4 text-[#5f7269]" />
              {option.label}
            </DropdownMenuItem>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
