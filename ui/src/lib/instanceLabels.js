/**
 * Plan 2: display labels for documents.instance (state / portal tag).
 * Authorization still comes from Keycloak JWT — this is UI only.
 */

export const PORTAL_INSTANCE = 'bv'

/** Short badge label: mh → MH, bv → BV */
export function formatInstanceLabel(instance) {
  const code = String(instance || '')
    .trim()
    .toLowerCase()
  if (!code || code === 'default') return null
  if (code === 'bv' || code === 'bharat-vistaar' || code === 'bharat_vistaar') {
    return 'BV'
  }
  return code.toUpperCase()
}

export function instanceBadgeTitle(instance) {
  const code = String(instance || '')
    .trim()
    .toLowerCase()
  if (!code || code === 'default') return 'No state tag'
  if (code === 'bv' || code === 'bharat-vistaar' || code === 'bharat_vistaar') {
    return 'Bharat Vistaar portal'
  }
  return `State: ${code.toUpperCase()}`
}

/**
 * Default instance for upload form.
 * Super admin → portal (bv); single state → that state; multi → first (user can change).
 */
export function defaultUploadInstance({ isSuperAdmin, instances = [], portalInstance = PORTAL_INSTANCE }) {
  if (isSuperAdmin) return portalInstance || PORTAL_INSTANCE
  const list = (instances || []).map((i) => String(i).trim().toLowerCase()).filter(Boolean)
  if (list.length >= 1) return list[0]
  return ''
}
