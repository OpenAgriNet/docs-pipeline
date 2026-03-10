import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../config'
import { styles } from '../styles/appStyles'

function formatValue(jsonStr) {
  if (!jsonStr) return '(empty)'
  try {
    const parsed = JSON.parse(jsonStr)
    if (typeof parsed === 'string') return parsed
    if (typeof parsed === 'boolean') return parsed ? 'Yes' : 'No'
    return JSON.stringify(parsed, null, 2)
  } catch {
    return jsonStr
  }
}

function getDescription(log) {
  if (log.action_type === 'stage_change') return `${formatValue(log.old_value)} → ${formatValue(log.new_value)}`
  if (log.action_type === 'approval') {
    const meta = log.metadata ? JSON.parse(log.metadata) : {}
    return `Approved at ${meta.stage || 'unknown stage'}`
  }
  if (log.entity_type && log.entity_id) return `${log.entity_type} #${log.entity_id}: ${log.field_name || ''}`
  return log.field_name || ''
}

function AuditEntry({ log, global = false, onNavigate }) {
  const [expanded, setExpanded] = useState(false)
  const actionColors = {
    stage_change: '#1d4ed8',
    page_edit: '#059669',
    chunk_edit: '#d97706',
    approval: '#7c3aed',
    page_reset: '#dc2626',
    chunk_reset: '#dc2626'
  }
  const actionLabels = {
    stage_change: 'Stage Change',
    page_edit: 'Page Edit',
    chunk_edit: 'Chunk Edit',
    approval: 'Approval',
    page_reset: 'Page Reset',
    chunk_reset: 'Chunk Reset'
  }
  const color = actionColors[log.action_type] || '#64748b'
  const hasDetails = log.old_value || log.new_value
  const docId = log.document_id || ''
  const docDisplay = docId.replace(/^doc-\d+-/, '') || docId

  return (
    <div
      style={{ padding: '12px 16px', borderBottom: '1px solid #e2e8f0', cursor: hasDetails ? 'pointer' : 'default' }}
      onClick={() => hasDetails && setExpanded(value => !value)}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <span style={{ background: `${color}20`, color, padding: '4px 10px', borderRadius: '999px', fontSize: '12px', fontWeight: 700 }}>
          {actionLabels[log.action_type] || log.action_type}
        </span>
        {global && (
          <button
            style={{ background: '#eff6ff', border: 'none', padding: '4px 8px', borderRadius: '999px', fontSize: '12px', color: '#1d4ed8', cursor: 'pointer' }}
            onClick={(event) => {
              event.stopPropagation()
              onNavigate(log.workflow_id)
            }}
            title={`View document: ${docDisplay}`}
          >
            {docDisplay.length > 30 ? `${docDisplay.substring(0, 30)}...` : docDisplay}
          </button>
        )}
        <span style={{ flex: 1, fontSize: '14px', color: '#334155' }}>{getDescription(log)}</span>
        <span style={{ fontSize: '12px', color: '#94a3b8', whiteSpace: 'nowrap' }}>
          {new Date(log.timestamp).toLocaleString()}
        </span>
        {hasDetails && <span style={{ color: '#94a3b8', fontSize: '12px' }}>{expanded ? '▼' : '▶'}</span>}
      </div>

      {expanded && hasDetails && (
        <div style={{ marginTop: '12px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <div>
            <div style={{ fontWeight: 700, marginBottom: '6px', color: '#991b1b', fontSize: '12px' }}>Before</div>
            <pre style={{ background: '#fef2f2', padding: '10px', borderRadius: '10px', overflow: 'auto', maxHeight: '220px', whiteSpace: 'pre-wrap', fontSize: '12px', margin: 0, border: '1px solid #fecaca' }}>
              {formatValue(log.old_value)}
            </pre>
          </div>
          <div>
            <div style={{ fontWeight: 700, marginBottom: '6px', color: '#166534', fontSize: '12px' }}>After</div>
            <pre style={{ background: '#f0fdf4', padding: '10px', borderRadius: '10px', overflow: 'auto', maxHeight: '220px', whiteSpace: 'pre-wrap', fontSize: '12px', margin: 0, border: '1px solid #bbf7d0' }}>
              {formatValue(log.new_value)}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

export function DocumentAuditLog({ workflowId }) {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [total, setTotal] = useState(0)

  useEffect(() => {
    fetchLogs()
  }, [workflowId, filter])

  async function fetchLogs() {
    setLoading(true)
    try {
      const url = filter === 'all'
        ? `${API_BASE}/documents/${workflowId}/audit?limit=100`
        : `${API_BASE}/documents/${workflowId}/audit?action_type=${filter}&limit=100`
      const response = await fetch(url)
      const data = await response.json()
      setLogs(data.logs || [])
      setTotal(data.total || 0)
    } catch (error) {
      console.error('Failed to fetch audit logs:', error)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={styles.card}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', flexWrap: 'wrap', gap: '12px' }}>
        <h3 style={{ margin: 0 }}>Audit Trail ({total} entries)</h3>
        <select value={filter} onChange={event => setFilter(event.target.value)} style={{ ...styles.input, width: '220px', marginBottom: 0 }}>
          <option value="all">All Actions</option>
          <option value="stage_change">Stage Changes</option>
          <option value="page_edit">Page Edits</option>
          <option value="chunk_edit">Chunk Edits</option>
          <option value="approval">Approvals</option>
          <option value="page_reset">Page Resets</option>
          <option value="chunk_reset">Chunk Resets</option>
        </select>
      </div>

      {loading ? (
        <p style={{ textAlign: 'center', color: '#64748b' }}>Loading audit logs...</p>
      ) : logs.length === 0 ? (
        <p style={{ textAlign: 'center', color: '#64748b', padding: '40px 0' }}>No audit entries found for this document.</p>
      ) : (
        <div style={{ maxHeight: '620px', overflow: 'auto' }}>
          {logs.map(log => <AuditEntry key={log.id} log={log} />)}
        </div>
      )}
    </div>
  )
}

export function GlobalAuditLogPanel() {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const limit = 50
  const navigate = useNavigate()

  useEffect(() => {
    fetchLogs()
  }, [filter, offset])

  async function fetchLogs() {
    setLoading(true)
    try {
      const url = filter === 'all'
        ? `${API_BASE}/audit?limit=${limit}&offset=${offset}`
        : `${API_BASE}/audit?action_type=${filter}&limit=${limit}&offset=${offset}`
      const response = await fetch(url)
      const data = await response.json()
      setLogs(data.logs || [])
      setTotal(data.total || 0)
    } catch (error) {
      console.error('Failed to fetch audit logs:', error)
    } finally {
      setLoading(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / limit))
  const currentPage = Math.floor(offset / limit) + 1

  return (
    <div style={styles.card}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px', flexWrap: 'wrap', gap: '12px' }}>
        <div>
          <h2 style={{ margin: 0 }}>Global Audit Log</h2>
          <p style={{ color: '#64748b', fontSize: '14px', marginTop: '4px' }}>Track edits, resets, approvals, and stage changes across the whole pipeline.</p>
        </div>
        <select value={filter} onChange={event => { setFilter(event.target.value); setOffset(0) }} style={{ ...styles.input, width: '220px', marginBottom: 0 }}>
          <option value="all">All Actions</option>
          <option value="stage_change">Stage Changes</option>
          <option value="page_edit">Page Edits</option>
          <option value="chunk_edit">Chunk Edits</option>
          <option value="approval">Approvals</option>
          <option value="page_reset">Page Resets</option>
          <option value="chunk_reset">Chunk Resets</option>
        </select>
      </div>

      {loading ? (
        <p style={{ textAlign: 'center', color: '#64748b', padding: '40px 0' }}>Loading audit logs...</p>
      ) : logs.length === 0 ? (
        <p style={{ textAlign: 'center', color: '#64748b', padding: '40px 0' }}>No audit entries found.</p>
      ) : (
        <>
          <div style={{ marginBottom: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
            <span style={{ color: '#64748b', fontSize: '14px' }}>
              Showing {offset + 1}-{Math.min(offset + limit, total)} of {total} entries
            </span>
            <div style={styles.flex}>
              <button style={{ ...styles.buttonSecondary, opacity: offset === 0 ? 0.5 : 1 }} onClick={() => setOffset(Math.max(0, offset - limit))} disabled={offset === 0}>Previous</button>
              <span style={{ fontSize: '14px', color: '#334155' }}>Page {currentPage} of {totalPages}</span>
              <button style={{ ...styles.buttonSecondary, opacity: offset + limit >= total ? 0.5 : 1 }} onClick={() => setOffset(offset + limit)} disabled={offset + limit >= total}>Next</button>
            </div>
          </div>
          <div style={{ maxHeight: '680px', overflow: 'auto' }}>
            {logs.map(log => (
              <AuditEntry
                key={log.id}
                log={log}
                global
                onNavigate={(workflowId) => navigate(`/documents/${workflowId}`)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
