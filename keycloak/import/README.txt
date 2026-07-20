# Place a Keycloak realm export here as:
#   docs-pipeline-realm.json
#
# Import runs only on first start (empty keycloak-db volume).
# Generate/update with the admin APIs, or copy a configured export from your Keycloak host:
#   ./keycloak/import/docs-pipeline-realm-configured.json
#
# After import, ensure test user exists:
#   python scripts/keycloak_bootstrap_docs_pipeline.py
