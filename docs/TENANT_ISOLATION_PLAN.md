# Full Tenant Isolation — Design Plan

Status: **proposed** · Scope: identity, authorization, data, search, storage, provisioning

This document plans the evolution of the pipeline from *multi-tenant-ready* (one soft
tenant dimension, one live tenant) to *fully tenant-isolated*, with **Keycloak as the
tenant control node**.

Decisions locked for this plan:

- **Keycloak Organizations** are the tenant primitive (requires upgrading Keycloak 22 → 26).
- **Namespaced index-per-tenant** for search isolation (one Marqo index per tenant).
- **Per-tenant roles** — a user can hold different roles in different tenants.

---

## 1. Goals & non-goals

**Goals**

- Keycloak is the single source of truth for *which tenants exist*, *who belongs to a
  tenant*, and *what role they hold within that tenant*.
- A request authenticated for tenant A can never read, search, or mutate tenant B's
  documents, chunks, artifacts, or search results — enforced at every data plane, not
  just in the UI.
- Onboarding a tenant is one operation that provisions identity **and** the data planes.
- A tenant can be fully deleted (identity + data + search + objects) for data-ownership
  / right-to-erasure requirements.

**Non-goals (for this iteration)**

- Separate database *servers* per tenant (shared SQLite with a `tenant` column + enforced
  filters is sufficient; revisit only if a tenant needs a physical DB boundary).
- Per-tenant compute isolation (workers are shared; Temporal task queues stay shared).
- Billing / metering.

---

## 2. Current state & gaps

| Layer | Today | Isolation |
|---|---|---|
| Identity (Keycloak **22.0.5**) | `instances` multivalued *user attribute* → `instances` JWT claim; realm-global roles | Soft, single realm |
| API authz | `allowed_instances()`, `assert_document_instance_access` (404 cross-tenant), all routes gated | Strong (app-enforced) |
| Data (SQLite) | `documents.instance` column, stamped on insert, immutable on update, filtered in list/summary | Row-level, shared DB |
| Search (Marqo) | **Single shared index**; `instance` is an optional field; the tolerant filter **no-ops** on the live index (no `instance` field) | **None in practice** |
| Object storage (MinIO) | **Single `documents` bucket**, key = `workflow_id/artifact/file`, no tenant prefix | **None (API-gated only)** |

The two red rows (search, storage) are the substance of "full isolation." Identity needs
to move from a hand-set attribute to a managed org membership, and roles need to become
per-tenant.

The current tenant dimension is called `instance` throughout the code
(`documents.instance`, `DEFAULT_INSTANCE`, `allowed_instances`, `normalize_instance`).
This plan keeps that name as the internal tenant id; a Keycloak **Organization** maps 1:1
to one `instance` id.

---

## 3. Keycloak as the control node (Organizations)

### 3.1 Why Organizations

Keycloak **Organizations** (GA in Keycloak 26) give a first-class tenant object:
members, per-organization roles, email-domain–based onboarding, per-org identity-provider
brokering (tenant SSO), and an `organization` token claim. This makes Keycloak the
authoritative tenant registry and lifecycle manager, rather than the app inferring
tenancy from a free-form attribute.

> **Enabling migration:** the live Keycloak is **22.0.5**; Organizations require **26+**.
> See §7 for the upgrade.

### 3.2 Model

- **One Organization per tenant.** The organization's id (or a stable `instance` org
  attribute) is the tenant's `instance` id used everywhere downstream.
- **Membership** replaces the hand-set `instances` attribute. A user is a member of the
  organizations (tenants) they may access; multi-tenant users are members of several.
- **Per-tenant roles.** Roles (`admin`, `content_curator`, `viewer`) are assigned *within*
  an organization, so a user can be `content_curator` in tenant A and `viewer` in tenant B.
- **Platform super-admin** (`master_admin`) stays a realm-level role, instance-unrestricted
  (the existing `is_instance_unrestricted()` behavior).

### 3.3 Token shape

A protocol mapper emits a structured **`tenant_roles`** claim plus the flat `instances`
list (kept for backward compatibility and cheap filtering):

```json
{
  "sub": "…",
  "instances": ["tenant-a", "tenant-b"],
  "tenant_roles": {
    "tenant-a": ["content_curator"],
    "tenant-b": ["viewer"]
  },
  "envs": ["dev", "prod"]
}
```

`instances` = `keys(tenant_roles)`. Admin/super-admin tokens may omit `tenant_roles` and
carry the unrestricted marker as today.

---

## 4. Authorization model changes

`pipeline/auth/` moves from *(global role) × (instance set)* to *(role **per** instance)*.

