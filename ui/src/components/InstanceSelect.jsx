import React from 'react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select'

/** Tenant picker; hidden when there are no options. */
export function InstanceSelect({
  value,
  onValueChange,
  options = [],
  placeholder = 'Select tenant',
  className = 'h-9 w-56',
}) {
  if (!options.length) return null
  return (
    <Select value={value} onValueChange={onValueChange}>
      <SelectTrigger className={className}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((inst) => (
          <SelectItem key={inst} value={inst} className="font-mono text-xs">
            {inst}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
