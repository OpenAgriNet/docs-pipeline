#!/usr/bin/env bash
set -euo pipefail

# Deploy-time Keycloak bootstrap for the docs-pipeline stack.
#
# Runs scripts/keycloak_bootstrap_docs_pipeline.py against a freshly imported
# realm, teaching the public `docs-pipeline-ui` client the deployment's real
# browser-facing origin (UI_PUBLIC_URL), then validates the result end-to-end so
# a broken login surface fails the deploy instead of shipping silently.
#
# Why this exists: a clean Keycloak 26 import only allows the wildcard/localhost
# redirect patterns from the realm export. The real UI is served at a
# deployment-specific origin, so browser login 400s with
# "Invalid parameter: redirect_uri" until that origin is added to the client.
# This is a redirect-URI-per-deployment step: it must run on every fresh import.
#
# Configuration (environment variables):
#   KEYCLOAK_URL                     Base Keycloak URL INCLUDING the relative path
#                                    (e.g. http://localhost:8082/auth).
#                                    Default: http://localhost:8082/auth
#   KEYCLOAK_ADMIN                   Bootstrap admin username. Default: admin
#   KEYCLOAK_ADMIN_PASSWORD          Bootstrap admin password. REQUIRED.
#   KEYCLOAK_REALM                   Realm to target. Default: docs-pipeline
#   UI_PUBLIC_URL                    REQUIRED. Browser-facing UI origin(s),
#                                    space/comma-separated (e.g. https://ui.example.com).
#   KEYCLOAK_BOOTSTRAP_PASSWORD_FILE Optional path; the python step writes the
#                                    generated example-user passwords there (mode 0600).
#   REGENERATE_ADMIN_SECRET          Set to 1 to rotate the docs-pipeline-admin
#                                    client secret during this run.
#
# Validations (any failure exits non-zero):
#   1. Redirect accepted  - the OIDC authorize endpoint does NOT reject
#      UI_PUBLIC_URL with an "Invalid parameter: redirect_uri" 400.
#   2. Service account     - a client_credentials grant for docs-pipeline-admin
#      returns an access_token (proves the backend can call the Admin API).
#
# Secrets (the admin client secret, admin password, tokens) are never printed.

# --- Config + defaults -------------------------------------------------------
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8082/auth}"
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-docs-pipeline}"
UI_PUBLIC_URL="${UI_PUBLIC_URL:-}"
KEYCLOAK_BOOTSTRAP_PASSWORD_FILE="${KEYCLOAK_BOOTSTRAP_PASSWORD_FILE:-}"
REGENERATE_ADMIN_SECRET="${REGENERATE_ADMIN_SECRET:-0}"

UI_CLIENT_ID="docs-pipeline-ui"
ADMIN_CLIENT_ID="docs-pipeline-admin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_PY="${SCRIPT_DIR}/keycloak_bootstrap_docs_pipeline.py"

# Strip any trailing slash so URL concatenation is predictable.
KEYCLOAK_URL="${KEYCLOAK_URL%/}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

# --- Pre-flight validation ---------------------------------------------------
echo "== Pre-flight validation =="

[ -n "$KEYCLOAK_ADMIN_PASSWORD" ] || fail "KEYCLOAK_ADMIN_PASSWORD is required."
[ -n "$UI_PUBLIC_URL" ] || fail "UI_PUBLIC_URL is required (the browser-facing UI origin, e.g. https://ui.example.com)."
[ -f "$BOOTSTRAP_PY" ] || fail "bootstrap script not found at $BOOTSTRAP_PY"
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH."
command -v curl >/dev/null 2>&1 || fail "curl not found on PATH."

