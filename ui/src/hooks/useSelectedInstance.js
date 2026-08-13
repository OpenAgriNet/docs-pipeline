import { useEffect, useMemo, useState } from 'react'
import { useAuth } from '../auth/AuthProvider'
import { fetchJson } from '../lib/pipelineUi'

/**
 * Tenant picker state for Taxonomy / Indexes.
 *
 * Platform admins load the registry (membership claim may be empty); other
 * callers use auth ``instances``. Auto-selects the first option.
 */
export function useSelectedInstance() {
  const { instances, isPlatformAdmin } = useAuth()
  const [selectedInstance, setSelectedInstance] = useState('')
  const [registryTenants, setRegistryTenants] = useState([])
  const [registryError, setRegistryError] = useState('')

  useEffect(() => {
    if (!isPlatformAdmin) return undefined
    let cancelled = false
    fetchJson('/tenants')
      .then((rows) => {
        if (cancelled) return
        setRegistryTenants((Array.isArray(rows) ? rows : []).map((t) => t.id).filter(Boolean))
      })
      .catch((err) => {
        if (!cancelled) setRegistryError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [isPlatformAdmin])

  const tenantOptions = useMemo(
    () => Array.from(new Set([...(instances || []), ...registryTenants])).sort(),
    [instances, registryTenants],
  )

  useEffect(() => {
    if (!selectedInstance && tenantOptions.length > 0) {
      setSelectedInstance(tenantOptions[0])
    }
  }, [tenantOptions, selectedInstance])

  return {
    selectedInstance,
    setSelectedInstance,
    tenantOptions,
    registryError,
    isPlatformAdmin,
  }
}
