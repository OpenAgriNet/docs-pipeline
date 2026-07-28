Keycloak import / tenant access
==============================

partial-tenant-groups-and-roles.json
  Partial import of product roles + group tree for multi-state access:
    /global/super-admin
    /states/{STATE}/{contributor|reviewer}
  Use on an EXISTING realm (e.g. bharat-vistaar):
    Admin Console → Realm settings → Partial import → Skip existing

docs-pipeline-realm.json
  Older local-dev realm export (legacy roles). Prefer partial import +
  bootstrap script for the multi-state model.

Bootstrap (idempotent groups + roles + groups claim mapper)
----------------------------------------------------------
  python scripts/keycloak_bootstrap_tenant_groups.py \
    --base-url https://dev-auth-vistaar.da.gov.in/auth \
    --realm bharat-vistaar \
    --admin-password "$KEYCLOAK_ADMIN_PASSWORD"

App behaviour
-------------
SSO JWT carries groups; API maps them to instances + roles.
UI profile panel shows role capabilities.
See: docs/keycloak-tenant-roles.md
