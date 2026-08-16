"""
Temporal-based document ingestion pipeline.

Components:
- workflows.py: Temporal workflow definitions
- activities.py: Retryable work units
- worker.py: Temporal worker process
- app.py: FastAPI construction and route registration
- routers/: REST handlers
- api_support.py: shared route collaborators
- api.py: backwards-compatible import façade
- models.py: Data models
"""
