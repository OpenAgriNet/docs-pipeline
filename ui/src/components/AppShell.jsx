import React from 'react'
import { NavLink } from 'react-router-dom'
import { styles } from '../styles/appStyles'

const navItems = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/new', label: 'Ingest' },
  { to: '/search', label: 'Search Lab' },
  { to: '/settings', label: 'Settings' },
  { to: '/audit', label: 'Audit' }
]

export default function AppShell({ children }) {
  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <div style={styles.headerInner}>
          <div style={styles.brandBlock}>
            <span style={styles.brandEyebrow}>docs Pipeline Console</span>
            <h1 style={styles.brandTitle}>Docs Pipeline Ops</h1>
            <p style={styles.brandSubtitle}>Workflow review, artifact access, and Marqo search operations in one surface.</p>
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
            <span style={styles.navKicker}>Temporal + MinIO + Marqo</span>
          </nav>
        </div>
      </header>
      <main>{children}</main>
    </div>
  )
}
