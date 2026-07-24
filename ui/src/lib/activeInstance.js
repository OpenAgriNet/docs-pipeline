/**
 * Module-level holder for the caller's ACTIVE tenant instance.
 *
 * The data client (`fetchJson` in lib/pipelineUi.js) is a plain module function,
 * not a React hook, so it cannot read the AuthProvider context directly. This tiny
 * module bridges the gap: AuthProvider pushes the selected instance here whenever it
 * changes, and fetchJson reads it to attach an `instance` param to data/search/create
 * calls. When no instance is active (single-tenant / legacy token) the value is null
 * and fetchJson appends nothing — so single-tenant behaviour is byte-for-byte today's.
 */

let activeInstance = null
const listeners = new Set()

/** Current active instance id, or null when there is none (single-tenant/legacy). */
export function getActiveInstance() {
  return activeInstance
}

/** Set by AuthProvider only. Notifies subscribers when the value actually changes. */
export function setActiveInstanceValue(next) {
  const value = next || null
  if (value === activeInstance) return
  activeInstance = value
  for (const fn of listeners) {
    try {
      fn(value)
    } catch {
      // a bad subscriber must never break instance propagation
    }
  }
}

/** Subscribe to active-instance changes; returns an unsubscribe fn. */
export function subscribeActiveInstance(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}
