"""Marqo search, index views, search settings and taxonomy."""

from fastapi import APIRouter, HTTPException, Header, Query
from typing import Optional
from .. import db, vector_store
from ..auth.deps import RequirePipeline, RequirePlatformAdmin, RequireSearch
from ..auth.tenancy import assert_instance_access
from ..models import SearchSettings, SearchSettingsUpdate, SettingsAuditResponse
from ..services import access, indexes, search as search_service

router = APIRouter()


@router.get("/taxonomy/domain-tags")
async def get_domain_tag_taxonomy(
    user: RequireSearch,
    instance: Optional[str] = Query(None, description="Tenant whose taxonomy to read; defaults to the caller's tenant"),
):
    """Return the domain tag taxonomy for UI editors, scoped to the caller's tenant.

    The taxonomy is now per-tenant (DB-backed, seeded from the shipped default).
    An explicit ``?instance=`` is honoured for a caller with access to it;
    otherwise the caller's own tenant is resolved. A tenant that has not been
    seeded yet transparently falls back to the shipped file default.
    """
    from ..domain_tags.service import get_taxonomy_for_api

    inst = access.resolve_taxonomy_read_instance(user, instance)
    return get_taxonomy_for_api(inst)


@router.get("/marqo/indexes/summary")
async def get_marqo_indexes_summary(
    user: RequireSearch,
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled"),
):
    """Summarize index coverage from SQLite-backed index status plus live Marqo stats."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    # Scope to the caller's tenants so a tenant admin never sees other tenants'
    # index names / document + chunk counts. None = data-unrestricted (bypass).
    summaries = db.list_index_summaries(
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=access.instance_scope_for_user(user),
    )
    if not summaries:
        return []

    store = vector_store.get_vector_store()

    results = []
    for summary in summaries:
        live_stats = None
        live_error = None
        has_domain_tags_field = None
        try:
            live_stats = store.get_stats(summary["index_name"])
        except vector_store.VectorStoreError as exc:
            live_error = str(exc)
        try:
            has_domain_tags_field = "domain_tags" in store.field_names(summary["index_name"])
        except vector_store.VectorStoreError:
            has_domain_tags_field = None
        results.append({
            **summary,
            "live_stats": live_stats,
            "live_error": live_error,
            "has_domain_tags_field": has_domain_tags_field,
        })
    return results

@router.get("/marqo/indexes/{index_name}/settings")
async def get_marqo_index_settings(index_name: str, user: RequireSearch):
    # Only expose metadata for an index the caller's tenant owns (404 otherwise).
    access.assert_marqo_index_access(user, index_name)
    return vector_store.get_vector_store().get_settings(index_name)


@router.get("/marqo/indexes/{index_name}/stats")
async def get_marqo_index_stats(index_name: str, user: RequireSearch):
    access.assert_marqo_index_access(user, index_name)
    return vector_store.get_vector_store().get_stats(index_name)


@router.get("/marqo/indexes/{index_name}/documents")
async def list_marqo_index_documents(
    index_name: str,
    user: RequirePipeline,
    mode: str = Query(
        "stale",
        description="stale = reindex_required only; all = stale plus completed/ready/chunk_review",
    ),
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled"),
):
    """List workflow ids eligible to reindex for one physical index.

    Used by the Indexes console so bulk reindex is scoped to the card's index
    instead of every document in the deployment. Pass the same physical
    ``index_name`` into ``POST /documents/bulk/reindex``; ingest honors it when
    the index is registered to the document's tenant.
    """
    if mode not in {"stale", "all"}:
        raise HTTPException(400, "mode must be 'stale' or 'all'")
    access.assert_marqo_index_access(user, index_name)
    include_demo = bool(x_include_demo and x_include_demo.lower() == "true")
    include_disabled = bool(x_include_disabled and x_include_disabled.lower() == "true")
    workflow_ids = db.list_workflow_ids_for_index(
        index_name,
        mode=mode,
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=access.instance_scope_for_user(user),
    )
    return {
        "index_name": index_name,
        "mode": mode,
        "total": len(workflow_ids),
        "workflow_ids": workflow_ids,
    }


@router.post("/marqo/search")
async def run_marqo_search(payload: dict, user: RequireSearch):
    settings = db.get_search_settings()
    # Index selection, in priority order:
    #  1. explicit (instance, index logical name) -> registry resolve + access
    #  2. explicit physical index_name -> reverse-resolve to its owning tenant
    #  3. the caller's instance default (or the configured default index)
    # Cross-tenant / unknown indexes 404; restricted callers are tenant-scoped
    # by the per-chunk `instance` filter, or fail-closed on legacy indexes.
    requested_instance = payload.get("instance")
    requested_index = payload.get("index")
    requested_physical = payload.get("index_name")
    if requested_instance or requested_index:
        target_instance = assert_instance_access(user, requested_instance)
        index_name = access.assert_index_access(user, target_instance, requested_index)
    elif requested_physical:
        index_name = access.assert_marqo_index_access(user, requested_physical)
    else:
        # No explicit target. A RESTRICTED caller must NEVER fall through to the
        # configured default index (`settings.indexName` / `_default_physical_index`)
        # — that is the DEFAULT tenant's corpus. Resolve the caller's own
        # instances instead; only an unrestricted / bypass caller may use the
        # configured default.
        scope = access.instance_scope_for_user(user)
        if scope is None:
            index_name = settings.get("indexName") or vector_store.default_physical_index()
        else:
            resolved: list[str] = []
            for inst in scope:
                physical = indexes.resolve_index(inst)  # name=None -> tenant default (or None)
                if physical and physical not in resolved:
                    resolved.append(physical)
            if len(resolved) == 1:
                index_name = resolved[0]
            elif not resolved:
                # None of the caller's tenants has an index -> empty result.
                index_name = None
            else:
                raise HTTPException(
                    400,
                    "Multiple indexes in your scope; specify ?instance= (or instance/index in the body).",
                )
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")

    # The caller's tenant(s) have no index of their own (explicit own instance
    # that resolved to no registered index, or an implicit-scope restricted caller
    # whose tenants have no index). Return an EMPTY result immediately: never
    # query Marqo, never fall back to another tenant's (the default's) physical
    # index. Only an unrestricted / bypass caller reaches the configured default.
    if index_name is None:
        return search_service.empty_search_result(
            query, include_raw_hits=bool(payload.get("include_raw_hits"))
        )

    store = vector_store.get_vector_store()
    index_view = indexes.IndexSettingsView(store, index_name)
    instance_filter = indexes.marqo_instance_filter(user, index_view)
    try:
        result = search_service.run_search(
            index_name=index_name,
            query=query,
            settings=settings,
            payload=payload,
            instance_filter=instance_filter,
            store=store,
        )
    except search_service.SearchServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if indexes.legacy_search_blocked(instance_filter):
        result["effective_config"]["tenant_scope"] = "blocked_legacy_index"
        result["effective_config"]["tenant_scope_reason"] = (
            "Index has no filterable instance field; restricted search is fail-closed."
        )
    elif (
        access.instance_scope_for_user(user) is not None
        and instance_filter is None
        and not vector_store.index_has_instance_field(index_view)
    ):
        result["effective_config"]["tenant_scope"] = "unscoped_legacy_override"
        result["effective_config"]["tenant_scope_reason"] = (
            "ALLOW_UNSCOPED_LEGACY_SEARCH is on; tenant filter was not applied."
        )
    return result


@router.get("/settings/search", response_model=SearchSettings)
async def get_search_settings(user: RequireSearch):
    """
    Get current search settings.

    Read-only search configuration. Gated with ``RequireSearch`` (not
    ``RequireAdmin``) because any search-capable user needs these defaults
    (index name, method, limits) to drive the search UI; mutating the
    settings (PUT / reset) still requires ``RequireAdmin``.

    Returns the current search configuration including:
    - searchMethod: TENSOR, LEXICAL, or HYBRID
    - limit: Number of results to return
    - alpha: Balance between lexical (0) and semantic (1) for hybrid search
    - rankingMethod: rrf or normalize_linear for hybrid search
    - showHighlights: Whether to show highlighted matches
    - efSearch: HNSW search accuracy parameter
    """
    return db.get_search_settings()


@router.put("/settings/search", response_model=SearchSettings)
async def update_search_settings_endpoint(settings: SearchSettingsUpdate, user: RequirePlatformAdmin):
    """
    Update search settings.

    Only provided fields will be updated. Changes are logged to the audit trail.

    These are GLOBAL, cross-tenant settings (notably the default ``indexName``),
    so mutation is restricted to the platform super-admin
    (``RequirePlatformAdmin``) — a per-tenant ``admin`` must not be able to
    repoint every tenant's search at an index of its choosing.
    """
    _ = user
    # Convert to dict, excluding None values
    updates = {k: v for k, v in settings.model_dump().items() if v is not None}

    if not updates:
        raise HTTPException(400, "No settings provided to update")

    return db.update_search_settings(updates)


@router.get("/settings/search/audit", response_model=SettingsAuditResponse)
async def get_search_settings_audit(
    user: RequirePlatformAdmin,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get audit trail for search settings changes.

    Shows all historical changes to search settings with old/new values. This is
    the change history of the GLOBAL settings, so it is restricted to the
    platform super-admin (``RequirePlatformAdmin``).
    """
    logs = db.get_settings_audit_logs(limit=limit, offset=offset)
    total = db.get_settings_audit_count()

    return SettingsAuditResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@router.post("/settings/search/reset", response_model=SearchSettings)
async def reset_search_settings(user: RequirePlatformAdmin):
    """
    Reset search settings to defaults.

    Resets all search settings to their default values:
    - searchMethod: HYBRID
    - limit: 10
    - alpha: 0.7
    - rankingMethod: rrf
    - showHighlights: true
    - efSearch: 256
    """
    _ = user
    defaults = {
        "searchMethod": "HYBRID",
        "limit": 12,
        "alpha": 0.6,
        "rankingMethod": "rrf",
        "showHighlights": True,
        "efSearch": 256,
        "indexName": "documents-index",
        "candidateCap": 120,
        "candidateMultiplier": 10,
        "maxChunksPerDoc": 2,
        "useE5Prefix": True,
        "excludeReference": True,
        "queryExpansionProfile": "gu-v1",
        "rerankMode": "none",
        "hybridRrfK": 60,
    }
    return db.update_search_settings(defaults)
