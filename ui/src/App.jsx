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
import SettingsView from './views/SettingsView'
import GlobalAuditView from './views/GlobalAuditView'
import LoginView from './views/LoginView'
import SsoCallbackView from './views/SsoCallbackView'

function ProtectedLayout() {
  return (
    <RequireAuth>
      <AppShell>
        <Outlet />
      </AppShell>
    </RequireAuth>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path={ROUTES.LOGIN} element={<LoginView />} />
      <Route path={ROUTES.AUTH_SSO_CALLBACK} element={<SsoCallbackView />} />

      <Route element={<ProtectedLayout />}>
        <Route index element={<DashboardView />} />
        <Route path="documents" element={<DocumentsView />} />
        <Route path="ingest" element={<NewDocumentView />} />
        <Route path="queue" element={<QueueView />} />
        <Route path="runs" element={<RunsView />} />
        <Route path="indexes" element={<IndexesView />} />
        <Route path="documents/:workflowId" element={<DocumentOpsView />} />
        <Route path="search" element={<SearchWorkbenchView />} />
        <Route path="chunks" element={<ChunkExplorerView />} />
        <Route path="settings" element={<SettingsView />} />
        <Route path="audit" element={<GlobalAuditView />} />
        <Route path="*" element={<Navigate to={ROUTES.HOME} replace />} />
      </Route>
    </Routes>
  )
}
