# DEV Qdrant BPP trial — 2026-10-25

The user resumed the deferred DEV switch after the server returned. DEV's
veterinary BPP now retrieves from `amul-veterinary-final-20261023` on H100:
**38,161 records**, authenticated private GPU search. This is a working BPP
retrieval trial. Full DEV chat/voice and live ingestion cutover is incomplete.
Production was not changed.

## Verification and changes

- Rechecked the DEV hostname, original image, effective application settings,
  managed Compose files, and routes. The original Marqo network search passed.
- Compared all 38,161 source records with the exported snapshot, excluding
  `_tensor_facets` and transport `_found`: no changed or missing payloads; source
  count 38,161. This is a current payload comparison, not a fresh vector export.
- Verified DEV can reach H100's authenticated endpoint and that the complete
  managed Compose resolves. Preserved its server-local interpolation inputs,
  application environment, mounts, networks, signing identity and Redis settings.
- Recreated only `amul-net-bpp-vet` using the tested candidate, with its original
  full Compose configuration plus private preservation and Qdrant overlays.
- Tested English and Gujarati through seeker → ONIX BAP caller → BPP receiver →
  veterinary BPP → Qdrant → ONIX callback → seeker: 12 catalog items each.
- Rolled back to the exact original Marqo image and repeated both languages,
  then reactivated Qdrant and repeated both. All six callbacks were `DELIVERED`
  in Redis, contained 12 items, and required zero retries. Initial delivered jobs
  remained available after container recreation. This does not exercise a pending
  live callback during a crash; that case passed the earlier isolated image test.
- Final Qdrant searches took approximately 391 ms EN / 197 ms GU in this small
  smoke check. These are successful requests, not a benchmark or relevance claim.
- Rechecked final image, backend, health and preservation of original settings,
  mounts, and network membership. The BPP is healthy and its source is reachable.

### Docker image import identity

The first activation attempt stopped before changing the running service:
H100's Docker 27 image configuration ID was not resolvable as an image ID by
DEV's Docker 29 containerd image store. After import, DEV reports a different
manifest/image digest. Verified the archive's configuration SHA-256 against the
tested H100 ID, exact image configuration, every root-filesystem layer digest,
and Linux/amd64 platform before pinning the DEV digest and retrying.

- Tested H100 config: `sha256:d52418aeb8513bddbb69c6243a8940bb4f496de03b32ec392b3d76c05beb4ba3`.
- Verified imported DEV image: `sha256:94c9a8266e0b89b5253aadf9973e351efa002d25874607a106dc0ed5547e6192`.
- Original rollback image: `sha256:d2efd2ad720d2735cb107ff3ee4d59ff349a5b835c4f03020006789703fe6467`.

## Application and migration boundaries

These conditions were identified before activating Qdrant and remain unresolved:

| Boundary | Observed behavior | Remaining work |
| --- | --- | --- |
| DEV chat history | `/api/chat/` returns HTTP 500; Redis resolves to `localhost:6379` inside `amul_app`. | Restore the intended managed Redis configuration. |
| Chat → seeker | `amul_app` belongs only to `amul-network`; seeker belongs to `amulnet` and its private caller network. Default seeker hostname fails DNS resolution. | Persist the correct network membership or supported seeker URL in the managed deployment. |
| Deployed chat search function | EN/GU both produce 12 source-bearing snippets with only a per-process `AMUL_NETWORK_URL` override to the host seeker endpoint. | Diagnostic success does not fix the live app or prove full chat turns. |
| Actual voice tool | `agents.voice.agent` registers `agents.voice.tools.search.search_documents`, which still calls Marqo directly. | Route the registered voice tool through the supported retrieval adapter and test real audio sessions. |
| Voice HTTP test authentication | Anonymous-token issuance returns 500 because `/app/jwt_private_key.pem` is missing. | Use the intended supported authentication flow or repair the managed test setup. No real audio session was completed. |
| Pipeline writes | Live H100 API/worker still use `VECTOR_STORE_BACKEND=marqo`. | Deploy and verify ingestion/lifecycle mapping or synchronization for the final Qdrant collection. Uploads, edits, visibility changes and deletes do not automatically update this snapshot. |

The existing ONIX BPP receiver enforces `validateSign`; an unsigned search probe
returned 401. The existing BAP callback receiver omits that step for the known
Bharat Vistaar exception documented in
[amul-oan-api #350](https://github.com/OpenAgriNet/amul-oan-api/issues/350).
The private callers retain their signing steps, and live callbacks were ACKed,
but this trial does not establish inbound callback signature enforcement.
No ONIX authentication configuration was changed.

Redis audit found 32 entries in the due index: 31 had missing/expired job records,
and the one retained job was created on 2026-10-24, before this trial. No newly
pending trial job was found. These existing entries were not removed or replayed.

Remaining acceptance criteria are filed in
[issue #178](https://github.com/OpenAgriNet/docs-pipeline/issues/178).
The local `bd` executable was unavailable, so GitHub tracks this follow-up.

## Durable artifacts and recovery

DEV: `/home/azureuser/qdrant-dev-cutover-20261025` (private directory).

- `TRIAL-RESULT.json`: final health, preservation, checks and incomplete boundaries.
- `image-import-verification.json`, `rollback-verified.json`,
  `live-callback-verification.json`, `pending-queue-audit.json`.
- `seeker-{qdrant,rollback,final}-summary.json` and transaction response files.
- `baseline-summary.json` / `final-summary.json`: app HTTP failures before/after.
- `baseline-tool-summary.json`: actual default chat tool fails before switch;
  `baseline-override-tool-summary.json` / `qdrant-override-tool-summary.json`:
  bounded tool diagnostics against working seeker URL.
- Private original runtime snapshot, resolved Compose configuration and mode-600
  `deploy.env`. These contain runtime secrets and must not be committed or printed.

Run on DEV to restore the original Marqo BPP:

```bash
/home/azureuser/qdrant-dev-cutover-20261025/rollback.sh
```

Reactivation after checking current state uses the preserved candidate overlay:

```bash
/home/azureuser/qdrant-dev-cutover-20261025/activate.sh
```

Do not rerun `switch-dev.sh --apply` against an already switched service: it
captures a new baseline and could replace the original rollback material.
The managed deployment must retain these overlays until integrated into the
normal source/deployment configuration; the base Compose alone still selects
the original service. The BPP patch/source and H100 query service remain in
`/home/aicloud/qdrant-dev-deploy-20261024`; upstream BPP publication remains
pending repository access.

H100 now also stores `DEV-TRIAL-20261025.json`, `freshness-20261025.json`, and
updated `READY.json`. The prior deferred readiness is retained as
`READY.before-dev-switch-20261025.json`. Server containers run independently of
the laptop. No automatic production promotion is configured.
