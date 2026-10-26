# DEV Qdrant retrieval cutover — 2026-10-26

**Superseded later on 2026-10-26:** the user selected native Qdrant ingestion.
API/worker now write directly to Qdrant and the mirror is retired. See
[the native deployment report](qdrant-native-dev-20261026.md). The mirror details
below are historical; do not restart it over native Qdrant data.

DEV chat and the registered voice veterinary search tool now retrieve through
the veterinary BPP from `amul-veterinary-final-20261023` on H100, **38,161 points**
at cutover. Actual English/Gujarati chat and voice HTTP turns passed, with search
tool calls and source-bearing results verified in their saved histories.

The effective **public** path is VM3 nginx → VM2 unified `amul_app` at
`10.5.25.12:8000` → VM5 seeker/BPP → H100 Qdrant query service. Public HTTPS
English/Gujarati tests passed at `api.dev.amulai.in/api/chat/` and
`voice.dev.amulai.in/api/voice/`; every saved session contains retrieval tool
results. VM2 retained its original `ba3a8275bf5551344eceaf0c7217212000741eda` base
image/source, JWT/Firebase mounts, environment values, networks, ports and memory.
Only the narrow voice adapter patch and `VET_SEARCH_BACKEND=beckn` were applied.
The gateway was inspected, not changed.

The initial failure investigation had targeted still-running VM5 legacy apps.
Those repairs work, but their direct-port tests alone were insufficient to prove
the public route. The final ingress audit identified VM2 and the additional
primary voice patch was applied/tested there. Its existing Redis/model config and
local JWT issuer were healthy; no issuer delegation or signing-key change was
needed on VM2. The VM5 fixes below describe secondary legacy instances.

Marqo remains the ingestion authority for the existing shared pipeline. A managed
H100 worker mirrors accepted source updates into Qdrant. This completes the DEV
retrieval cutover with eventual synchronization; it does not switch production
routing or deploy a native Qdrant pipeline writer.

## Repairs

- Restored DEV chat Redis connectivity and seeker network membership, preserving
  all existing environment values, image and bind mounts. Its legacy Compose
  invocation had omitted the settings already present in the managed infra config.
- Restored the intended `/app/pipelines/dev/chat.yaml` configuration. Previously,
  missing endpoint/config settings selected default OpenAI models with a dummy
  key, producing fallback answers despite HTTP 200. Reused the existing matching
  DEV inference credentials privately; their original values were unchanged.
- Added explicit `VET_SEARCH_BACKEND=beckn` support to the registered voice tool.
  Its query validation and result limits remain; the BPP retains backend selection,
  reranking and diversity. Default Marqo behavior remains for unconfigured deployments.
- Fixed voice anonymous-token issuance using explicit delegation to the existing
  chat signer. Voice retains its public verification key; no private signing key
  was copied into it. Malformed or failed issuer responses return a clean 503.
- Persisted both voice settings in the managed DEV Compose source. Its deploy
  preflight refuses old app revisions that ignore the Beckn route or issuer setting.
- Backed up the veterinary callback due index and atomically removed 32 entries
  whose job records had already expired. Kept the one existing job and its payload;
  no valid job was deleted or manually replayed.

