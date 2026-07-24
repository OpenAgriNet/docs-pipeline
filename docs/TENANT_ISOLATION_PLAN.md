# Full Tenant Isolation — Design Plan

Status: **proposed** · Scope: identity, authorization, data, search, storage, provisioning

This document plans the evolution of the pipeline from *multi-tenant-ready* (one soft
tenant dimension, one live tenant) to *fully tenant-isolated*, with **Keycloak as the
tenant control node**.

Decisions locked for this plan:

- **Keycloak Organizations** are the tenant primitive. Keycloak holds **no data worth
  preserving** — it is disposable: we move to Keycloak 26 by a **clean redeploy** (wipe,
  fresh realm import, re-create orgs + users), not a migration.
- **Namespaced index-per-tenant** for search isolation (one Marqo index per tenant).
- **Per-tenant roles** — a user can hold different roles in different tenants.
- **Data ownership is encoded in the three durable stores** that actually hold tenant
  data: the **Marqo index**, **SQLite**, and **Temporal runs**. Every persisted record in
  each is attributable to exactly one tenant. (Keycloak is the *control* plane, not a data
  store; object storage is hardened by prefix but ownership is always derivable from SQLite.)

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
- Per-tenant **workers/compute** (workers are shared). Note this is distinct from Temporal
  *run ownership*, which **is** a goal — every workflow execution is tenant-attributable
  (§5.4), even though the worker pool processing them is shared.
- Billing / metering.

---

## 2. Current state & gaps

| Layer | Today | Isolation |
|---|---|---|
| Identity (Keycloak **22.0.5**) | `instances` multivalued *user attribute* → `instances` JWT claim; realm-global roles | Soft, single realm |
| API authz | `allowed_instances()`, `assert_document_instance_access` (404 cross-tenant), all routes gated | Strong (app-enforced) |
| Data (SQLite) | `documents.instance` column, stamped on insert, immutable on update, filtered in list/summary | Row-level, shared DB |
| Search (Marqo) | **Single shared index**; `instance` is an optional field; the tolerant filter **no-ops** on the live index (no `instance` field) | **None in practice** |
| **Temporal runs** | `DocumentPipelineWorkflow` on a shared task queue; the execution itself carries **no tenant tag** — ownership is only derivable by joining the SQLite `jobs`→`documents` row | **None at the Temporal layer** |
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

### 5.2 Search (Marqo) — namespaced indexes, **many per tenant**

Marqo isolates **per index** (no in-index namespace primitive). A tenant owns **one or
more** indexes; **each index belongs to exactly one tenant.** So the tenant is a
*namespace over a set of indexes*, not a single index — a tenant can hold e.g. a
`veterinary` index, a `schemes` index, a `dev` scratch index, each with its own embedding
model/settings.

**Index registry (the source of truth).** A SQLite table `tenant_indexes` maps the
*logical* identity → the *physical* Marqo index:

| column | meaning |
|---|---|
| `instance` | owning tenant |
| `name` | logical index name **within** the tenant (unique per tenant) |
| `marqo_index` | the physical Marqo index name |
| `embedding_model` / settings | per-index config |
| `is_default` | the tenant's default target when none is specified |
| `status`, `created_at` | lifecycle |

