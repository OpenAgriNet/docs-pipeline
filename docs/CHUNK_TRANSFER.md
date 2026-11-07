# Chunk export and import (#63)

## Focused slices

| Slice | Issue | Scope / dependency |
|---|---|---|
| 1 | [#183](https://github.com/OpenAgriNet/docs-pipeline/issues/183) | Portable format and complete, tenant-scoped export API |
| 2 | [#184](https://github.com/OpenAgriNet/docs-pipeline/issues/184) | Authenticated document / filtered-result downloads; depends on 1 |
| 3 | [#185](https://github.com/OpenAgriNet/docs-pipeline/issues/185) | Whole-file validation, create-only SQL records, tags and optional PDF; depends on 1 |
| 4 | [#186](https://github.com/OpenAgriNet/docs-pipeline/issues/186) | DEV-only import API, ingestion/job orchestration and verification; depends on 3 |
| 5 | [#187](https://github.com/OpenAgriNet/docs-pipeline/issues/187) | Upload form, progress and optional PDF navigation; depends on 4 |

Imports are planned to finish as `completed` in an explicitly configured DEV
destination. Production approval is separate work; main does not currently have
an `approval_for_prod` stage. This feature does not choose or deploy a backend.

## Export API (slice 1)

- `GET /documents/{workflow_id}/chunks/export?format=json|csv`
- `GET /chunks/export?format=json|csv&q=...&tags=...&stage=...`
- Both support `include_excluded=true` (default false).
- Both require the same search permission and tenant scope as chunk reads.
- The cross-document endpoint applies the same keyword, inline-tag, repeated-tag,
  stage and exclusion predicates as `/chunks/search`. It exports **all** matches;
  there is no limit or offset. Rows and their tags come from one SQLite snapshot.
- Results stream as attachments named `chunks.json` or `chunks.csv`. These are
  saved SQL chunks, not pending browser edits or backend embedding payloads.
- The older `/documents/{workflow_id}/export/chunks` remains compatible.

## Portable format, version 2

JSON is the canonical, exact transfer format: an array of objects with unmodified
Unicode text, JSON types and null/empty-string distinctions. `text` is the
authoritative editable content; `domain_tags` is the authoritative editable tag
list. `original_text`, `edited_text` and `domain_tag_metadata` preserve history.
They must not overwrite edits to those authoritative fields in a future import.
Existing `*_json` provenance fields remain their original JSON strings.

| Fields | Meaning |
|---|---|
| `schema_version` | `2` |
| `chunk_number`, `text` | Original numbering (including gaps), effective saved edited/original text |
| `original_text`, `edited_text`, `token_count` | Saved text history and stored count |
| `section_title`, `content_type`, `is_reference`, `is_excluded` | Content metadata and exclusion flag |
| `page_start`, `page_end`, `source_page_numbers_json`, `source_spans_json` | Source location |
| `domain_tags` | Sorted `dimension:value` strings |
| `domain_tag_metadata` | Tag objects including dimension, value, source and timestamps |
| `is_reviewed`, `reviewer_notes`, `chunk_version` | Review/version provenance |
| `chunking_provider`, `chunking_model`, `chunking_config_json`, `chunking_run_id` | Chunk-generation provenance |
| `source_workflow_id`, `source_document_id`, `source_filename`, `source_instance` | Source ownership/provenance, never authority for a future import |
| `source_display_name`, `source_stage` | Saved display name and processing stage, including work still in progress |
| `source_query_enabled`, `source_is_disabled` | Document search/deletion context; distinct from chunk exclusion |
| `exported_at` | One UTC ISO timestamp for the entire download |

Document context, chunk rows and tags come from the same database snapshot.
`exported_at` identifies the download time, not the time the document was indexed.
This exports SQL's saved chunks, not a Qdrant backup or proof of searchability.

### CSV encoding

CSV has the same fields plus `csv_encoding=backslash-v1`, UTF-8 with a BOM and all
cells quoted by the CSV writer. Booleans, numbers and nested values use JSON
encoding; existing `*_json` string fields remain strings. Formula-like textual
cells are prefixed with a **visible, literal backslash** instead of relying on
spreadsheet-specific quote/tab escapes. The database and JSON text never change.

The prefix applies to strings beginning with `=`, `+`, `-`, `@`, their full-width
variants, a quote, whitespace or a control/format character. Literal leading
backslashes are also escaped, making the encoding unambiguous. A lone backslash
encodes an empty string; a blank CSV cell encodes null.

| Saved string | CSV cell value (after CSV parsing) | Decoded value |
|---|---|---|
| `=1+1` | `\=1+1` | `=1+1` |
| `\literal` | `\\literal` | `\literal` |
| empty string | `\` | empty string |
| null | blank | null |

`pipeline.services.chunk_transfer.decode_csv_record` reverses this encoding and
checks its marker/version. It is a file-format helper, not an upload endpoint or
import validator. CSV import code must decode marked rows before validation;
third-party unmarked CSV needs its own explicit parsing path. Do not remove
backslashes indiscriminately from ordinary files.

[OWASP documents spreadsheet-dependent CSV interpretation and save/reopen
limitations](https://owasp.org/www-community/attacks/CSV_Injection). A spreadsheet
may rewrite cell types or content when resaving; exact equivalence through every
spreadsheet editor is not promised. Prefer JSON for exact transfers. Tests cover
our generated CSV and decoder, including formula-leading text and empty/null
values; desktop Excel/LibreOffice save/reopen behaviour is not yet verified.

First import scope is **one new document per file**. A mixed-document export is
useful for QA, but must be split by source document before import because chunk
numbers repeat across documents. Imports will take effective `text`, create fresh
internal identities and will not inherit approval/tenant permissions from the
file. Source document status, reviewer and generation fields are provenance, not
automatic approval or instructions to enable a new document for search.

## Internal prepared import (slice 3)

`pipeline.services.chunk_import.create_prepared_import` accepts one new logical
`document_id`, a JSON/CSV filename and bytes, the acting user/tenant, and an
optional PDF with its filename. It requires upload permission in the target
tenant. There is no HTTP import route yet.

Minimal JSON (gaps in numbering are kept):

```json
[
  {"chunk_number": 3, "text": "First prepared chunk", "domain_tags": ["species:cattle"]},
  {"chunk_number": 8, "text": "Second prepared chunk", "is_excluded": true}
]
```

Plain UTF-8 CSV requires `chunk_number,text`; optional columns use the export
field names. Integers, booleans and tag/metadata lists use JSON cell values
(`true`/`false`, or `0`/`1` for CSV flags). Plain CSV backslashes are literal.
Marked v2 CSV uses the exact export header and `backslash-v1` decoder above.
JSON is an array, not a document wrapper; unknown fields, unsupported versions,
duplicate numbers and conflicting source identities are rejected.

The importer validates the complete file and PDF before any persistent writes.
Page bounds are optional but must be paired/ordered when supplied; all supplied
page references must fit the PDF when attached. Source spans use `page_number`
(legacy `page` also accepted), with optional ordered `start_char`/`end_char`.
Offsets are structurally validated; they are not checked against OCR text.
Missing token counts use the existing `cl100k_base` counter. Explicit reference
and exclusion flags, section/content fields, spans and tags are retained. Strict
taxonomy mode rejects unknown tags rather than silently losing them; permissive
mode keeps normalized custom tags. No automatic tagging runs.

Server-owned `ImportLimits` defaults: 25 MiB transfer, 10,000 chunks, 256 KiB UTF-8
text per chunk, 64 tags per chunk, and a 100 MiB/10,000-page PDF. Empty, malformed
or encrypted PDFs are rejected. The future route must enforce byte limits while
reading uploads too; this internal service receives bytes already in memory.

The exact input file is saved as a tenant-scoped `original_upload` artifact for
source IDs, text history and other provenance. An attached PDF is stored as
`normalized_pdf` for the existing preview path, without conversion. Workflow and
object identities are generated server-side. The displayed document filename is
the PDF's name when attached, otherwise the transfer filename. Active text comes
from `text`, active tags from `domain_tags`; imported review/history, generation IDs and source search
flags do not grant approval. New chunks are unreviewed, and the new document is
`ready_for_ingestion` with `query_enabled=false`, with no job/index-status row.

Known identity conflicts are rejected before upload; a second check inside
`BEGIN IMMEDIATE` also protects concurrent imports across processes. Existing
tenant document/canonical IDs (even disabled ones) cannot be replaced; live exact
input-file duplicates are rejected using the existing fingerprint mechanism.
Different tenants may import the same source. The transaction saves the document,
chunks, tags, fingerprint claim and artifact records together.

Uploads use unique new object paths before the SQL transaction, so network I/O
does not hold a SQLite writer lock. On upload/SQL failure, only this import's
attempted keys are deleted, including a possibly completed upload that timed out.
`ImportCleanupError.orphaned_uris` reports cleanup failures. A process crash
between upload and SQL commit can still leave an unreferenced object; this is
compensation, not a distributed transaction or a crash-recovery system.

## Remaining endpoint/workflow requirements (slices 4–5)

Reuse the ingestion-only workflow, without OCR, translation, chunking or retagging.
Resolve the allowed DEV destination server-side, fail closed when not configured,
preserve explicit reference/tag metadata and verify expected non-excluded records.
Test workflow-start and partial-write failures, retry and cleanup. Whole-file
validation does not imply a distributed transaction or atomic search visibility:
that publication requirement must be handled explicitly before exposing imports.

No existing collection, production routing or live deployment changes are part of
these slices. Validate indexing in an isolated collection.
