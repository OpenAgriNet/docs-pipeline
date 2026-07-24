import React from 'react'
import { Navigate, Outlet, Route, Routes } from 'react-router-dom'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import { RequireAuth } from './auth/AuthProvider'
import { ROUTES } from './auth/keycloak'
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
import GlobalAuditView from './views/GlobalAuditView'
import TenantsView from './views/TenantsView'
import LoginView from './views/LoginView'
import SsoCallbackView from './views/SsoCallbackView'
import { useAuth } from './auth/AuthProvider'

function ProtectedLayout() {
  return (
    <RequireAuth>
      <AppShell>
        <Outlet />
      </AppShell>
    </RequireAuth>
  )
}

// Landing route. The dashboard is a data-plane view (needs `search`). A pure
// control-plane platform admin (no tenant membership, no data permissions) would
// otherwise hit an empty dashboard, so send it to the Tenants console instead.
// Every data user still lands on the dashboard; auth-disabled dev keeps it too.
function DefaultRoute() {
  const { hasPermission, isPlatformAdmin } = useAuth()
  if (isPlatformAdmin && !hasPermission('search')) {
    return <Navigate to="/tenants" replace />
  }
  return <DashboardView />
}

export default function App() {
  return (
    <Routes>
      <Route path={ROUTES.LOGIN} element={<LoginView />} />
      <Route path={ROUTES.AUTH_SSO_CALLBACK} element={<SsoCallbackView />} />

      <Route element={<ProtectedLayout />}>
        <Route index element={<DefaultRoute />} />
        <Route path="documents" element={<DocumentsView />} />
        <Route path="ingest" element={<NewDocumentView />} />
        <Route path="queue" element={<QueueView />} />
        <Route path="runs" element={<RunsView />} />
        <Route path="indexes" element={<IndexesView />} />
        <Route path="documents/:workflowId" element={<DocumentOpsView />} />
        <Route path="search" element={<SearchWorkbenchView />} />
        <Route path="chunks" element={<ChunkExplorerView />} />
        {/* Settings temporarily hidden */}
        <Route path="settings" element={<Navigate to={ROUTES.HOME} replace />} />
        <Route path="audit" element={<GlobalAuditView />} />
        <Route path="tenants" element={<TenantsView />} />
        <Route path="*" element={<Navigate to={ROUTES.HOME} replace />} />
      </Route>
    </Routes>
  )
}
