"""Temporal SDK adapter for document-pipeline orchestration.

HTTP routers must access this package through ``pipeline.services``. Worker
registration, workflow definitions, activity implementations, and the lazy
Temporal client live here so the SDK boundary is explicit in import paths.
"""