- `AuthUser` gains `tenant_roles: dict[str, set[str]]` (parsed from the claim). `roles`
  and `instances` remain derived views for compatibility.
- `permissions_for(user, instance)` replaces the global `permissions` set: permissions are
  computed **for the instance being acted on**. A route that mutates a tenant-A document
  checks the caller's role **in tenant A**.
- Dependency shape: guards become instance-aware. Instead of `RequireReview` (global),
  doc-scoped routes resolve the doc's `instance`, then assert the caller has the needed
  permission *in that instance*. Cross-doc endpoints (search, list) already receive
  `allowed_instances`; they additionally filter by "instances where the caller has the
  required permission."
- `master_admin` short-circuits to unrestricted (unchanged).

This is the single biggest backend change and should land behind the existing
`AUTH_DISABLED` bypass so it is inert until the claim is present.

---

## 5. Data-plane isolation

### 5.1 Data (SQLite) — tighten, don't re-architect

Keep the shared DB + `documents.instance` column. Work items:

- Audit **every** query path for an instance predicate. `list_documents` /
  summaries already filter; sweep the admin, audit, provenance, and marqo-read paths that
  currently rely on the tolerant search filter.
- Add `instance` to any table that can be queried independently of `documents`
  (e.g. audit rows) so filtering never requires a join back through a tenant-crossable id.

### 5.2 Search (Marqo) — namespaced index per tenant

Marqo isolates **per index** (no in-index namespace primitive), so a tenant "namespace" is
a **dedicated index per tenant** under a naming convention.

- `index_for_instance(instance) -> f"{MARQO_INDEX_NAMESPACE}{instance}"` (e.g.
  `t-tenant-a-documents`). `MARQO_INDEX_NAMESPACE` is configurable; `MARQO_INDEX_NAME`
  becomes the resolver's fallback/default only.
- **Ingest** writes chunks to the caller's/document's tenant index.
- **Search / reads** resolve the index from the caller's instance. A restricted caller can
  only ever be handed an index in their allowed set; cross-tenant search is *physically*
  impossible, not filter-dependent.
- **Admin/global search** (super-admin) fans out across indexes explicitly and is clearly
  labeled as cross-tenant.
- The per-chunk `instance` field is kept (belt-and-suspenders / migration aid) but is no
  longer the isolation boundary — the index is.
- **Deletion** of a tenant = drop its index (clean, complete).

This supersedes the current tolerant single-index filter, which stays only as the
transitional path during migration (§8).

### 5.3 Object storage (MinIO) — per-tenant prefix

- `_minio_object_name(...)` gains an `instance` and prefixes keys:
  `"{instance}/{workflow_id}/{artifact_type}/{filename}"`.
- All read/write/copy paths derive the prefix from the document's tenant.
- Optional hardening: a bucket per tenant + a per-tenant access policy, if physical bucket
  separation or per-tenant lifecycle/quota is required. Prefix-per-tenant is the default;
  bucket-per-tenant is a config flag.

---

## 6. Tenant lifecycle & provisioning

"Create a tenant" becomes one orchestrated operation (a `manage_tenants` admin API, gated
by `master_admin`, backed by the Keycloak Admin API + resource provisioning):

```mermaid
sequenceDiagram
    participant Admin as Platform admin
    participant API as manage_tenants API
    participant KC as Keycloak (Admin API)
    participant MQ as Marqo
    participant S3 as MinIO
    participant DB as SQLite (tenants)

    Admin->>API: create tenant "tenant-a"
    API->>KC: create Organization + roles + domain
    API->>MQ: create index t-tenant-a-documents (passage schema)
    API->>S3: ensure prefix / bucket + policy
    API->>DB: insert tenants row (id, status=active)
    API-->>Admin: tenant ready; invite members via KC org
```

- **Registry.** A `tenants` table (id, display name, status, created_at) is the app-side
  mirror of the Keycloak orgs, used for listing/validation without a KC round-trip on the
  hot path.
