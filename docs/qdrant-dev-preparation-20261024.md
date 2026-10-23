# DEV Qdrant switch preparation — 2026-10-24

The user selected the 38,161-record collection for DEV. The user subsequently
confirmed DEV is down and deferred the switch until it is back. No DEV or
production route was changed and no automatic future switch was scheduled.

## Prepared on H100

Directory: `/home/aicloud/qdrant-dev-deploy-20261024`.

- Authenticated GPU query service: `docs-qdrant-dev-search`, managed by
  `compose.json`, bound to H100 private IP `10.185.25.197:8018`.
  It queries `amul-veterinary-final-20261023` (38,161 points), with tenant and
  query-visibility filters. The underlying Qdrant remains on loopback port 6336.
- Credentials are stored in server-local mode-600 `auth.env` and `deploy.env`.
  They are excluded from the source archives, patch, and Git history.
- Deployable BPP image: `amulnet/vet-kb-bpp:qdrant-dev-20261024`, immutable ID
  `sha256:d52418aeb8513bddbb69c6243a8940bb4f496de03b32ec392b3d76c05beb4ba3`.
- `bpp-image.tar.gz` plus checksum, `bpp-source.tar.gz`, `bpp-candidate.patch`,
  `preparation-files.sha256`, and `image-id.txt` preserve the candidate.
  The patch is against the recovered deployed BPP source at `5b8a7cf`.
- `compose.qdrant-dev.json` changes only the veterinary BPP image and opt-in
  `SEARCH_BACKEND=qdrant`, authenticated `QDRANT_SEARCH_URL`, and API credential.
  The BPP retains its existing reranker, diversity/backfill, catalog mapping,
  Redis queue, and callback transport.

The BPP repository remains inaccessible with the current GitHub credentials.
The tested patch and source are preserved on H100; no upstream BPP PR is claimed.

## Completed checks

- Final TypeScript build and complete veterinary BPP suite: **27 passed**.
- Actual authenticated private endpoint: EN/GU retrieval, paired isolated Beckn
  callbacks, Marqo → Qdrant → Marqo retrieval rehearsal, and rejection of an
  incorrect token passed. Missing authentication returns HTTP 401.
- Actual deployable Node 20 image with a disposable Redis and callback receiver:
  EN/GU catalogs passed; a pending callback survived a BPP container restart and
  was delivered from the durable Redis queue afterwards.
- The exact-image preflight caught a missing authentication header in the server's
  source-health probe. It now calls the authenticated Qdrant client; the rebuilt
  image above passed the complete preflight.
- Image archive checksum/compression, source patch application, shell syntax, and
  Compose overlay parsing were checked.

Evidence: `image-preflight.json`, `image-preflight.exit`, `preflight/`,
`bpp-image-build.log`, and `package-complete.txt` in the preparation directory;
`bpp-final-build.log`, `bpp-final-tests.log`, and `bpp-final-tests.exit` in
`/home/aicloud/qdrant-migration-final-20261023`.
The earlier full pipeline and collection validation evidence remains in
[qdrant-h100-migration-20261023.md](qdrant-h100-migration-20261023.md).

These checks do not establish real DEV ONIX signatures or the complete live
chat/voice route. The isolated callback receiver ACKed messages without signing.

## Remaining ingestion boundary

Live H100 `docs-pipeline-api-1` and `docs-pipeline-worker-1` still run
`docs-pipeline-pr157:1326edd` with `VECTOR_STORE_BACKEND=marqo` and
`MARQO_INDEX_NAME=amul-veterinary-rebuild-20260821`. Their configured Qdrant
collection name is the older `amul-veterinary-rebuild-20260821-qdrant`, not the
new final candidate. The Marqo source currently still reports 38,161 records and
190,341 vectors, but equal counts alone do not establish current payload parity.

Tomorrow's BPP change is a retrieval trial against the prepared snapshot. New
uploads, edits, hide/show and deletion operations from the currently deployed
pipeline are not automatically mirrored into the final Qdrant collection. Before
a lasting cutover, explicitly deploy and validate the write/lifecycle backend and
collection mapping, or establish a verified synchronisation path. Recheck source
freshness before the DEV trial; do not imply that switching the BPP also switches
ingestion. Keep this separate from the subsequently verified production rollout.

## Next session, after DEV returns

1. Verify the DEV host identity and refresh the currently running veterinary BPP,
   managed Compose files, image, signing configuration, Redis settings, and routes.
2. Securely copy the prepared image, checksum, switch script, overlay, and
   `deploy.env` to a private directory on DEV. Keep `deploy.env` mode 600.
3. Review and run `switch-dev.sh --apply` on DEV. It is prepared but has **not**
   been executed. It refuses a different hostname, checks the authenticated 38k
   endpoint before switching, verifies/loads the exact image archive, and captures
   a fresh private runtime snapshot. It resolves the original managed Compose
   files and preserves the original application settings, mounts, and networks.
   It only recreates `amul-net-bpp-vet`, with `--no-deps --no-build`.
4. The script writes `rollback.sh` before activation and restores the original
   Marqo image/settings if the update, source health, or preservation checks fail.
   Missing managed Compose interpolation values cause an early failure before
   the service switch; recover the actual managed deployment environment then.
5. Test real English/Gujarati chat and voice retrieval through seeker, ONIX,
   deployed BPP, and Qdrant. Correlate transaction IDs and signed callback ACKs,
   verify the Redis queue, and observe latency/errors. Verify rollback against the
   actual DEV deployment before considering the trial complete.

Do not run the switch script while DEV remains down. Production is a separate
subsequent rollout and requires verified production routing and configuration.
