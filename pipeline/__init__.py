"""
Temporal-based document ingestion pipeline.

Components:
- temporal/: Temporal client, document workflows/tasks, and worker process
- worker.py: Backward-compatible Temporal worker launcher
- app.py: FastAPI construction and route registration
- routers/: HTTP parsing, authorization dependencies, and response handling
- services/: application operations shared by the HTTP routers
- storage/, db.py, vector_store.py, keycloak_admin.py: infrastructure access
- ingestion_records.py: SDK-independent vector-record and provenance construction
- models.py: Data models

Runtime dependencies point in one direction: app -> routers -> services ->
infrastructure. Services never import the FastAPI app or route modules.
"""
