Keycloak 26 realm import — docs-pipeline (tenant-isolation, clean redeploy)
==========================================================================

File: docs-pipeline-realm.json  (realm: docs-pipeline, Keycloak 26.x)

This is a CLEAN REDEPLOY, not a migration. Keycloak holds no data worth
preserving. On the 22 -> 26 cutover:

  1. Stop the stack and WIPE the Keycloak DB volume so the import runs against
     an empty database (import only runs on an empty realm):
       docker compose down
       rm -rf ./data/keycloak-db        # or your KEYCLOAK_DB_DATA_PATH
       # (or: docker volume rm <project>_keycloak-db-data)
  2. Bring the stack up — the 26 image imports this realm at first boot:
       docker compose up -d keycloak-db keycloak
  3. Re-create example orgs + users (idempotent):
       python scripts/keycloak_bootstrap_docs_pipeline.py

What the export contains
------------------------
- Organizations ENABLED (KC 26 tenant primitive), one org per example tenant:
  tenant-a, tenant-b (each carries an "instance" attribute = the tenant id).
- Groups = the per-tenant ROLE model. Top-level group == tenant/instance;
  child groups == roles (admin | content_curator | viewer):
      /tenant-a/admin  /tenant-a/content_curator  /tenant-a/viewer
      /tenant-b/admin  /tenant-b/content_curator  /tenant-b/viewer
- Realm role: master_admin (platform super-admin, instance-unrestricted).
- Four clients:
    docs-pipeline-api       bearer-only confidential resource server (validates tokens)
    docs-pipeline-ui        public SPA, Authorization Code + PKCE (S256)
    docs-pipeline-test-cli  public, Direct Access Grants (password) for local/dev
    docs-pipeline-admin     confidential SERVICE-ACCOUNT client (client_credentials only).
                            The backend uses its token to call the Keycloak Admin API
                            and create/manage Organizations, users, groups, and group
                            memberships. Its service account holds the realm-management
                            `realm-admin` role. The export ships a PLACEHOLDER secret
                            ("CHANGE_ME_ADMIN_SECRET") — regenerate at deploy (see below).
- Protocol mappers on docs-pipeline-ui and docs-pipeline-test-cli:
    groups                    group-membership, FULL PATH  -> "groups" claim
    realm-roles               realm roles (multivalued)    -> "roles" claim (carries master_admin)
    instances-claim           user attribute "instances"   -> "instances" claim
    envs-claim                user attribute "envs"         -> "envs" claim
    docs-pipeline-api-audience audience                     -> aud: docs-pipeline-api
- NO human users are in the export (they come from the bootstrap script). The only
  user is the SERVICE ACCOUNT for docs-pipeline-admin (username
  service-account-docs-pipeline-admin), which carries the realm-management
  `realm-admin` client role so the backend can drive the Admin API.
- The only client secret in the export is the docs-pipeline-admin PLACEHOLDER
  ("CHANGE_ME_ADMIN_SECRET"). It is NOT a real secret — regenerate it at deploy.
  The docs-pipeline-api / -ui / -test-cli clients ship no secret.

The docs-pipeline-admin service account (backend Admin API access)
-----------------------------------------------------------------
The backend does a client_credentials grant with docs-pipeline-admin's id + secret
and calls the Keycloak Admin API to create/manage Organizations, users, groups and
memberships. To wire it up after the realm imports:

  1. Regenerate + print the real secret (rotates the placeholder):
       python scripts/keycloak_bootstrap_docs_pipeline.py --regenerate-admin-secret
     (a plain run also re-asserts the service-account role and prints the current
      secret; use --print-admin-secret if you only want to read it.)
  2. Copy the printed value into the backend .env:
       KEYCLOAK_ADMIN_CLIENT_ID=docs-pipeline-admin
       KEYCLOAK_ADMIN_CLIENT_SECRET=<printed value>
       KEYCLOAK_REALM=docs-pipeline
       KEYCLOAK_ADMIN_BASE_URL=http://keycloak:8080/auth
  3. Restart the api (and worker) so they pick up the secret.

The bootstrap script is idempotent: it (re)asserts the realm-admin role on the
service account and reads/rotates the secret on every run.

Token / claim contract (what a docs-pipeline-ui / test-cli token carries)
-------------------------------------------------------------------------
  {
    "aud": "docs-pipeline-api",
    "groups": ["/tenant-a/content_curator", "/tenant-b/viewer"],   // full paths
    "roles": ["master_admin"],                                     // only for platform super-admins
    "instances": ["tenant-a", "tenant-b"],                         // back-compat flat list
    "envs": ["dev", "prod"]
  }

The backend parses "groups" full paths into tenant_roles = { instance: [roles] }
(split on "/": segment 1 = instance, segment 2 = role). "roles" containing
master_admin short-circuits to instance-unrestricted.

Verify the groups claim after bootstrap
---------------------------------------
  curl -s -X POST \
    http://localhost:8082/auth/realms/docs-pipeline/protocol/openid-connect/token \
    -d grant_type=password -d client_id=docs-pipeline-test-cli \
    -d username=demo-curator -d password=<generated> | \
    python3 -c 'import sys,json,base64; t=json.load(sys.stdin)["access_token"]; \
      p=t.split(".")[1]; p+="="*(-len(p)%4); \
      print(json.dumps(json.loads(base64.urlsafe_b64decode(p)), indent=2))'

Expect: "groups": ["/tenant-a/content_curator"], "instances": ["tenant-a"].
