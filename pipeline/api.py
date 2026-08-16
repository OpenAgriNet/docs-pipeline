"""Backward-compatible API façade.

Application assembly lives in :mod:`pipeline.app`, route handlers live in
:mod:`pipeline.routers`, and their shared collaborators live in
:mod:`pipeline.api_support`. Keeping this module as a one-way re-export layer
preserves the historical ``pipeline.api`` surface without making runtime
modules depend on it.
"""

import sys
from types import ModuleType

from . import api_support as _support

_SUPPORT_EXPORTS = frozenset(
    name for name in vars(_support) if not name.startswith("__")
)

from .app import app  # noqa: E402,F401
from .routers.admin import (  # noqa: E402,F401
    auth_me,
    create_marqo_index,
    get_all_audit_logs,
    get_ingest_info,
    get_marqo_index_schema,
    get_operations_queue,
    get_pipeline_stages,
    get_run,
    health,
    list_runs,
)
from .routers.content import (  # noqa: E402,F401
    delete_chunk,
    export_chunks,
    export_markdown,
    get_chunk,
    get_document_marqo_status,
    get_page,
    list_chunks,
    list_document_marqo_chunks,
    list_pages,
    reset_chunk,
    reset_page,
    resolve_provenance_chunk,
    search_chunks_across_documents,
    set_chunk_tags,
    update_chunk,
    update_page,
)
from .routers.documents import (  # noqa: E402,F401
    disable_document,
    get_document,
    get_document_allowed_actions,
    get_document_artifact,
    get_document_artifact_content,
    get_document_audit_log,
    get_document_cohorts,
    get_document_graph,
    get_document_pdf,
    get_document_runtime,
    get_document_stage_io,
    get_documents_summary,
    get_workflow_error_details,
    list_document_artifacts,
    list_document_jobs,
    list_documents,
    restore_document,
    set_document_demo,
    set_document_query_enabled,
    start_batch_workflows,
    start_document_workflow,
    update_document_metadata,
    upload_and_process,
)
from .routers.documents_actions import (  # noqa: E402,F401
    approve_chunks,
    approve_ingestion,
    approve_ocr,
    approve_translation,
    auto_tag_document_chunks,
    bulk_approve_chunks,
    bulk_approve_ocr,
    bulk_approve_translation,
    bulk_auto_tag_documents,
    bulk_reindex_documents,
    clear_reindex_required,
    mark_reindex_required,
    reconcile_document_states,
    reconcile_single_document,
    reingest_document,
    retry_chunking,
    retry_ingestion,
    retry_ocr,
    retry_translation,
)
from .routers.search import (  # noqa: E402,F401
    get_domain_tag_taxonomy,
    get_marqo_index_settings,
    get_marqo_index_stats,
    get_marqo_indexes_summary,
    get_search_settings,
    get_search_settings_audit,
    reset_search_settings,
    run_marqo_search,
    update_search_settings_endpoint,
)
from .routers.tenants import (  # noqa: E402,F401
    change_tenant_member_role_route,
    create_tenant_admin_route,
    create_tenant_index,
    create_tenant_member_route,
    create_tenant_route,
    create_tenant_taxonomy_node_route,
    delete_tenant_index,
    delete_tenant_route,
    delete_tenant_taxonomy_dimension_route,
    delete_tenant_taxonomy_node_route,
    get_tenant_taxonomy_route,
    list_tenant_indexes,
    list_tenant_members_route,
    list_tenants_route,
    reconcile_tenants_route,
    remove_tenant_member_route,
    rename_tenant_taxonomy_node_route,
    reset_tenant_member_password_route,
    suspend_tenant_route,
)


class _ApiFacadeModule(ModuleType):
    """Forward legacy helper reads and writes to :mod:`api_support`.

    Assignment forwarding matters for existing integrations that inject lazy
    clients or test doubles via ``pipeline.api.temporal_client = ...``. Runtime
    modules never import this façade, so this compatibility behavior cannot
    recreate the old dependency cycle.
    """

    def __getattribute__(self, name):
        if name not in {"_support", "_SUPPORT_EXPORTS"}:
            exports = ModuleType.__getattribute__(self, "_SUPPORT_EXPORTS")
            if name in exports:
                support = ModuleType.__getattribute__(self, "_support")
                return getattr(support, name)
        return ModuleType.__getattribute__(self, name)

    def __setattr__(self, name, value):
        exports = ModuleType.__getattribute__(self, "_SUPPORT_EXPORTS")
        if name in exports:
            support = ModuleType.__getattribute__(self, "_support")
            setattr(support, name, value)
            return
        ModuleType.__setattr__(self, name, value)

    def __dir__(self):
        return sorted(set(ModuleType.__dir__(self)) | set(self._SUPPORT_EXPORTS))


sys.modules[__name__].__class__ = _ApiFacadeModule
