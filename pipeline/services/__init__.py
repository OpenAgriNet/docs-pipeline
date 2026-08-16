"""Application services used by the HTTP routers.

Services depend on infrastructure and domain modules, never on the FastAPI
application or router modules.  Routers import service modules so tests can
replace collaborators at their owning seam without compatibility facades.
"""
