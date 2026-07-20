# Place a Keycloak realm export here as:
#   docs-pipeline-realm.json
#
# Import runs only on first start (empty keycloak-db volume).
# Generate/update with the admin APIs, or copy a configured export from your Keycloak host:
#   ./keycloak/import/docs-pipeline-realm-configured.json
#
# After import, seed clients/roles/mappers and example role fixtures:
#   python scripts/keycloak_bootstrap_docs_pipeline.py
#
# Example fixtures (same KEYCLOAK_TEST_USER_PASSWORD for all):
#   docs-master-admin  → master_admin   (tenant-a/b/c, dev+prod)
#   docs-admin         → admin          (tenant-a/b,   dev+prod)
#   docs-test-curator  → content_curator (tenant-a,    dev)
#   docs-viewer        → viewer         (tenant-a,     dev)
#
# Skip fixtures:  python scripts/keycloak_bootstrap_docs_pipeline.py --no-seed-fixtures
