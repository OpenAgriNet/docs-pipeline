import React from 'react'
import { GlobalAuditLogPanel } from '../components/AuditPanels'
import { styles } from '../styles/appStyles'

export default function GlobalAuditView() {
  return (
    <div style={styles.container}>
      <section style={styles.pageHero}>
        <h2 style={styles.pageHeroTitle}>Global change journal</h2>
        <p style={styles.pageHeroText}>Trace document edits, approvals, resets, and stage transitions across the full repository-backed pipeline state.</p>
      </section>
      <GlobalAuditLogPanel />
    </div>
  )
}
