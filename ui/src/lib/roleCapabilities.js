/**
 * Product role catalog for the access / profile overlay.
 * Keep in sync with Keycloak roles + pipeline/auth/permissions.py.
 */

export const UserRole = {
  SUPER_ADMIN: 'super_admin',
  CONTRIBUTOR: 'contributor',
  REVIEWER: 'reviewer',
}

/** Full catalog — order used when super admin views all roles. */
export const ROLE_CATALOG = [
  {
    id: UserRole.SUPER_ADMIN,
    label: 'Super Admin',
    shortLabel: 'Bharat Vistaar · Platform',
    scope: 'All states / tenants',
    summary:
      'Full platform access across every state. Manage users, settings, and all documents.',
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
    ],
  },
  {
    id: UserRole.CONTRIBUTOR,
    label: 'Contributor',
    shortLabel: 'State contributor',
    scope: 'Assigned state(s) only',
    summary:
      'Upload and manage content in assigned states. Can approve and delete only their own documents.',
    capabilities: [
      { id: 'all_states', label: 'Access all states', allowed: false },
      { id: 'view', label: 'View all documents in state', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: true },
      { id: 'edit_own', label: 'Edit own documents', allowed: true },
      { id: 'review_own', label: 'Approve own documents', allowed: true },
      { id: 'delete_own', label: 'Delete own documents', allowed: true },
      { id: 'pipeline', label: 'Run pipeline on own docs', allowed: true },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
    ],
  },
  {
    id: UserRole.REVIEWER,
    label: 'Reviewer',
    shortLabel: 'State reviewer',
    scope: 'Assigned state(s) only',
    summary:
      'Review and approve documents in assigned states. Cannot upload or delete.',
    capabilities: [
      { id: 'all_states', label: 'Access all states', allowed: false },
      { id: 'view', label: 'View all documents in state', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: false },
      { id: 'edit', label: 'Edit documents in state', allowed: true },
      { id: 'review', label: 'Review & approve', allowed: true },
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
  contributor: UserRole.CONTRIBUTOR,
  reviewer: UserRole.REVIEWER,
  content_curator: UserRole.CONTRIBUTOR,
  admin: UserRole.CONTRIBUTOR,
  viewer: UserRole.REVIEWER,
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
