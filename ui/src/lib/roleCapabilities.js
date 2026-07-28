/**
 * Product role catalog for the access / profile overlay.
 * Keep in sync with Keycloak roles + pipeline/auth/permissions.py.
 *
 * Model:
 *   - super_admin  — Bharat Vistaar (BV) platform super admin
 *   - state_admin  — full access within assigned state(s)
 *   - state_view   — view-only within assigned state(s)
 */

export const UserRole = {
  SUPER_ADMIN: 'super_admin',
  STATE_ADMIN: 'state_admin',
  STATE_VIEW: 'state_view',
  // Legacy aliases used by older UI strings / group leaves
  CONTRIBUTOR: 'state_admin',
  REVIEWER: 'state_view',
}

/** Full catalog — order used when super admin views all roles. */
export const ROLE_CATALOG = [
  {
    id: UserRole.SUPER_ADMIN,
    label: 'Super Admin',
    shortLabel: 'Bharat Vistaar · Platform',
    scope: 'All states / tenants',
    summary:
      'Full platform access across every state. Manage users, settings, PROD approval, and all documents.',
    capabilities: [
      { id: 'all_states', label: 'Access all states', allowed: true },
      { id: 'view', label: 'View all documents', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: true },
      { id: 'edit', label: 'Edit any document', allowed: true },
      { id: 'review', label: 'Review & approve', allowed: true },
      { id: 'delete', label: 'Delete documents', allowed: true },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: true },
      { id: 'settings', label: 'Platform settings', allowed: true },
      { id: 'manage_users', label: 'User & role management', allowed: true },
      { id: 'prod_approve', label: 'Approve PROD promotion', allowed: true },
    ],
  },
  {
    id: UserRole.STATE_ADMIN,
    label: 'State Admin',
    shortLabel: 'State · full access',
    scope: 'Assigned state(s) only',
    summary:
      'Full operational access in assigned states: upload, review, pipeline, and manage content. Cannot manage platform users or approve PROD.',
    capabilities: [
      { id: 'all_states', label: 'Access all states', allowed: false },
      { id: 'view', label: 'View all documents in state', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: true },
      { id: 'edit', label: 'Edit documents in state', allowed: true },
      { id: 'review', label: 'Review & approve in state', allowed: true },
      { id: 'delete_own', label: 'Delete own documents', allowed: true },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: true },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
      { id: 'prod_approve', label: 'Approve PROD promotion', allowed: false },
    ],
  },
  {
    id: UserRole.STATE_VIEW,
    label: 'State View',
    shortLabel: 'State · view only',
    scope: 'Assigned state(s) only',
    summary:
      'View-only access in assigned states. Can browse and search documents; cannot upload, edit, or run pipeline.',
    capabilities: [
      { id: 'all_states', label: 'Access all states', allowed: false },
      { id: 'view', label: 'View documents in state', allowed: true },
      { id: 'search', label: 'Search in state', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: false },
      { id: 'edit', label: 'Edit documents', allowed: false },
      { id: 'review', label: 'Review & approve', allowed: false },
      { id: 'delete', label: 'Delete documents', allowed: false },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: false },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
    ],
  },
]

const PRODUCT_ROLE_IDS = new Set(ROLE_CATALOG.map((r) => r.id))

const ROLE_ALIASES = {
  super_admin: UserRole.SUPER_ADMIN,
  'super-admin': UserRole.SUPER_ADMIN,
  superadmin: UserRole.SUPER_ADMIN,
  master_admin: UserRole.SUPER_ADMIN,
  state_admin: UserRole.STATE_ADMIN,
  'state-admin': UserRole.STATE_ADMIN,
  admin: UserRole.STATE_ADMIN,
  contributor: UserRole.STATE_ADMIN,
  content_curator: UserRole.STATE_ADMIN,
  state_view: UserRole.STATE_VIEW,
  'state-view': UserRole.STATE_VIEW,
  view: UserRole.STATE_VIEW,
  viewer: UserRole.STATE_VIEW,
  reviewer: UserRole.STATE_VIEW,
}

export function normalizeProductRole(value) {
  if (!value || typeof value !== 'string') return null
  const key = value.trim().toLowerCase().replace(/\s+/g, '_').replace(/-/g, '_')
  if (ROLE_ALIASES[key]) return ROLE_ALIASES[key]
  if (ROLE_ALIASES[value.trim().toLowerCase()]) return ROLE_ALIASES[value.trim().toLowerCase()]
  if (PRODUCT_ROLE_IDS.has(key)) return key
  return null
}

/**
 * Which role cards to show in the access panel.
 * - Super admin → all three (platform catalog)
 * - Others → only product roles they actually hold
 */
export function rolesToDisplay({ isSuperAdmin, roles = [], stateRoles = {} }) {
  if (isSuperAdmin) {
    return ROLE_CATALOG
  }

  const held = new Set()
  for (const r of roles) {
    const n = normalizeProductRole(r)
    if (n) held.add(n)
  }
  for (const r of Object.values(stateRoles || {})) {
    const n = normalizeProductRole(r)
    if (n) held.add(n)
  }

  // Prefer stable catalog order
  return ROLE_CATALOG.filter((entry) => held.has(entry.id))
}

export function primaryProductRole({ isSuperAdmin, roles = [], stateRoles = {} }) {
  if (isSuperAdmin) return UserRole.SUPER_ADMIN
  const displayed = rolesToDisplay({ isSuperAdmin: false, roles, stateRoles })
  return displayed[0]?.id || null
}

export function roleLabel(roleId) {
  return ROLE_CATALOG.find((r) => r.id === roleId)?.label || roleId || 'Member'
}

/** Human state list from instances / state_roles keys. */
export function formatStateList(instances = [], stateRoles = {}) {
  const codes = new Set()
  for (const i of instances || []) {
    if (i) codes.add(String(i).toUpperCase())
  }
  for (const k of Object.keys(stateRoles || {})) {
    if (k) codes.add(String(k).toUpperCase())
  }
  return [...codes].sort()
}
