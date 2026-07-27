# Plan 2 — Keycloak instance tenancy (reversible)

**Status:** implemented  
**Goal:** Reuse Keycloak for *who can see which state*; store only document state tags in DB.

## Model

| Concern | Source |
|---------|--------|
| User roles / states | Keycloak groups → JWT `groups` |
| Document state / portal | DB `documents.instance` (`mh`, `up`, `bv`, …) |
| List/get isolation | API filters by JWT instances |
| Super admin | `/global/super-admin` → unrestricted |

### Instance codes

- State: lowercase Keycloak state (`mh` from `/states/MH/...`)
- Portal / Bharat Vistaar: `bv` (`PORTAL_INSTANCE`)

## What changed (Plan 2)

- `pipeline/auth/tenancy.py` — `resolve_create_instance()` (no default to wrong state)
- Upload/register already call `_resolve_create_instance`
- `/auth/me` includes `portal_instance`
- UI: upload state picker, document **State** column + badges, super-admin state filter

## Not in Plan 2

- No access-control tables in DB
- No per-user ACL sync from Keycloak into SQLite

## How to revert Plan 2

If this approach fails product-wise:

```bash
git log --oneline --grep='Plan 2\|plan2\|instance tenancy' 
# or revert the commit(s) that introduced PLAN2 / resolve_create_instance UI
git revert <commit-sha>
```

Or restore previous `tenancy.py` create resolution:

```python
# Old behaviour (for reference only)
return assert_instance_access(user, requested or default_instance())
```

and remove UI instance picker / State column if desired. Core Keycloak group parsing can stay.

## Verify

1. Super admin SSO → documents from all states; upload defaults to **BV**
2. MH contributor → only `instance=mh` docs; upload stamps `mh`
3. MH reviewer → same visibility; no upload permission
4. Cross-state get → 404