- **Suspend** = disable the org (members can't get tokens) — data retained.
- **Delete** = disable org → drop Marqo index → delete objects by prefix/bucket → soft- or
  hard-delete the tenant's `documents` rows → remove the org.
- **Onboarding** uses Keycloak org **invitations** / email-domain self-registration instead
  of the manual `keycloak_bootstrap_docs_pipeline.py` attribute-setting path (which remains
  for local/dev).

---

## 7. Enabling migration: Keycloak 22 → 26

Organizations need Keycloak **26+**. The upgrade is the gating prerequisite.

- Four major versions; upgrade **stepwise on a staging copy first** (22 → 24 → 26 is the
  safe cadence), validating realm export/import and the existing clients
  (`docs-pipeline-api` / `-ui` / `-test-cli`) + the `instances`/`envs` mappers at each hop.
- Keycloak runs its own DB schema migration on start; snapshot `keycloak-db` before each
  step. The realm and users persist in the Postgres volume.
- Re-verify the reverse-proxy issuer setup (`KC_PROXY`, forwarded headers) — proxy/hostname
  options were re-worked across 23–26 (`KC_PROXY` → `--proxy-headers`).
- Enable the Organizations feature; migrate existing membership: create an Organization for
  the current single tenant and add current users as members with their role.
- **Rollback**: restore the `keycloak-db` snapshot + pin the prior image.

Until the upgrade completes, the **Groups** pattern (a group per tenant → `instances`
claim) is a drop-in interim that needs no code change beyond the claim source — useful if a
second tenant is needed before the upgrade window.

---

## 8. Data migration (single tenant → multi-tenant)

The current live tenant is a single `instance`. Migration is non-destructive and
incremental because the tolerant filter keeps the old path working throughout:

1. **Backfill** is already safe — NULL/empty instances normalize to `DEFAULT_INSTANCE`.
2. **Search split.** Create the per-tenant index for the existing tenant, copy its chunks
   into it (re-ingest from SQLite/MinIO artifacts, or a Marqo copy), verify counts, then
   flip reads to `index_for_instance`. Do it off-peak; the shared index stays read-only
   until cutover. (The current live index must not be mutated in place — build the new
   per-tenant index alongside and switch.)
3. **Storage.** New artifacts land under the tenant prefix immediately; a background job
   re-keys existing objects (copy → verify → delete-old) per tenant.
4. **Identity.** Create the Organization for the existing tenant; migrate members.
5. Remove the tolerant single-index fallback once all tenants are on per-tenant indexes.

---

## 9. Phased rollout

1. **Backend authz refactor** to per-tenant roles + `tenant_roles` claim — inert under
   `AUTH_DISABLED=true`. (No infra change.)
2. **Storage prefixing** — new writes tenant-prefixed; backfill job for old objects.
3. **Keycloak 22 → 26** on staging → prod; enable Organizations; migrate the current tenant.
4. **Search: namespaced index-per-tenant** — `index_for_instance`, provision + migrate the
   existing tenant's index, flip reads, retire the tolerant fallback.
5. **`manage_tenants` provisioning API** + `tenants` registry; wire onboarding to org
   invitations.
6. **Cutover & harden** — remove single-tenant assumptions; add cross-tenant isolation tests
   (a tenant-A token gets 404/empty on every tenant-B doc, chunk, artifact URL, and search).

Phases 1–2 are pure code and can proceed immediately; phase 3 (KC upgrade) gates 4–5.

---

## 10. Risks & open items

- **KC 26 upgrade** is the highest-risk step (four majors, proxy/hostname reconfig, realm
  migration). Stage it; snapshot the DB.
- **Per-tenant index cost** — N indexes carry per-index memory/overhead in Marqo; validate
  resource headroom before onboarding many tenants (consider index consolidation for very
  small tenants if it becomes an issue).
- **Cross-tenant admin UX** — global search/reporting now spans indexes; the super-admin
  paths must fan out explicitly and be clearly labeled.
- **Claim size** — `tenant_roles` grows with membership; fine for tens of tenants per user,
  revisit if a user could belong to thousands.
- **Isolation test coverage** is a deliverable, not an afterthought — the guardrail is an
  automated cross-tenant probe on every data plane.

---

## 11. Code touch-points (reference)

- `pipeline/auth/models.py` — `AuthUser.tenant_roles`, per-instance permission resolution.
- `pipeline/auth/tenancy.py` — `permissions_for(user, instance)`; keep `allowed_instances`.
- `pipeline/auth/permissions.py` — role→permission map unchanged; consumed per-instance.
- `pipeline/auth/deps.py` — instance-aware permission guards.
- `pipeline/api.py` — `index_for_instance()`, resolve index per request; per-tenant search/ingest; `manage_tenants` routes.
- `pipeline/activities.py` — ingest to the tenant index; `_minio_object_name` tenant prefix.
- `pipeline/db.py` — `tenants` table; instance predicate audit across all query paths.
- `docker-compose.yml` / `.env.example` — `MARQO_INDEX_NAMESPACE`, KC 26 image + Organizations feature flag.
- `scripts/` — a `provision_tenant` script; retire manual attribute-setting for prod.
