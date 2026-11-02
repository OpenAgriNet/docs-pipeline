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

## Portable format, version 1

JSON is an array of objects. CSV has the same columns, UTF-8 with a BOM, standard
quoting for commas/newlines, lowercase JSON booleans and JSON-encoded nested
values. Empty CSV optional cells represent missing values; JSON preserves the
difference between an empty string and null. Existing `*_json` provenance fields
remain their original JSON strings in both formats.

| Fields | Meaning |
|---|---|
| `schema_version` | `1` |
| `chunk_number`, `text` | Original numbering (including gaps), effective saved edited/original text |
| `original_text`, `edited_text`, `token_count` | Saved text history and stored count |
| `section_title`, `content_type`, `is_reference`, `is_excluded` | Content metadata and exclusion flag |
| `page_start`, `page_end`, `source_page_numbers_json`, `source_spans_json` | Source location |
| `domain_tags` | Sorted `dimension:value` strings |
| `domain_tag_metadata` | Tag objects including dimension, value, source and timestamps |
| `is_reviewed`, `reviewer_notes`, `chunk_version` | Review/version provenance |
| `chunking_provider`, `chunking_model`, `chunking_config_json`, `chunking_run_id` | Chunk-generation provenance |
| `source_workflow_id`, `source_document_id`, `source_filename`, `source_instance` | Source ownership/provenance, never authority for a future import |

Text is preserved exactly, including formula-like text. CSV is a machine data
format: use spreadsheet import with text columns rather than executing cell
formulas; use JSON when preservation of nulls matters.

First import scope is **one new document per file**. A mixed-document export is
useful for QA, but must be split by source document before import because chunk
numbers repeat across documents. Imports will take effective `text`, create fresh
internal identities and will not inherit approval/tenant permissions from the
file. Reviewer and generation fields are provenance, not automatic approval.

## Import acceptance requirements (slices 3–5)

Validate the entire file before any persistent writes: unique positive chunk
numbers, non-empty text, typed metadata, configured size limits and valid PDF page
spans when attached. Compute missing token counts; use the tenant taxonomy policy.
Create a new document with collision protection and save chunks/tags atomically;
coordinate optional PDF storage with compensation. Existing documents must never
be replaced by file import.

Reuse the ingestion-only workflow, without OCR, translation, chunking or retagging.
Resolve the allowed DEV destination server-side, fail closed when not configured,
preserve explicit reference/tag metadata and verify expected non-excluded records.
Test workflow-start and partial-write failures, retry and cleanup. Whole-file
validation does not imply a distributed transaction or atomic search visibility:
that publication requirement must be handled explicitly before exposing imports.

No existing collection, production routing or live deployment changes are part of
these slices. Validate indexing in an isolated collection.
