# Multi-tenant Marqo migration (operator runbook)

This is the **Phase 2 / issue #18** follow-up for moving from a single shared
index to enforced tenant isolation. It is **not automatic** — run it only when
you are about to serve more than one tenant from the same Marqo deployment.

## Current (V1) behaviour

- Ingest stamps an `instance` field on every new chunk.
- Search AND-s an `instance:(...)` filter **only when** the live index advertises
  a filterable `instance` field.
- Legacy indexes without that field keep working (filter is skipped).
- SQLite `documents.instance` is the control-plane source of truth for API
  tenancy; Marqo filtering is the search-plane counterpart.

## When you need this

| Situation | Action |
|-----------|--------|
| Single tenant, auth on | Nothing — optional backfill for cleanliness |
| Second tenant about to go live | Follow the steps below |
| Prefer hard isolation (drop/reindex per tenant) | Use **index-per-tenant** (Option B) |

## Option A — Single index + `instance` filter (shipped tolerant path)

1. Confirm schema:
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" \
     "$API/admin/index/schema?index_name=$MARQO_INDEX_NAME"
   ```
2. If `instance` is missing on a **structured** index, recreate with the passage
   schema (includes `instance`) and reingest — see
   `scripts/bulk_reingest_sqlite_to_marqo.py`. Do **not** expect an in-place
   field add on structured indexes.
3. If the index is unstructured (or already has `instance`), backfill:
   ```bash
   python3 scripts/backfill_marqo_instance.py \
     --index "$MARQO_INDEX_NAME" \
     --instance tenant-a \
     --only-missing \
     --dry-run
   # then drop --dry-run
   ```
4. Verify a scoped curator token only sees that tenant’s hits on
   `POST /marqo/search` and `GET /chunks/search`.

## Option B — Index-per-tenant (stronger isolation)

Use when tenants must not share a corpus or when you want independent lifecycle
(drop/reindex/settings) per tenant.

1. Create one index per tenant via `POST /admin/index/create` (schema already
   includes `instance`).
2. Map tenants → indexes in the deploy env, for example:
   ```bash
   MARQO_INDEX_BY_INSTANCE=tenant-a:docs-tenant-a,tenant-b:docs-tenant-b
   MARQO_INDEX_NAME=documents-index   # default / legacy tenant
   ```
3. Point ingest + search at the resolved index for the document’s `instance`
   (wiring lives with the operator until a follow-up PR threads the registry
   through activities/search). Until that lands, keep Option A.
4. No shared-index backfill is required for tenant #2 — start empty.

## Safety checklist

- [ ] Backup / confirm reingest path before mutating a live index
- [ ] Run off-peak; start with `--dry-run` on the backfill script
- [ ] Keep `AUTH_DISABLED=false` only after UI Bearer login is verified
- [ ] Confirm SQLite `documents.instance` values before stamping Marqo

## Related

- `scripts/backfill_marqo_instance.py`
- `scripts/bulk_reingest_sqlite_to_marqo.py`
- GitHub issue #18 follow-ups
- `docs/DESIGN.md` §5 (search) and §11 (roadmap)
