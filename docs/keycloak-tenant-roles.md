# Keycloak multi-state (tenant) access

All user → state → role configuration lives in **Keycloak**. The app only reads
the JWT after SSO and enables/disables UI + API actions from claims.

## Model

| Product role | Keycloak | Scope | Capabilities |
|---|---|---|---|
| `super_admin` | Group `/global/super-admin` + realm role `super_admin` | All states | Full access, user management |
| `contributor` | Group `/states/{STATE}/contributor` + realm role `contributor` | That state | Upload, edit/delete **own**, view all, approve **own** |
| `reviewer` | Group `/states/{STATE}/reviewer` + realm role `reviewer` | That state | Edit / review / approve; **no** upload / delete |

A user can hold **different roles in different states**, e.g.:

- Contributor in Maharashtra → `/states/MH/contributor`
- Reviewer in Uttar Pradesh → `/states/UP/reviewer`

State codes are uppercase in Keycloak paths (`MH`, `UP`, `BH`); the API stores
them lowercase (`mh`, `up`, `bh`) as the document `instance` tenant id.

## Group tree (configure once in Keycloak)

```
/global
  /super-admin          → realm role super_admin
/states
  /MH
    /contributor        → realm role contributor
    /reviewer           → realm role reviewer
  /BH
    /contributor
    /reviewer
  /UP
    …
```

### Assign access to a user (admin only — Keycloak)

1. Open realm **Users** → select user (or create).
2. **Groups** tab → Join:
   - Super admin: `/global/super-admin`
   - State ops: `/states/MH/contributor`, `/states/UP/reviewer`, …
3. Do **not** hardcode tenants in the SPA. Membership alone drives the token.

Group → realm role mapping on each leaf group ensures `realm_access.roles`
also lists `contributor` / `reviewer` / `super_admin` (optional but useful).

## Token claim (required mapper)

On the public SPA client (`bharat-vistaar` / `docs-pipeline-ui`):

| Field | Value |
|---|---|
| Mapper type | **Group Membership** (`oidc-group-membership-mapper`) |
| Name | `groups` |
| Token Claim Name | `groups` |
| Full group path | **ON** |
| Add to access token | ON |
| Add to ID token | ON |
| Add to userinfo | ON |

### Example access token payload after SSO

```json
{
  "sub": "…",
  "preferred_username": "multi-state-user",
  "email": "multi@example.com",
  "groups": [
    "/states/MH/contributor",
    "/states/UP/reviewer"
  ],
  "realm_access": {
    "roles": [
      "default-roles-bharat-vistaar",
      "contributor",
      "reviewer"
    ]
  }
}
```

Super-admin example:

```json
{
  "groups": ["/global/super-admin"],
  "realm_access": { "roles": ["super_admin"] }
}
```

## What the app does on login

1. Browser SSO (existing Keycloak PKCE flow) → access token.
2. API `GET /auth/me` validates JWT (JWKS) and returns:

```json
{
  "user_id": "…",
  "username": "multi-state-user",
  "roles": ["contributor", "reviewer"],
  "permissions": ["delete_own", "pipeline", "review", "search", "upload"],
  "instances": ["mh", "up"],
  "groups": ["/states/MH/contributor", "/states/UP/reviewer"],
  "state_roles": { "mh": "contributor", "up": "reviewer" },
  "is_super_admin": false
}
```

3. UI uses:
   - `permissions` / `hasPermission('upload'|'review'|…)` for global buttons
   - `instances` to filter tenant lists
   - `state_roles` / `hasPermissionForInstance(perm, state)` for per-state enable/disable
   - `is_super_admin` → all tenants, full console

No Keycloak Admin API calls are required at login for normal operators.  
(Admin user-management UIs may call Admin API later; that is separate.)

## Configure on existing realm (DEV / PROD)

Use the platform realm + SPA client that already has Google SSO:

```
Keycloak base:  https://dev-auth-vistaar.da.gov.in/auth
Realm:          bharat-vistaar
SPA client:     bharat-vistaar
```

### Roles + groups

| File / tool | Use |
|---|---|
| `keycloak/import/partial-tenant-groups-and-roles.json` | Partial import of roles + group tree |
| `scripts/keycloak_bootstrap_tenant_groups.py` | Idempotent create roles, groups, groups mapper |

1. Partial import **or** run the bootstrap script.
2. Client **bharat-vistaar** → Group Membership mapper claim **`groups`**, full path **ON**.
3. Valid redirect URIs include your UI (e.g. `http://localhost:3001/*`, prod `/docs-pipeline/*`).
4. Users → **Groups** → join `/global/super-admin` or `/states/MH/contributor`, etc.
5. Restart API + UI after env changes.

## Env (DEV example)

```env
# API
AUTH_DISABLED=false
KEYCLOAK_ISSUER=https://dev-auth-vistaar.da.gov.in/auth/realms/bharat-vistaar
KEYCLOAK_JWKS_URL=https://dev-auth-vistaar.da.gov.in/auth/realms/bharat-vistaar/protocol/openid-connect/certs
KEYCLOAK_AUDIENCE=

# UI
VITE_AUTH_ENABLED=true
VITE_KEYCLOAK_URL=https://dev-auth-vistaar.da.gov.in/auth
VITE_KEYCLOAK_REALM=bharat-vistaar
VITE_KEYCLOAK_CLIENT_ID=bharat-vistaar
VITE_KEYCLOAK_IDP_HINT=google
```

Document `instance` values must match state codes (`mh`, `up`, …) so list/get
filtering matches group-derived `instances`.

## Permission matrix (API + UI)

| Capability | `super_admin` | `contributor` | `reviewer` |
|---|:---:|:---:|:---:|
| View docs in allowed state(s) | all | ✓ | ✓ |
| Upload | ✓ | ✓ | |
| Review / approve | ✓ | own* | ✓ |
| Delete | ✓ | own* | |
| Pipeline / reprocess | ✓ | ✓ | |
| Settings / manage users | ✓ | | |

\* Ownership uses `uploaded_by_user_id` on the document. Global permission
`delete_own` is granted to contributors; enforce owner match on mutating routes
when tightening product rules.

## Adding a new state

1. Keycloak → Groups → under `/states` create `XX` with children
   `contributor` and `reviewer`.
2. Map realm roles on the leaf groups.
3. Assign users. No app redeploy required (groups flow into the next login token).

## Bootstrap script

```bash
# Ensure roles + group tree + mappers on a running Keycloak (idempotent)
python scripts/keycloak_bootstrap_tenant_groups.py \
  --base-url http://127.0.0.1:8082/auth \
  --realm bharat-vistaar \
  --admin-password "$KEYCLOAK_ADMIN_PASSWORD"
```
