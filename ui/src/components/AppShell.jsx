import React from 'react'
import { NavLink } from 'react-router-dom'
import { styles } from '../styles/appStyles'

const navItems = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/new', label: 'New Document' },
  { to: '/search', label: 'Search' },
  { to: '/settings', label: 'Settings' },
  { to: '/audit', label: 'Audit Log' }
]

export default function AppShell({ children }) {
  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <div style={styles.headerInner}>
          <div style={styles.brandBlock}>
            <h1 style={styles.brandTitle}>Document Ingestion Pipeline</h1>
            <p style={styles.brandSubtitle}>Review workflows, inspect artifacts, and manage document indexing.</p>
          </div>
          <nav style={styles.nav}>
            {navItems.map(item => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                style={({ isActive }) => ({
                  ...styles.navLink,
                  ...(isActive ? styles.navLinkActive : null)
                })}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main>{children}</main>
    </div>
  )
}
