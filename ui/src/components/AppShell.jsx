import React from 'react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'
import { styles } from '../styles/appStyles'

const navItems = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/new', label: 'New Document', permission: 'upload' },
  { to: '/search', label: 'Search', permission: 'search' },
  { to: '/settings', label: 'Settings', permission: 'admin' },
  { to: '/audit', label: 'Audit Log', permission: 'search' }
]

export default function AppShell({ children }) {
  const { authEnabled, username, hasPermission, logout } = useAuth()
  const visibleNavItems = navItems.filter(item => !item.permission || hasPermission(item.permission))

  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <div style={styles.headerInner}>
          <div style={styles.brandBlock}>
            <h1 style={styles.brandTitle}>Document Ingestion Pipeline</h1>
            <p style={styles.brandSubtitle}>Review workflows, inspect artifacts, and manage document indexing.</p>
          </div>
          <nav style={styles.nav}>
            {visibleNavItems.map(item => (
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
            {authEnabled && (
              <span style={{ display: 'flex', alignItems: 'center', gap: '10px', marginLeft: '8px' }}>
                {username && <span style={{ fontSize: '13px', color: '#cbd5e1' }}>{username}</span>}
                <button
                  onClick={logout}
                  style={{
                    padding: '6px 12px',
                    borderRadius: '6px',
                    border: '1px solid rgba(255,255,255,0.3)',
                    background: 'transparent',
                    color: 'white',
                    fontSize: '13px',
                    fontWeight: 500,
                    cursor: 'pointer'
                  }}
                >
                  Logout
                </button>
              </span>
            )}
          </nav>
        </div>
      </header>
      <main>{children}</main>
    </div>
  )
}