API changes: [amul-oan-api PR #460](https://github.com/OpenAgriNet/amul-oan-api/pull/460).
Managed deployment changes:
[amul-infra PR #183](https://github.com/OpenAgriNet/amul-infra/pull/183).
Both are draft review artifacts. Public VM2 uses the narrow retrieval patch over
its `ba3a8275bf55` source. Legacy VM5 voice uses retrieval/issuer patches over
`b4f3313e6919a94955b7788a0265555b101660f7`. Neither received an unrelated full main
update. Managed VM2 and legacy VM5 deployment files both retain their route flags.

## Ingestion synchronization

- Managed services: `docs-qdrant-dev-mirror` and `docs-qdrant-source-reader`.
- Complete source/Qdrant payload and ID verification: **38,161**, no differences
  and no changes required in the initial reconciliation.
- New and edited chunks copy Marqo's exact exported dense vectors. BM25 sparse
  values are generated from text plus description. Metadata changes preserve vectors.
- Visibility and removal propagate from the authoritative source. The current
  pipeline disables documents by purging source chunks; re-enabling requires
  reingestion. The mirror hides removed chunks on the first verified scan and
  deletes them only after confirmation. It does not bypass the pipeline's policy.
- Full scans took approximately 12 seconds, with 30-second polling after each
  completion: typical update/visibility propagation is about **45 seconds**;
  confirmed hard deletion is typically within about **90 seconds**.
- The authenticated GPU query service rejects unhealthy/incomplete/stale sync
  with HTTP 503. The freshness limit is 120 seconds; bad credentials still return 401.
- Source schema/model changes, namespace mismatches, incomplete scans, source
  counts below 30,000 or a deletion exceeding 10% stop reconciliation for review.
  These are operational guards, not claims of unlimited automatic bulk deletion.

Implementation and operational limits: [ops/README.md](../ops/README.md).

## Source disk write-block and recovery

The live lifecycle test uncovered a pre-existing source failure. Vespa's actual
feed response was HTTP 507: **75.2% disk usage exceeded its 75.0% limit**. Marqo's
API reported this as vector-store memory/disk exhaustion, while reads still worked.

Moved only the owned DEV Qdrant store from the Docker volume on `/var` to
`/home/aicloud/qdrant-dev-cutover-20261026/qdrant-storage`. During the move, DEV
used the original Marqo BPP. Before removing the redundant old stopped container
and volume, verified a full archive, every copied file, all collection counts,
and sampled vectors/payloads after restart. The full backup remains on `/home`.
No existing Marqo index, authentication container, or feed threshold was changed.

This restored source writes, confirmed by a real isolated Marqo → Qdrant lifecycle
canary. Further durable `/var` capacity headroom is tracked in
[amul-infra #184](https://github.com/OpenAgriNet/amul-infra/issues/184).

## Validation

| Check | Result |
| --- | --- |
| Voice routing/auth and existing registration/search tests | 28 passed |
| Qdrant adapter/local contracts plus new mirror/freshness tests | 56 passed |
| Actual chat EN/GU | HTTP 200, substantive responses, `search_documents` and source-bearing results persisted |
| Actual voice EN/GU | HTTP 200, substantive responses, registered search tool and source-bearing results persisted |
| Public HTTPS chat/voice EN/GU through VM3 → VM2 | All four HTTP 200; real retrieval verified in every saved session |
| Voice's own anonymous-token endpoint | Delegated tokens accepted by the actual voice endpoint |
| Application rollback to Marqo and Qdrant reactivation | Eight EN/GU chat/voice sessions passed; retrieval verified in every history |
| Final scoped seeker EN/GU checks | 12 chunks each, actual Beckn callbacks delivered |
| Live isolated ingestion/lifecycle canary | New chunks, hide/show, vector preservation, edits, copied-vector equality, chunk numbers, scoped deletion and unrelated workflow retention passed |
| Canary cleanup | Its source and target indexes removed |
| Actual stale-source negative test | Both authenticated health and search returned 503; unauthenticated health remained 401 |
| Storage move | Full archive/member and copied-file verification; live counts and sampled vectors/payloads matched |

The voice endpoint here is the backend's actual post-STT voice HTTP transport.
No outbound telephone call, microphone/STT capture or end-user audio playback was
performed. Tests establish retrieval and backend turns, not full telephony quality
or that Qdrant is more relevant than Marqo.

The existing BAP callback receiver's `validateSign` exception for Bharat Vistaar
([amul-oan-api #350](https://github.com/OpenAgriNet/amul-oan-api/issues/350)) remains.
Real callback delivery does not prove inbound callback signature enforcement.
Production promotion, that security boundary, native-writer migration and upstream
BPP publication remain separate gates. The BPP repository is still inaccessible;
its deployed candidate source, patch and image remain preserved on H100.

## Durable handoff and recovery

Primary VM2 artifacts: `/home/azureuser/qdrant-dev-cutover-20261026`.

- `PUBLIC-CUTOVER-RESULT.json`: authoritative public routing, preserved runtime,
  public requests and retrieval histories. Also copied to H100.
- `public-final/`: actual public HTTPS synthetic responses and summary.
- Original app/source/Compose snapshots and private environment/image-preserving
  `app-apply.sh`, `app-rollback.sh`, `app.override.json` and `deployment.env`.
- Its original local JWT issuer remains intact; no signing key was moved or copied.

Secondary VM5 artifacts: `/home/azureuser/qdrant-dev-cutover-20261026`.

- `CUTOVER-RESULT.json`, `final-history-verification.json`, `voice-tests.log`.
- `final/` and `marqo-rollback/`: synthetic app responses and summaries.
- Original app inspections, source/auth/config backups, private resolved runtime
  overlays, and `chat-apply.sh`, `voice-apply.sh` plus original-state rollback scripts.
- `callback-due-before-cleanup.json` and `callback-due-cleanup.json`.

Backend rollback keeps the repaired applications on Beckn but restores their
provider to the original Marqo image/index:

```bash
# Run on DEV.
/home/azureuser/qdrant-dev-cutover-20261025/rollback.sh
# Re-enable Qdrant after checking sync/query health.
/home/azureuser/qdrant-dev-cutover-20261025/activate.sh
```

Original-state voice rollback also needs the archived original Compose/source
files if the managed source has since changed. It deliberately restores the old
application state, including its original limitations. Prefer the backend-only
rollback above for retrieval incidents.

H100 artifacts: `/home/aicloud/qdrant-dev-cutover-20261026`.

- `compose.json`: mirror and loopback-only source bridge; `config.json`: pinned
  source/target/schema, guards and marker. `state/status.json` records every cycle.
- `store-compose.json`: relocated Qdrant store, same HTTP loopback endpoint.
- `canary-result.json`, `canary.log`, `targeted-tests.log`, `stale-guard-test.json`.
- `storage-copy-verification.json`, `storage-relocation-complete.json`, original
  store inspection and before/after collection evidence.
- `qdrant-volume-before-home-20261026.tar.gz`: verified full stopped-volume backup.
- Versioned `ops/` source plus old query script/Compose backups. The active query
  deployment remains in `/home/aicloud/qdrant-dev-deploy-20261024/compose.json`.

Containers restart independently of the laptop. Use the recorded Compose files
for maintenance; base-only deployments can discard runtime preservation overlays.
Credentials, inspections and resolved runtime files are private and must not be
printed or committed. No production promotion is scheduled.
