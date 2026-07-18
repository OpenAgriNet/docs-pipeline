# Place a Keycloak realm export here as:
#   amul-vistaar-realm.json
#
# Import runs only on first start (empty keycloak-db volume).
# Generate/update with admin APIs or copy from H100:
#   ~/docs-pipeline-keycloak-test/import/amul-vistaar-realm-configured.json
#
# After import, ensure test user exists:
#   python scripts/keycloak_bootstrap_docs_pipeline.py