Decoupling logical from physical buys two things: **(a) many indexes per tenant** — `(instance,
name)` is the key, `marqo_index` is 1:1 with a real index; and **(b) no forced rename of
legacy indexes** — an existing physical index is simply registered under its tenant (e.g.
the current shared index becomes its tenant's default with `marqo_index` unchanged), so the
migration touches no live index.

- **Naming for *new* indexes**: `marqo_index = f"{MARQO_INDEX_NAMESPACE}{instance}-{name}"`
  (e.g. `t-tenant-a-veterinary`). The tenant prefix keeps the boundary in the physical name;
  the suffix allows many. Legacy indexes keep their original physical name in the registry.
- **`resolve_index(instance, name=None)`** → registry lookup → the physical Marqo index
  (`name=None` → the tenant's `is_default` index). Replaces the single `MARQO_INDEX_NAME`.
- **Ingest** targets a chosen index *within the document's tenant*; the `documents` row
  records which index it lives in (see below). **Search/reads** resolve the physical index
  from `(caller tenant, optional index selection)`.
- **Authorization is index→tenant→role**: every index operation resolves the index to its
  owning tenant via the registry, then gates on the caller's role *in that tenant*. A caller
  can only address indexes owned by a tenant in their `allowed_instances`; **creating** or
  **deleting** an index needs `admin`/`pipeline` in that tenant. Cross-tenant index access is
  physically impossible (a query is only ever handed a `marqo_index` the registry confirms is
  the caller's).
- **Documents gain an index reference**: `documents.index` (the logical index name within the
  tenant) so a doc's chunks are bound to `(instance, index)`. Defaults to the tenant's default
  index.
- **Super-admin/global search** fans out across the registry's indexes explicitly, labeled
  cross-tenant.
- The per-chunk `instance` field is kept as belt-and-suspenders, but the **index is the
  isolation boundary**.
- **Deletion**: dropping one index = drop its Marqo index + registry row (+ handle its docs);
  dropping a tenant = drop *all* its registered indexes.

This supersedes the tolerant single-index filter, which remains only as the transitional
fallback until the registry is populated (§8).

### 5.3 Object storage (MinIO) — per-tenant prefix

- `_minio_object_name(...)` gains an `instance` and prefixes keys:
  `"{instance}/{workflow_id}/{artifact_type}/{filename}"`.
- All read/write/copy paths derive the prefix from the document's tenant.
- Optional hardening: a bucket per tenant + a per-tenant access policy, if physical bucket
  separation or per-tenant lifecycle/quota is required. Prefix-per-tenant is the default;
  bucket-per-tenant is a config flag.

### 5.4 Temporal runs — tenant-owned executions

Every workflow execution must be **attributable to exactly one tenant**, so that run
history, listing, retries, and reconciliation are tenant-scoped rather than reachable
across tenants. Ownership is encoded on the execution itself, not just inferred from the
SQLite join.

- **Workflow id carries the tenant**: `wf-{instance}-{document_id}` (today it's
  document-scoped only). The id becomes self-describing and collision-safe across tenants.
- **Tenant tag on the execution**: attach `instance` as a Temporal **Search Attribute**
  (`Tenant` / `Instance`) plus a **Memo**, set at `start_workflow`. This makes executions
  queryable/filterable by tenant in the Temporal API and UI without touching SQLite.
- **Scoped run APIs**: `/runs`, `/operations/queue`, run detail, and reconciliation resolve
  the caller's `allowed_instances` and filter Temporal queries by the `Instance` search
  attribute (and continue to cross-check the SQLite `jobs`→`documents` instance). A
  restricted caller can never list or open another tenant's run.
- **Ingest/activities** thread `instance` through the workflow input so activities write to
  the correct tenant index (§5.2) and storage prefix (§5.3) — the run is the thing that
  binds identity → data.

**Hard-isolation upgrade (optional):** a **Temporal namespace per tenant** (or a per-tenant
task queue) gives physical separation of workflow visibility/history — the analog of
index-per-tenant. Namespaces are heavier to provision, so the default is
*shared-namespace + tenant-tagged + scoped-queries* (full ownership + attribution), with
namespace-per-tenant as the escalation if a tenant needs a hard Temporal boundary.
Provisioning a namespace, when used, joins the create-tenant flow in §6.

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
    API->>KC: create Organization + /tenant-a/{role} groups + domain
    API->>MQ: create default index t-tenant-a-default (passage schema)
    API->>S3: ensure prefix / bucket + policy
    API->>DB: insert tenants row + tenant_indexes default row
    Note over API: (optional) provision per-tenant Temporal namespace
    API-->>Admin: tenant ready; invite members via KC org
```

- **Registry.** A `tenants` table (id, display name, status, created_at) mirrors the KC orgs
  for listing/validation without a KC round-trip; a `tenant_indexes` table (§5.2) is the
  per-tenant index registry.
- **Tenant creation** provisions the org + group tree + a **default** index + storage prefix
  + registry rows in one operation.
- **Index management (many per tenant, self-service later).** A `manage_indexes` API —
  `POST /tenants/<instance>/indexes` (name + embedding/settings), `GET /tenants/<instance>/indexes`,
  `DELETE /tenants/<instance>/indexes/<name>` — lets an authorized member of a tenant create
  and manage **additional** indexes within it at any time. Gated by `admin`/`pipeline` in that
  tenant; creates the Marqo index `t-<instance>-<name>` and its `tenant_indexes` row. This is
  the "create indexes using all of this later" capability — indexes are first-class, tenant-owned,
  and not limited to one per tenant.
- **Suspend** = disable the org (members can't get tokens) — data retained.
- **Delete** = disable org → drop **all** the tenant's Marqo indexes → delete objects by
  prefix/bucket → soft/hard-delete its `documents` + `tenant_indexes` rows → remove the org.
- **Onboarding** uses Keycloak org **invitations** / email-domain self-registration instead
  of the manual bootstrap attribute path (which remains for local/dev).

---

## 7. Keycloak 26 — clean redeploy (no migration)

Organizations need Keycloak **26+**, and **Keycloak holds no data worth preserving** — so
this is a *redeploy*, not a migration. That removes the single biggest risk from the plan
and un-gates everything downstream.

- Bump the `keycloak` image to **26.x** in `docker-compose.yml`; **wipe the `keycloak-db`
  volume** (and its data path) so it starts clean.
- Ship a **fresh realm export** in `keycloak/import/` for the new version, containing: the
  three clients (`docs-pipeline-api` bearer-only, `docs-pipeline-ui` public/PKCE,
  `docs-pipeline-test-cli`), the `tenant_roles` + `instances`/`envs` protocol mappers, and
  **Organizations enabled**.
- Re-apply the proxy/issuer config for 26 (the `KC_PROXY=edge` form is superseded by
  `--proxy-headers=xforwarded` in newer majors — set the 26 equivalent), keep
  `KEYCLOAK_ISSUER` pointing at the public issuer.
- **Re-create orgs + users** from scratch via the provisioning path (§6) / an updated
  bootstrap script. Because nothing is being preserved, there is no export/import fidelity
  risk and no stepwise 22→24→26 cadence — a single cutover.
- Because it's disposable, **no Groups interim is needed** — go straight to Organizations.

The only real caution: this invalidates all existing sessions/tokens and the current test
users, so do it in a maintenance moment and re-run the bootstrap immediately after.

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
4. **Temporal.** New runs get the tenant workflow-id + `Instance` search attribute from day
   one; in-flight runs simply complete under the old scheme (they already resolve tenant
   via SQLite). No backfill of historical executions is required — ownership of *new* runs
   is what matters, and old completed runs remain joinable via `jobs`→`documents`.
5. **Identity.** Trivial — Keycloak is redeployed clean (§7); simply create the Organization
   for the existing tenant and re-create its users. No membership migration.
6. Remove the tolerant single-index fallback once all tenants are on per-tenant indexes.

---

## 9. Phased rollout

1. **Backend authz refactor** to per-tenant roles + `tenant_roles` claim — inert under
   `AUTH_DISABLED=true`. (No infra change.)
2. **Ownership tagging in the durable stores** — `instance` threaded through the workflow
   input; tenant workflow-id + `Instance` search attribute/memo on start; instance-scoped
   `/runs` + reconciliation; MinIO tenant prefix. (Mostly code; no infra.)
3. **Keycloak 26 clean redeploy** — bump image, wipe volume, fresh realm with Organizations,
   re-bootstrap orgs + users. Low risk (nothing preserved).
4. **Search: namespaced index-per-tenant** — `index_for_instance`, provision + migrate the
   existing tenant's index, flip reads, retire the tolerant fallback.
5. **`manage_tenants` provisioning API** + `tenants` registry; onboarding via org
   invitations; (optional) per-tenant Temporal namespace in the provisioning flow.
6. **Cutover & harden** — remove single-tenant assumptions; add cross-tenant isolation tests
   (a tenant-A token gets 404/empty on every tenant-B doc, chunk, artifact URL, **run**, and
   search).

Phases 1–2 are pure code and can proceed immediately; phase 3 (KC redeploy) is now cheap and
un-gates 4–5.

---

## 10. Risks & open items

- **KC 26 redeploy is now low-risk** (disposable — wipe + fresh realm + re-bootstrap). The
  only cost is invalidating current sessions/users; do it in a maintenance moment.
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
- `pipeline/api.py` — `resolve_index(instance, name=None)` (registry-backed, replaces the single `MARQO_INDEX_NAME`); per-tenant/per-index search + ingest; instance-scoped `/runs` + reconciliation; `manage_tenants` + `manage_indexes` routes (index→tenant→role gating).
- `pipeline/activities.py` — ingest to the resolved `(instance, index)` Marqo index; `_minio_object_name` tenant prefix.
- `pipeline/workflows.py` — thread `instance` (and target index) through workflow input; tenant workflow-id; set the `Instance` Temporal search attribute + memo on `start_workflow`.
- `pipeline/db.py` — `tenants` table + **`tenant_indexes` registry** (`instance`,`name`,`marqo_index`,settings,`is_default`); `documents.index` column; a `seed_tenant_indexes` migration that registers the existing physical index as its tenant's default; instance predicate audit across all query paths.
- `docker-compose.yml` / `.env.example` — `MARQO_INDEX_NAMESPACE`, **Keycloak 26 image**, Organizations feature flag, `--proxy-headers`, the Temporal search-attribute registration.
- `keycloak/import/` — fresh 26 realm export (clients + `tenant_roles` mapper + Organizations).
- `scripts/` — a `provision_tenant` script; retire manual attribute-setting for prod.
