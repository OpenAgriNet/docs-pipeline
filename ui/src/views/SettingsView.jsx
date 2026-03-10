import React, { useEffect, useState } from 'react'
import { API_BASE } from '../config'
import { DEFAULT_SEARCH_SETTINGS, styles } from '../styles/appStyles'

export default function SettingsView() {
  const [settings, setSettings] = useState(DEFAULT_SEARCH_SETTINGS)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [auditLogs, setAuditLogs] = useState([])
  const [auditLoading, setAuditLoading] = useState(true)

  useEffect(() => {
    fetchSettings()
    fetchAuditLogs()
  }, [])

  async function fetchSettings() {
    try {
      const response = await fetch(`${API_BASE}/settings/search`)
      if (response.ok) setSettings(await response.json())
    } catch (error) {
      console.error('Failed to fetch settings:', error)
    } finally {
      setLoading(false)
    }
  }

  async function fetchAuditLogs() {
    try {
      const response = await fetch(`${API_BASE}/settings/search/audit?limit=20`)
      if (response.ok) {
        const data = await response.json()
        setAuditLogs(data.logs || [])
      }
    } catch (error) {
      console.error('Failed to fetch settings audit logs:', error)
    } finally {
      setAuditLoading(false)
    }
  }

  function updateSetting(key, value) {
    setSettings(prev => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  async function saveSettings() {
    setSaving(true)
    try {
      const response = await fetch(`${API_BASE}/settings/search`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
      })
      if (response.ok) {
        setSaved(true)
        fetchAuditLogs()
        setTimeout(() => setSaved(false), 3000)
      }
    } catch (error) {
      console.error('Failed to save settings:', error)
    } finally {
      setSaving(false)
    }
  }

  async function resetSettings() {
    setSaving(true)
    try {
      const response = await fetch(`${API_BASE}/settings/search/reset`, { method: 'POST' })
      if (response.ok) {
        setSettings(await response.json())
        setSaved(true)
        fetchAuditLogs()
        setTimeout(() => setSaved(false), 3000)
      }
    } catch (error) {
      console.error('Failed to reset settings:', error)
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div style={styles.container}><div style={styles.card}><p>Loading settings...</p></div></div>

  return (
    <div style={styles.container}>
      <section style={styles.pageHero}>
        <h2 style={styles.pageHeroTitle}>Search defaults and runtime policy</h2>
        <p style={styles.pageHeroText}>Manage the default retrieval contract used by the app and inspect the audit trail of changes. The workbench can still override these values per query.</p>
      </section>

      <div style={styles.card}>
        <div style={{ ...styles.flex, justifyContent: 'space-between', marginBottom: '24px', flexWrap: 'wrap' }}>
          <div>
            <h3 style={{ margin: 0 }}>Search settings</h3>
            <p style={{ margin: '6px 0 0', color: '#64748b' }}>Persisted backend defaults for Marqo search and reranking behavior.</p>
          </div>
          <div style={styles.flex}>
            {saved && <span style={{ color: '#059669', fontSize: '14px' }}>Saved</span>}
            <button style={styles.buttonSecondary} onClick={resetSettings} disabled={saving}>Reset to defaults</button>
            <button style={styles.button} onClick={saveSettings} disabled={saving}>{saving ? 'Saving...' : 'Save settings'}</button>
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '24px' }}>
          <div style={styles.panelMuted}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Search Method</label>
            <select value={settings.searchMethod} onChange={event => updateSetting('searchMethod', event.target.value)} style={{ ...styles.input, marginBottom: '8px' }}>
              <option value="TENSOR">Tensor</option>
              <option value="LEXICAL">Lexical</option>
              <option value="HYBRID">Hybrid</option>
            </select>
          </div>

          {settings.searchMethod === 'HYBRID' && (
            <div style={styles.panelMuted}>
              <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Hybrid Balance (Alpha): {settings.alpha.toFixed(2)}</label>
              <input type="range" min="0" max="1" step="0.05" value={settings.alpha} onChange={event => updateSetting('alpha', parseFloat(event.target.value))} style={{ width: '100%' }} />
            </div>
          )}

          <div style={styles.panelMuted}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Results per Search: {settings.limit}</label>
            <input type="range" min="5" max="50" step="5" value={settings.limit} onChange={event => updateSetting('limit', parseInt(event.target.value, 10))} style={{ width: '100%' }} />
          </div>

          {settings.searchMethod !== 'LEXICAL' && (
            <div style={styles.panelMuted}>
              <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Search Accuracy (efSearch): {settings.efSearch}</label>
              <input type="range" min="64" max="512" step="64" value={settings.efSearch} onChange={event => updateSetting('efSearch', parseInt(event.target.value, 10))} style={{ width: '100%' }} />
            </div>
          )}

          <div style={styles.panelMuted}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Default Index</label>
            <input style={styles.input} value={settings.indexName} onChange={event => updateSetting('indexName', event.target.value)} />
          </div>

          <div style={styles.panelMuted}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Candidate Cap: {settings.candidateCap}</label>
            <input type="range" min="20" max="200" step="10" value={settings.candidateCap} onChange={event => updateSetting('candidateCap', parseInt(event.target.value, 10))} style={{ width: '100%' }} />
          </div>

          <div style={styles.panelMuted}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Max Chunks Per Doc: {settings.maxChunksPerDoc}</label>
            <input type="range" min="1" max="10" step="1" value={settings.maxChunksPerDoc} onChange={event => updateSetting('maxChunksPerDoc', parseInt(event.target.value, 10))} style={{ width: '100%' }} />
          </div>

          <div style={styles.panelMuted}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 700, marginBottom: '8px' }}>Query Expansion Profile</label>
            <input style={styles.input} value={settings.queryExpansionProfile} onChange={event => updateSetting('queryExpansionProfile', event.target.value)} />
          </div>

          <div style={styles.panelMuted}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <input type="checkbox" checked={settings.showHighlights} onChange={event => updateSetting('showHighlights', event.target.checked)} />
              <span>Show highlights</span>
            </label>
          </div>

          <div style={styles.panelMuted}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <input type="checkbox" checked={settings.useE5Prefix} onChange={event => updateSetting('useE5Prefix', event.target.checked)} />
              <span>Use E5 query prefix</span>
            </label>
          </div>

          <div style={styles.panelMuted}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <input type="checkbox" checked={settings.excludeReference} onChange={event => updateSetting('excludeReference', event.target.checked)} />
              <span>Exclude reference chunks</span>
            </label>
          </div>
        </div>
      </div>

      <div style={styles.card}>
        <h3 style={{ marginTop: 0 }}>Settings change history</h3>
        {auditLoading ? (
          <p style={{ color: '#64748b' }}>Loading audit logs...</p>
        ) : auditLogs.length === 0 ? (
          <p style={{ color: '#64748b' }}>No settings changes recorded yet.</p>
        ) : (
          <div style={{ maxHeight: '400px', overflow: 'auto' }}>
            <table style={{ ...styles.table, fontSize: '13px' }}>
              <thead>
                <tr>
                  <th style={styles.th}>Time</th>
                  <th style={styles.th}>Setting</th>
                  <th style={styles.th}>Old Value</th>
                  <th style={styles.th}>New Value</th>
                </tr>
              </thead>
              <tbody>
                {auditLogs.map((log, index) => (
                  <tr key={index}>
                    <td style={styles.td}>{new Date(log.timestamp).toLocaleString()}</td>
                    <td style={styles.td}><code style={{ background: '#f1f5f9', padding: '2px 6px', borderRadius: '4px' }}>{log.field_name}</code></td>
                    <td style={styles.td}><span style={{ color: '#dc2626' }}>{log.old_value || '-'}</span></td>
                    <td style={styles.td}><span style={{ color: '#059669' }}>{log.new_value || '-'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
