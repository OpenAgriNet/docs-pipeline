export const styles = {
  container: { maxWidth: '1200px', margin: '0 auto', padding: '20px' },
  wideContainer: { maxWidth: '1600px', margin: '0 auto', padding: '20px' },
  shell: {
    minHeight: '100vh',
    background: '#f9fafb',
    color: '#14213d'
  },
  header: {
    background: '#1a1a2e',
    color: 'white',
    borderBottom: 'none'
  },
  headerInner: {
    maxWidth: '1600px',
    margin: '0 auto',
    padding: '16px 20px',
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: '20px',
    flexWrap: 'wrap'
  },
  brandBlock: { display: 'flex', flexDirection: 'column', gap: '4px' },
  brandEyebrow: { display: 'none' },
  brandTitle: {
    margin: 0,
    fontSize: '20px',
    fontWeight: 600
  },
  brandSubtitle: { margin: 0, fontSize: '13px', color: '#cbd5e1' },
  nav: { display: 'flex', gap: '20px', flexWrap: 'wrap', alignItems: 'center' },
  navLink: {
    color: 'white',
    textDecoration: 'none',
    opacity: 0.85,
    fontSize: '14px',
    fontWeight: 500
  },
  navLinkActive: {
    opacity: 1,
    textDecoration: 'underline'
  },
  navKicker: { display: 'none' },
  pageHero: {
    marginBottom: '24px',
    padding: '20px',
    borderRadius: '8px',
    background: 'white',
    color: '#111827',
    boxShadow: '0 2px 4px rgba(0,0,0,0.08)',
    border: '1px solid #e5e7eb'
  },
  pageHeroTitle: { margin: '0 0 8px', fontSize: '24px', fontWeight: 600 },
  pageHeroText: { margin: 0, maxWidth: '840px', color: '#6b7280', lineHeight: 1.5 },
  pageHeroMeta: { marginTop: '16px', display: 'flex', gap: '10px', flexWrap: 'wrap' },
  metaPill: {
    padding: '6px 12px',
    borderRadius: '12px',
    background: '#f3f4f6',
    border: '1px solid #e5e7eb',
    fontSize: '13px',
    color: '#374151'
  },
  card: {
    background: 'white',
    borderRadius: '8px',
    padding: '20px',
    boxShadow: '0 2px 4px rgba(0,0,0,0.08)',
    marginBottom: '16px',
    border: '1px solid #e5e7eb'
  },
  panelMuted: { background: '#f9fafb', borderRadius: '8px', padding: '16px' },
  button: {
    background: '#4f46e5',
    color: 'white',
    border: 'none',
    padding: '10px 20px',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '14px',
    fontWeight: 500
  },
  buttonSecondary: {
    background: '#e5e7eb',
    color: '#374151',
    border: 'none',
    padding: '10px 20px',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '14px',
    fontWeight: 500
  },
  buttonSuccess: {
    background: '#10b981',
    color: 'white',
    border: 'none',
    padding: '10px 20px',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '14px',
    fontWeight: 500
  },
  buttonSmall: {
    padding: '6px 12px',
    fontSize: '12px',
    borderRadius: '4px',
    border: 'none',
    cursor: 'pointer'
  },
  input: {
    width: '100%',
    padding: '12px',
    border: '1px solid #d1d5db',
    borderRadius: '6px',
    fontSize: '14px',
    marginBottom: '12px',
    boxSizing: 'border-box'
  },
  textarea: {
    width: '100%',
    padding: '12px',
    border: '1px solid #d1d5db',
    borderRadius: '6px',
    fontSize: '14px',
    minHeight: '200px',
    fontFamily: 'monospace',
    boxSizing: 'border-box'
  },
  badge: (stage) => ({
    display: 'inline-block',
    padding: '6px 12px',
    borderRadius: '999px',
    fontSize: '12px',
    fontWeight: '700',
    letterSpacing: '0.02em',
    background: {
      registered: '#dbeafe',
      ocr_processing: '#fef3c7',
      ocr_review: '#fde7f3',
      translation_processing: '#ffedd5',
      translation_review: '#e0e7ff',
      chunking: '#fef3c7',
      chunk_review: '#fce7f3',
      ready_for_ingestion: '#dcfce7',
      ingesting: '#fef3c7',
      completed: '#dcfce7',
      failed: '#fee2e2'
    }[stage] || '#e5e7eb',
    color: {
      registered: '#1d4ed8',
      ocr_processing: '#92400e',
      ocr_review: '#9d174d',
      translation_processing: '#9a3412',
      translation_review: '#3730a3',
      chunking: '#92400e',
      chunk_review: '#9d174d',
      ready_for_ingestion: '#166534',
      ingesting: '#92400e',
      completed: '#166534',
      failed: '#991b1b'
    }[stage] || '#374151'
  }),
  stepper: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '12px 0 4px',
    marginBottom: '4px',
    overflowX: 'auto',
    gap: '8px'
  },
  stepperStep: () => ({
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    flex: '1',
    position: 'relative',
    minWidth: '84px'
  }),
  stepperCircle: (status) => ({
    width: '34px',
    height: '34px',
    borderRadius: '50%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: '14px',
    fontWeight: '700',
    marginBottom: '8px',
    background: status === 'completed' ? '#059669' : status === 'active' ? '#1d4ed8' : status === 'failed' ? '#dc2626' : '#e2e8f0',
    color: status === 'pending' ? '#64748b' : 'white',
    border: status === 'active' ? '3px solid #bfdbfe' : 'none'
  }),
  stepperLabel: (status) => ({
    fontSize: '11px',
    textAlign: 'center',
    color: status === 'active' ? '#1d4ed8' : status === 'completed' ? '#166534' : status === 'failed' ? '#991b1b' : '#64748b',
    fontWeight: status === 'active' ? 700 : 500,
    maxWidth: '78px'
  }),
  stepperLine: (status) => ({
    position: 'absolute',
    top: '17px',
    left: '50%',
    width: '100%',
    height: '2px',
    background: status === 'completed' ? '#059669' : '#cbd5e1',
    zIndex: -1
  }),
  summaryGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '14px' },
  statCard: {
    background: 'white',
    padding: '18px',
    borderRadius: '16px',
    border: '1px solid rgba(148, 163, 184, 0.16)',
    boxShadow: '0 14px 36px rgba(15, 23, 42, 0.06)'
  },
  statValue: { fontSize: '30px', fontWeight: 700, letterSpacing: '-0.03em', marginBottom: '8px' },
  statLabel: { fontSize: '12px', color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.08em' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '16px' },
  flex: { display: 'flex', gap: '12px', alignItems: 'center' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    textAlign: 'left',
    padding: '12px',
    borderBottom: '2px solid #e2e8f0',
    fontWeight: '700',
    fontSize: '13px',
    color: '#475569'
  },
  td: { padding: '12px', borderBottom: '1px solid #e2e8f0', verticalAlign: 'top' },
  splitPane: { display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: '20px', alignItems: 'start' },
  opsLayout: { display: 'grid', gridTemplateColumns: '300px minmax(0, 1fr)', gap: '20px', alignItems: 'start' },
  pdfContainer: {
    background: '#e2e8f0',
    borderRadius: '16px',
    padding: '16px',
    position: 'sticky',
    top: '104px',
    maxHeight: 'calc(100vh - 124px)',
    overflow: 'auto'
  },
  pdfControls: {
    display: 'flex',
    gap: '8px',
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: '12px',
    padding: '10px',
    background: 'white',
    borderRadius: '10px'
  },
  pageIndicator: {
    background: '#1d4ed8',
    color: 'white',
    padding: '5px 10px',
    borderRadius: '999px',
    fontSize: '12px',
    fontWeight: '700'
  },
  tabs: { display: 'flex', gap: '10px', marginBottom: '16px', flexWrap: 'wrap' },
  tabButton: (active) => ({
    border: 'none',
    borderRadius: '999px',
    padding: '10px 16px',
    cursor: 'pointer',
    background: active ? '#0f172a' : '#e2e8f0',
    color: active ? 'white' : '#334155',
    fontWeight: 700
  }),
  sideStack: { display: 'grid', gap: '16px' }
}

export const PIPELINE_STAGES = [
  { id: 'registered', label: 'Registered' },
  { id: 'ocr_processing', label: 'OCR' },
  { id: 'ocr_review', label: 'OCR Review' },
  { id: 'translation_processing', label: 'Translation' },
  { id: 'translation_review', label: 'Translation Review' },
  { id: 'chunking', label: 'Chunking' },
  { id: 'chunk_review', label: 'Chunk Review' },
  { id: 'ready_for_ingestion', label: 'Pre-Ingestion' },
  { id: 'ingesting', label: 'Ingesting' },
  { id: 'completed', label: 'Completed' }
]

export const DEFAULT_SEARCH_SETTINGS = {
  searchMethod: 'HYBRID',
  limit: 12,
  alpha: 0.6,
  rankingMethod: 'rrf',
  showHighlights: true,
  efSearch: 256,
  indexName: 'documents-index',
  candidateCap: 120,
  candidateMultiplier: 10,
  maxChunksPerDoc: 2,
  useE5Prefix: true,
  excludeReference: true,
  queryExpansionProfile: 'gu-v1',
  rerankMode: 'none',
  hybridRrfK: 60
}