# UI_PUBLIC_URL may carry several space/comma-separated origins; each must be http(s).
_ui_url_re='^https?://[^[:space:]/]+'
for _u in ${UI_PUBLIC_URL//,/ }; do
  [ -n "$_u" ] || continue
  echo "$_u" | grep -Eq "$_ui_url_re" \
    || fail "UI_PUBLIC_URL entry '$_u' does not look like an http(s) origin."
done

# The first origin is the one used for the redirect validation below.
FIRST_UI_URL="$(echo "$UI_PUBLIC_URL" | tr ',' ' ' | awk '{print $1}')"
FIRST_UI_URL="${FIRST_UI_URL%/}"

OIDC_CONFIG_URL="${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/.well-known/openid-configuration"
curl -fsS "$OIDC_CONFIG_URL" >/dev/null \
  || fail "Keycloak not reachable / realm '$KEYCLOAK_REALM' missing at $OIDC_CONFIG_URL"

echo "  OK: required vars present, UI_PUBLIC_URL looks valid, Keycloak reachable."

# --- Run the python bootstrap ------------------------------------------------
echo "== Running keycloak bootstrap =="

PY_ARGS=(
  "$BOOTSTRAP_PY"
  --base-url "$KEYCLOAK_URL"
  --realm "$KEYCLOAK_REALM"
  --ui-public-url "$UI_PUBLIC_URL"
)
if [ "$REGENERATE_ADMIN_SECRET" = "1" ]; then
  PY_ARGS+=(--regenerate-admin-secret)
fi

# Admin creds + optional password file are consumed by the python step via env.
export KEYCLOAK_ADMIN KEYCLOAK_ADMIN_PASSWORD
if [ -n "$KEYCLOAK_BOOTSTRAP_PASSWORD_FILE" ]; then
  export KEYCLOAK_BOOTSTRAP_PASSWORD_FILE
fi

# Capture stdout (it contains the sensitive KEYCLOAK_ADMIN_CLIENT_SECRET line);
# do NOT echo it verbatim. Let the exit status propagate under `set -e`.
BOOTSTRAP_OUT="$(python3 "${PY_ARGS[@]}")"

# Echo a redacted view so operators see progress without leaking the secret.
echo "$BOOTSTRAP_OUT" | sed -E 's/^(KEYCLOAK_ADMIN_CLIENT_SECRET=).*/\1<redacted>/'

# Extract the admin client secret into a shell var for validation #2. Never printed.
ADMIN_CLIENT_SECRET="$(printf '%s\n' "$BOOTSTRAP_OUT" \
  | sed -n 's/^KEYCLOAK_ADMIN_CLIENT_SECRET=//p' | head -n1)"

# --- Post-deploy validation --------------------------------------------------
echo "== Post-deploy validation =="

CHECK1_OK=0
CHECK2_OK=0

# 1) Redirect accepted: the authorize endpoint must NOT reject the real origin
#    with a 400 "Invalid parameter: redirect_uri".
AUTH_ENDPOINT="${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/auth"
AUTH_QS="client_id=${UI_CLIENT_ID}&redirect_uri=${FIRST_UI_URL}/&response_type=code&scope=openid&code_challenge=x&code_challenge_method=S256"

# Capture body + status; Keycloak returns 400 with an error page when the
# redirect_uri is not allowed, and 200/302 (login form / redirect) when it is.
AUTH_RESPONSE="$(curl -sS -o - -w '\n__HTTP_STATUS__=%{http_code}' \
  "${AUTH_ENDPOINT}?${AUTH_QS}" || true)"
AUTH_STATUS="$(printf '%s' "$AUTH_RESPONSE" | sed -n 's/.*__HTTP_STATUS__=//p' | tail -n1)"
AUTH_BODY="$(printf '%s' "$AUTH_RESPONSE" | sed 's/__HTTP_STATUS__=[0-9]*$//')"

if [ "$AUTH_STATUS" = "400" ] || printf '%s' "$AUTH_BODY" | grep -qi 'Invalid parameter: redirect_uri'; then
  echo "  FAIL check 1 (redirect): ${UI_CLIENT_ID} rejected redirect_uri ${FIRST_UI_URL}/ (HTTP ${AUTH_STATUS})."
  echo "         The UI origin is NOT allowed — browser login will 400."
else
  echo "  PASS check 1 (redirect): ${UI_CLIENT_ID} accepts ${FIRST_UI_URL}/ (HTTP ${AUTH_STATUS})."
  CHECK1_OK=1
fi

# 2) Service account works: client_credentials grant for docs-pipeline-admin
#    must return an access_token (proves the backend can call the Admin API).
if [ -z "$ADMIN_CLIENT_SECRET" ]; then
  echo "  FAIL check 2 (service account): no ${ADMIN_CLIENT_ID} secret in bootstrap output."
else
  TOKEN_ENDPOINT="${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token"
  # --data-urlencode keeps the secret off the process arg list where practical;
  # the response (which contains a token) is inspected but never printed.
  TOKEN_RESPONSE="$(curl -sS \
    -d 'grant_type=client_credentials' \
    --data-urlencode "client_id=${ADMIN_CLIENT_ID}" \
    --data-urlencode "client_secret=${ADMIN_CLIENT_SECRET}" \
    "$TOKEN_ENDPOINT" || true)"
  if printf '%s' "$TOKEN_RESPONSE" | grep -q '"access_token"'; then
    echo "  PASS check 2 (service account): ${ADMIN_CLIENT_ID} client_credentials grant returned an access_token."
    CHECK2_OK=1
  else
    echo "  FAIL check 2 (service account): ${ADMIN_CLIENT_ID} client_credentials grant returned no access_token."
  fi
fi

# --- Summary -----------------------------------------------------------------
echo "== Summary =="
[ "$CHECK1_OK" = "1" ] && echo "  [PASS] redirect URI accepted for $FIRST_UI_URL" || echo "  [FAIL] redirect URI"
[ "$CHECK2_OK" = "1" ] && echo "  [PASS] service-account client_credentials grant" || echo "  [FAIL] service-account grant"

if [ "$CHECK1_OK" = "1" ] && [ "$CHECK2_OK" = "1" ]; then
  echo "Deploy bootstrap: ALL CHECKS PASSED."
  exit 0
fi
echo "Deploy bootstrap: ONE OR MORE CHECKS FAILED." >&2
exit 1
