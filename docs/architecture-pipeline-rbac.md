# Architecture — Login, Role-Based Access, Dev Ingest & Prod Approval

This document describes **how Bharat Vistaar Docs Pipeline works end-to-end**: from SSO login and state/role resolution, through document processing and **DEV** ingestion, to **PROD** promotion that only **Bharat Vistaar Super Admin** can approve.

Grounded in current code under `pipeline/`, `ui/`, and Keycloak group conventions.

**Also see:** [SYSTEM_DESIGN.md](./SYSTEM_DESIGN.md) for full system design, plus the box-and-arrow diagrams:

- [system-design-diagram.png](./system-design-diagram.png) · [HTML](./system-design-diagram.html)
- [architecture-diagram.png](./architecture-diagram.png) · [HTML](./architecture-diagram.html)

---

## 1. Goals (governance)

| Actor | Scope | What they may do | What they may **not** do |
|---|---|---|---|
| **State Contributor** | One or more states (`mh`, `up`, …) | Upload, process, review, **ingest to DEV** for their state | Promote to PROD; see other states |
| **State Reviewer** | Assigned state(s) | Review / approve stages within state | Upload, delete, promote to PROD |
| **Super Admin (Bharat Vistaar)** | All states + portal `bv` | Everything above **plus** user management, settings, **review & approve PROD** | — |

**Hard rule:** State operators may drive a document only as far as **Ingested in DEV** (`approval_for_prod` queue).  
**PROD vectors are written only after Super Admin approval.**

---

## 2. High-level system architecture

```mermaid
flowchart TB
  subgraph Clients
    Browser["Operator browser<br/>React SPA (ui/)"]
  end

  subgraph Identity
    KC["Keycloak<br/>SSO + Groups + Realm roles"]
  end

  subgraph ControlPlane
    API["FastAPI<br/>pipeline/api.py"]
    Worker["Temporal Worker<br/>pipeline/worker.py"]
    Temporal["Temporal Server<br/>workflows + signals"]
  end

  subgraph DataPlane
    SQLite["SQLite<br/>documents, pages, chunks,<br/>jobs, audit"]
    MinIO["MinIO / S3<br/>source PDFs"]
    QdrantDev["Qdrant DEV<br/>search index"]
    QdrantProd["Qdrant PROD<br/>search index"]
  end

  subgraph Inference
    OCR["OCR providers"]
    TR["Translation"]
    CH["Chunking / tagging"]
    EMB["Embeddings"]
  end

  Browser -->|"OIDC PKCE login"| KC
  Browser -->|"Bearer JWT + /api/*"| API
  API -->|"validate JWT / JWKS"| KC
  API --> SQLite
  API --> MinIO
  API -->|"start / signal workflows"| Temporal
  Temporal --> Worker
  Worker --> SQLite
  Worker --> MinIO
  Worker --> OCR
  Worker --> TR
  Worker --> CH
  Worker --> EMB
  Worker -->|"ingest_document_from_db"| QdrantDev
  Worker -->|"promote_document_to_prod_qdrant"| QdrantProd
  API -->|"tenant-scoped search"| QdrantDev
```

### Responsibilities

| Layer | Responsibility |
|---|---|
| **Keycloak** | Source of truth for *who* is Super Admin vs which **state + role** a user has |
| **JWT → API** | Map groups/roles → `permissions`, `instances`, `state_roles`; enforce tenancy + capability |
| **Temporal workflow** | Durable pipeline with human gates (signals) |
| **SQLite** | System of record for document stage, pages, chunks, uploader, `instance` |
| **Qdrant DEV** | Searchable vectors after state-approved ingestion |
| **Qdrant PROD** | Production vectors only after Super Admin `approve_prod` |

---

## 3. Login & role resolution (start of every session)

```mermaid
sequenceDiagram
  participant U as User
  participant SPA as "React UI"
  participant KC as Keycloak
  participant API as "API /auth/me"

  U->>SPA: Open app
  SPA->>KC: OIDC login PKCE
  KC-->>SPA: Access token JWT
  Note over KC,SPA: claims groups, realm roles, email, sub

  SPA->>API: GET /auth/me Bearer JWT
  API->>API: Validate JWT via JWKS
  API->>API: parse groups to instances and state_roles
  API->>API: roles to permissions union
  API-->>SPA: user profile

  Note over SPA: UI enables menus from permissions and instances
```

