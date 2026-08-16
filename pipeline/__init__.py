"""
Temporal-based document ingestion pipeline.

Components:
- workflows.py: Temporal workflow definitions
- activities.py: Retryable work units
- worker.py: Temporal worker process
- app.py: FastAPI construction and route registration
- routers/: HTTP parsing, authorization dependencies, and response handling
- services/: application operations shared by the HTTP routers
- clients.py, db.py, vector_store.py, keycloak_admin.py: infrastructure access
- models.py: Data models

Runtime dependencies point in one direction: app -> routers -> services ->
infrastructure. Services never import the FastAPI app or route modules.
"""
