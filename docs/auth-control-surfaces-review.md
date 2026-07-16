# Auth control surfaces — FE + BE review (docs-pipeline)

**Context**  
We need an admin-controlled access layer (Keycloak later): multi-instance (Amul / Bharat Vistaar / Mahavistaar), login via password or Google, users only see instances they have, can run doc processing if allowed, and can put docs in **dev** / **prod** / both. Master admin manages every user’s instance + env access.

**Today’s ask**  
List the places in the app where access should be checked — frontend (what you see/click) and backend (what the API allows).

**Current state**  
The UI and API are open. Anyone who can reach them can do everything. No login yet.

**Two rules**
1. **Website** — hide or turn off things the person shouldn’t use (nice experience).
2. **Server** — actually block the request if they’re not allowed (real security).  
   Hiding a button is not enough. People can still call the API directly.

---

## Frontend — what we should control

| Screen / area | What the user can do there | Who should be allowed (suggestion) |
|---------------|----------------------------|------------------------------------|
| **Upload / New document** | Upload a PDF and start processing | People who may add documents |
| **Queue** | Approve OCR/translation/chunks in bulk; kick reindex | People who run/review the pipeline |
| **Document detail** | Approve stages, edit OCR/translation, tags, retry, reingest | Same — day-to-day document work |
| **Indexes** | Reindex stale docs / full reindex | Admins or senior maintainers |
| **Settings** | Change search defaults | Admins only |
| **Search** | Search Marqo / try queries | Most logged-in users with that instance |
| **Chunk explorer** | Browse/find chunks across docs | Same as search |
| **Audit log** | See who changed what | Admins (or maintainers if we want) |
| **Dashboard / document list / runs** | Browse status | Anyone with access to that instance |
| **Tenant / instance switcher** *(not built yet)* | Switch Amul vs BV vs MH | Only instances that user has |
| **Admin: user access** *(not built yet)* | Set who gets which instance + dev/prod | Master admin only |
| **Doc enablement table** *(not built yet)* `Doc \| Instance \| Dev \| Prod` | Turn a doc on/off for dev/prod | Users with that env access for that instance |

Also hide **nav items** the user can’t use (e.g. don’t show Settings or Upload if they’re not allowed).

---

## Backend — what we must block if not allowed

Same idea, but on APIs. If the UI hides a button but the API stays open, it’s still unsafe.

| Kind of work | Example APIs | Suggestion |
|--------------|--------------|------------|
| **Add documents** | `POST /upload`, `/documents`, `/documents/batch` | Only users allowed to upload |
| **Run / retry pipeline** | reingest, retry-ocr/translation/chunking, reconcile, reindex flags, bulk reindex | Pipeline operators |
| **Review & edit content** | approve-*, patch pages/chunks, tags, auto-tag | Reviewers / curators |
| **Look at search & chunks** | GET pages/chunks, search, PDF, exports | Users with that instance (read) |
| **Turn docs on/off** | disable, restore, demo *(env-specific enable comes next)* | Users with that env right |
| **Change system search config** | PUT/reset `/settings/search`, admin index create/reset | Admins only |
| **Manage users** | *(no APIs yet)* | Master admin only — to be added |

**Public / always ok:** health check. Everything else should eventually require a logged-in user + the right rights + the right instance.

---

## Instance + env (extra rules on top of roles)

1. User opens the app → only see **Amul / BV / MH** they have.
2. Lists and document opens must **filter by instance** on the server, not only in the UI.
3. Enabling a PDF for **dev** or **prod** only if the user has that env for that instance.
4. Prefer one document allowed in **both** envs when that’s what the ticket says (confirm).

---

## How we’d implement this later (simple)

### React (website)
- After login, keep “who am I + what am I allowed.”
- Block whole pages (e.g. Settings) if not allowed.
- Hide or disable buttons (Upload, Approve, Reindex).
- Send the login token on every API call.

### FastAPI (server)
- Check the token on each request.
- If not logged in → 401.
- If logged in but not allowed → 403.
- Also make sure the document’s instance is one they have.

---

## Open questions to confirm

1. Is access “this user can upload on Amul (all docs)” or also “only these specific docs”?
2. Does “enable for prod” simply mean “searchable in the prod index”?
3. Separate rights for enable vs disable vs delete?
4. Can the same PDF be on for both dev and prod?
5. v1 roles: Master Admin + Content Curator only, or more?

---

## Bottom line

| Layer | Job |
|-------|-----|
| Frontend | Show only what they should use |
| Backend | Refuse anything they’re not allowed to do |
| Keycloak (later) | Login + store roles / instances / envs |
| Master admin | Edit those for every user |

Happy to refine this matrix on a quick call — FE/BE checklist is the main deliverable before we wire Keycloak.