### Keycloak group model

```text
/global
  /super-admin                 → product role: super_admin (Bharat Vistaar)
/states
  /MH
    /contributor               → state mh + contributor
    /reviewer                  → state mh + reviewer
  /UP
    /contributor
    /reviewer
  …
```

### Token → app profile (example)

| Claim | App field | Meaning |
|---|---|---|
| `/global/super-admin` | `is_super_admin: true`, all instances | Platform operator |
| `/states/MH/contributor` | `instances: [mh]`, `state_roles.mh: contributor` | MH uploader/ops |
| `realm_access.roles` | `roles[]` → `permissions[]` | Capability bits |

### Product permissions

| Permission | Super Admin | State Contributor | State Reviewer |
|---|:---:|:---:|:---:|
| `search` | ✓ | ✓ | ✓ |
| `upload` | ✓ | ✓ | — |
| `review` | ✓ | ✓ (own/state policy) | ✓ |
| `pipeline` | ✓ | ✓ | — |
| `delete_own` | ✓ | ✓ | — |
| `admin` (**prod approve**, settings, hard delete) | ✓ | — | — |
| `manage_users` | ✓ | — | — |

Implementation: `pipeline/auth/permissions.py`, `pipeline/auth/groups.py`, `pipeline/auth/jwt.py`, `ui/src/auth/AuthProvider.jsx`.

---

## 4. Tenant isolation (state-level data boundary)

Every document is stamped with **`documents.instance`** at upload time (e.g. `mh`, `up`, or portal `bv`).

```mermaid
flowchart LR
  subgraph Token
    G["groups / instances"]
  end
  subgraph API
    Scope["_instance_scope_for_user()"]
  end
  subgraph Lists
    Docs["GET /documents"]
    Runs["GET /runs"]
    Search["Marqo/Qdrant filters"]
  end

  G --> Scope
  Scope -->|"None = all (super admin)"| Docs
  Scope -->|"['mh'] only"| Docs
  Scope --> Runs
  Scope --> Search
```

- **Super Admin:** unrestricted instance list (`None` scope).
- **State user:** only documents / runs / chunks / search hits for allowed states.
- Cross-tenant access returns **404** (not 403) to avoid leaking IDs.

---

## 5. End-to-end document pipeline (stages)

Human-in-the-loop stages use **Temporal signals**. Processing stages run activities on the worker.

```mermaid
stateDiagram-v2
  [*] --> registered: Upload (state contributor / super admin)
  registered --> ocr_processing: Temporal starts OCR
  ocr_processing --> ocr_review: OCR done
  ocr_review --> translation_processing: signal approve_ocr
  translation_processing --> translation_review: translation done
  translation_review --> chunking: signal approve_translation
  chunking --> chunk_review: chunks + optional domain tags
  chunk_review --> ready_for_ingestion: signal approve_chunks
  ready_for_ingestion --> ingesting: signal approve_ingestion
  ingesting --> approval_for_prod: vectors written to DEV
  approval_for_prod --> completed: signal approve_prod\n(Super Admin only)
  note right of approval_for_prod
    State users stop here.
    Document is searchable in DEV.
    Waiting for Bharat Vistaar Super Admin.
  end note
  completed --> [*]
  ocr_processing --> failed: error
  translation_processing --> failed: error
  chunking --> failed: error
  ingesting --> failed: error
```

### Stage ownership matrix

| Stage | Who advances it | Effect |
|---|---|---|
| Upload → Registered | Contributor / Super Admin (`upload`) | File in MinIO; SQLite row; `instance` set |
| OCR / Translation / Chunking | System (worker activities) | Pages/chunks in SQLite |
| OCR / Translation / Chunk / **Pre-ingestion** approve | State **review** (+ pipeline as needed) or Super Admin | Temporal signal continues workflow |
| **Ingesting in Dev** | System after `approve_ingestion` | `ingest_document_from_db` → **Qdrant DEV** |
| **Approval for Prod** (queue) | Document waits | Still **DEV-only** in search until promote |
| **Approve for Prod** | **Super Admin only** (`admin` / `RequireAdmin`) | `promote_document_to_prod_qdrant` → **Qdrant PROD** → `completed` |

