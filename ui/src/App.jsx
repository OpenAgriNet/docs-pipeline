import React from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import { useAuth } from './auth/AuthProvider'
import AppShell from './components/AppShell'
import DashboardView from './views/DashboardView'
import DocumentsView from './views/DocumentsView'
import QueueView from './views/QueueView'
import RunsView from './views/RunsView'
import IndexesView from './views/IndexesView'
import NewDocumentView from './views/NewDocumentView'
import DocumentOpsView from './views/DocumentOpsView'
import SearchWorkbenchView from './views/SearchWorkbenchView'
import ChunkExplorerView from './views/ChunkExplorerView'
import SettingsView from './views/SettingsView'
import GlobalAuditView from './views/GlobalAuditView'
import TenantsView from './views/TenantsView'

// Landing route. The dashboard is a data-plane view (needs `search`), so a pure
// control-plane platform admin (master_admin with no data permissions) would hit
// an empty/403 dashboard. Redirect it to the Tenants console instead; every data
// user still lands on the dashboard. Local dev (auth disabled) keeps the
// dashboard — it holds every permission.
function DefaultRoute() {
  const { hasPermission, isPlatformAdmin } = useAuth()
  if (isPlatformAdmin && !hasPermission('search')) {
    return <Navigate to="/tenants" replace />
  }
  return <DashboardView />
}

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<DefaultRoute />} />
        <Route path="/documents" element={<DocumentsView />} />
        <Route path="/ingest" element={<NewDocumentView />} />
        <Route path="/queue" element={<QueueView />} />
        <Route path="/runs" element={<RunsView />} />
        <Route path="/indexes" element={<IndexesView />} />
        <Route path="/documents/:workflowId" element={<DocumentOpsView />} />
        <Route path="/search" element={<SearchWorkbenchView />} />
        <Route path="/chunks" element={<ChunkExplorerView />} />
        <Route path="/settings" element={<SettingsView />} />
        <Route path="/audit" element={<GlobalAuditView />} />
        <Route path="/tenants" element={<TenantsView />} />
      </Routes>
    </AppShell>
  )
}
