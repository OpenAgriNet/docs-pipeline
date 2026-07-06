import React from 'react'
import { Route, Routes } from 'react-router-dom'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
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

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<DashboardView />} />
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
      </Routes>
    </AppShell>
  )
}