Signals (workflow): `approve_ocr`, `approve_translation`, `approve_chunks`, `approve_ingestion`, `approve_prod` — see `pipeline/workflows.py`.

API (examples):

- `POST /documents/{id}/approve-ocr` … `approve-ingestion` → state-capable roles with `review`
- `POST /documents/{id}/approve-prod` → **`RequireAdmin`** (Super Admin)

UI gates: `DocumentOpsView` maps `approve_prod` → permission `admin`; other approvals → `review`.

---

## 6. Dev vs Prod data path (critical split)

```mermaid
flowchart TB
  subgraph StateLane["STATE LANE — allowed for state operators"]
    U[Upload PDF<br/>instance = mh / up / …]
    P[Process + human reviews]
    PRE[Pre-ingestion approve]
    DEVINGEST[Ingest to DEV Qdrant]
    QUEUE[Stage: approval_for_prod<br/>Request / wait for BV approval]
  end

  subgraph SuperAdminLane["BHARAT VISTAAR SUPER ADMIN ONLY"]
    REVIEW[Review document in console<br/>pages, chunks, DEV search quality]
    APPROVE[POST approve-prod]
    PROD[Promote vectors DEV → PROD Qdrant]
    DONE[Stage: completed]
  end

  U --> P --> PRE --> DEVINGEST --> QUEUE
  QUEUE -->|"state user has no admin"| QUEUE
  QUEUE -->|"Super Admin clicks Approve for Prod"| REVIEW
  REVIEW --> APPROVE --> PROD --> DONE
```

### What “request for approval” means operationally

| Step | State user | Super Admin |
|---|---|---|
| Finish DEV ingest | Workflow auto-moves stage to `approval_for_prod` | Same |
| Express readiness for prod | Document sits in **Approval for Prod** queue / Runs / Dashboard (state-scoped list) | Sees **all states** in queue |
| Promote | **No** `Approve for Prod` button (`admin` missing) | Reviews content → **Approve for Prod** |
| Result | Still only DEV-indexed until SA acts | PROD index updated; stage `completed` |

> **Implementation note:** There is no separate “request approval” API today. Completing DEV ingest **is** the request: the document enters the Super Admin queue at `approval_for_prod`. Prod promotion never runs on `auto_approve`; the workflow always waits for `prod_approved` (`workflows.py`).

---

## 7. Swimlane: full journey (login → prod)

```mermaid
sequenceDiagram
  autonumber
  actor SC as "State Contributor MH"
  actor SA as "Super Admin BV"
  participant UI as "SPA"
  participant API as "API"
  participant T as "Temporal Worker"
  participant DEV as "Qdrant DEV"
  participant PROD as "Qdrant PROD"

  SC->>UI: Login via Keycloak
  UI->>API: GET /auth/me - instances mh, role contributor
  SC->>UI: Upload PDF for state mh
  UI->>API: POST upload
  API->>T: Start DocumentPipelineWorkflow
  T->>T: OCR then wait for approve_ocr
  SC->>API: Approve OCR, translation, chunks
  SC->>API: Approve ingestion DEV
  T->>DEV: ingest_document_from_db
  T->>API: Set stage approval_for_prod
  Note over SC,UI: State user can search DEV only - no Approve for Prod

  SA->>UI: Login as super_admin
  UI->>API: GET /auth/me - unrestricted + admin
  SA->>UI: Open Approval for Prod queue
  SA->>API: Review document detail
  SA->>API: POST /documents/id/approve-prod
  API->>T: Signal approve_prod or PromoteToProdWorkflow
  T->>PROD: promote_document_to_prod_qdrant
  T->>API: Set stage completed
```

---

## 8. Component architecture (runtime)

