import React from 'react'
import { Route, Routes } from 'react-router-dom'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import './markdown.css'
import AppShell from './components/AppShell'
import DashboardView from './views/DashboardView'
import NewDocumentView from './views/NewDocumentView'
import DocumentOpsView from './views/DocumentOpsView'
import SearchWorkbenchView from './views/SearchWorkbenchView'
import SettingsView from './views/SettingsView'
import GlobalAuditView from './views/GlobalAuditView'

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<DashboardView />} />
        <Route path="/new" element={<NewDocumentView />} />
        <Route path="/documents/:workflowId" element={<DocumentOpsView />} />
        <Route path="/search" element={<SearchWorkbenchView />} />
        <Route path="/settings" element={<SettingsView />} />
        <Route path="/audit" element={<GlobalAuditView />} />
      </Routes>
    </AppShell>
  )
}
