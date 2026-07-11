import React from 'react'
import { NavLink as RouterNavLink } from 'react-router-dom'
import { cn } from '../lib/utils'

export const NavLink = React.forwardRef(function NavLink(
  { className, activeClassName, pendingClassName, to, ...props },
  ref
) {
  return (
    <RouterNavLink
      ref={ref}
      to={to}
      className={({ isActive, isPending }) => cn(className, isActive && activeClassName, isPending && pendingClassName)}
      {...props}
    />
  )
})