```mermaid
flowchart LR
  subgraph UI
    Login[LoginView / Keycloak JS]
    Docs[Documents / New Document]
    Ops[DocumentOpsView]
    Runs[Runs]
    Search[Search Workbench]
    Users[Users Admin - SA only]
  end

  subgraph API_Auth
    JWT[jwt claims_to_user]
    Tenancy[tenancy.allowed_instances]
    Perms[permissions_for_roles]
    Deps[RequireUpload / Review / Pipeline / Admin]
  end

  subgraph API_Domain
    UploadAPI[upload endpoints]
    ApproveAPI[approve-* endpoints]
    ListAPI[list docs / runs]
    SearchAPI[search]
  end

  Login --> JWT
  Docs --> UploadAPI
  Ops --> ApproveAPI
  Runs --> ListAPI
  Search --> SearchAPI
  Users --> Deps

  UploadAPI --> Deps
  ApproveAPI --> Deps
  ListAPI --> Tenancy
  SearchAPI --> Tenancy
  JWT --> Perms
  JWT --> Tenancy
```

---

## 9. Storage & promotion detail

```mermaid
flowchart TB
  PDF[Source PDF in MinIO]
  SQLITE[(SQLite: pages + chunks<br/>+ instance + uploader)]
  DEV[(Qdrant DEV collection)]
  PROD[(Qdrant PROD collection)]

  PDF --> SQLITE
  SQLITE -->|"ingest_document_from_db<br/>after approve_ingestion"| DEV
  DEV -->|"promote_document_to_prod_qdrant<br/>after Super Admin approve_prod"| PROD
  SQLITE -.->|"stage mirror"| SQLITE
```

- **DEV ingest** activity: `ingest_document_from_db` (worker).
- **PROD promote** activity: `promote_document_to_prod_qdrant` (requires `PROD_QDRANT_URL`).
- Chunk payloads include **`instance`** for tenant-filtered search.

---

## 10. UI capability map (what each role sees)

| Console area | State Contributor | State Reviewer | Super Admin |
|---|---|---|---|
| Login / profile | Own states + role | Own states + role | All states · Super Admin |
| Upload new document | Yes (own state) | No | Yes (any / portal) |
| Documents / Runs lists | Own state only | Own state only | All states |
| OCR → Chunk reviews | Yes | Yes | Yes |
| Approve ingestion (**DEV**) | Yes | Yes (if review) | Yes |
| **Approve for Prod** | Hidden / denied | Hidden / denied | **Yes** |
| Users admin | No | No | Yes |
| Platform settings | No | No | Yes |

---

## 11. Security summary

1. **Authentication:** Keycloak OIDC; API validates JWT (JWKS).
2. **Authorization (capability):** permission bits from product roles.
3. **Authorization (tenant):** `instance` on every document; list/get/search scoped.
4. **Authorization (prod):** `approve_prod` bound to `Permission.ADMIN` → Super Admin only.
5. **Audit:** approval and promote actions logged with actor identity/roles.

---

## 12. Reference file map

| Concern | Location |
|---|---|
| Stages enum | `pipeline/models.py` → `DocumentStage` |
| Workflow + signals | `pipeline/workflows.py` |
| DEV ingest / PROD promote activities | `pipeline/activities.py` |
| Approve prod API | `pipeline/api.py` → `POST .../approve-prod` |
| Permissions | `pipeline/auth/permissions.py` |
| Groups / super_admin | `pipeline/auth/groups.py` |
| Tenancy | `pipeline/auth/tenancy.py` |
| Keycloak groups / roles | this doc §3 + `keycloak/import/` |
| UI action → permission | `ui/src/views/DocumentOpsView.jsx` (`ACTION_PERMISSION`) |
| Role catalog UI | `ui/src/lib/roleCapabilities.js` |

---

## 13. One-page story

```text
  Login (Keycloak)
        │
        ▼
  Resolve role + states ──► Super Admin? ──yes──► full platform
        │                         │
        │ no                      │
        ▼                         │
  State operator (MH / UP / …)    │
        │                         │
        ▼                         │
  Upload → OCR → Translate → Chunk │
  (reviews at each gate)          │
        │                         │
        ▼                         │
  Approve ingestion ──────────────┼──► Write vectors to Qdrant DEV
        │                         │
        ▼                         │
  Stage = approval_for_prod       │
  (state work complete)           │
        │                         │
        └──── Super Admin review ─┘
                    │
                    ▼
           Approve for Prod
                    │
                    ▼
           Promote DEV → PROD
                    │
                    ▼
              Stage = completed
```

**State lane ends at DEV. Bharat Vistaar Super Admin alone opens the gate to PROD.**
