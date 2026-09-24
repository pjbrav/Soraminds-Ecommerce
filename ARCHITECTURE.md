# Architecture & Key Technical Decisions

## The pipeline, stage by stage

```
Upload → Column Mapping → Field Validation → Auto-Repair →
Structured Output (+ RAG index) → Owner Validation Report →
Live Preview → Publish → Downstream Jobs
```

**Step 1 — File intake** (`app/routers/menu.py`, `app/security.py`).
Before any parser touches a file: extension check → declared MIME check → magic
bytes (xlsx must start with `PK\x03\x04`; CSV must decode as UTF-8 text without
NULs; images must match their signatures and decode via Pillow) → size limits
(5 MB menu, per-image and total limits) → malware scan. The malware scan is a
**stub with a real interface** (`MalwareScanner` protocol); the production
implementation sends bytes to a clamd socket (ClamAV `INSTREAM`) and raises on
any non-clean verdict — swapping it in is a one-line change at the
`malware_scanner` binding. Raw uploads land under
`storage/tenants/{tenantId}/menu/raw/{uploadId}.xlsx`, mirroring the spec's
`s3://sora-tenant-uploads/...` layout.

**Step 2 — Column mapping** (`app/ingest/mapping.py`). Exact match first
(case-insensitive, trimmed). Anything else is embedded and compared against
per-field *reference phrases* ("Dish", "Item", "Menu Item Name" all map to
`item_name`); ≥ 0.85 cosine auto-maps (flagged in the report), below that is
flagged `unmapped_column` — a hard, owner-visible issue. Missing required
columns abort the upload with a plain-language message. The mapping used is
persisted on the upload row for audit. The header row itself is *located*
(scanning for the row that matches the schema), which is what lets partial
re-upload files with 2–3 columns work, and the template's hint row ("Required —
pick from dropdown"…) is detected and skipped.

**Step 3 — Field validation** (`app/ingest/rules.py`). A **rule table**, not
per-field hardcoded logic: each rule is a `Rule(field, code, severity, check)`
entry over a list, and every rule runs against every row with *every* failure
collected — the validator never stops at the first error. Thresholds and option
lists come from the tenant YAML. Severities: `hard` blocks publish, `soft` /
`warning` never block, `info` is a note (e.g. case-insensitive photo match).
The category rule implements the spec's conservative philosophy explicitly:
exact match passes; a fuzzy match ≥ 0.85 with a *clear* winner auto-maps but is
flagged (soft) for owner review; two candidates within the ambiguity gap
("Currys" → Veg Currys vs Non Veg Currys) is a **hard error asking the owner**;
below threshold is a hard error with the top suggestions attached — the
dashboard renders them as one-click buttons.

**Step 4 — Auto-repair** (`app/ingest/autorepair.py`). Deterministic fixes run
before anything reaches the owner: whitespace trimming, currency-symbol and
separator parsing for prices (`"$14,99"` → 14.99, `"1,299.99"` → 1299.99),
dropdown casing normalization (`non-veg` → `Non-Veg`), HTML/script tag
stripping, documented defaults (`spice_level=None`, `is_available=Yes`,
`is_featured=No`), and sequential `sort_order` within category. **Every repair
is recorded on the row** and shown in the report (an "auto-fixes applied"
disclosure), so the owner can see what the system changed.

**Step 5 — Structured output** (`app/records.py`). Every validated row becomes
one canonical record matching the spec §7 schema — including the structurally
separate `runtime_stats` block (always null at ingestion; populated by the
Analytics/Feedback blocks after launch) and `description_auto_generated`
flagging. Per-item embeddings (see below) are generated and stored in a
tenant-scoped vector index (JSON vectors in SQLite — no vector DB needed at
~90-item scale). On publish, the full menu is written as an immutable versioned
document (`storage/.../menu/menu-v{n}.json`) plus one row per item in the
`menu_items` table.

**Step 6 — Owner validation report**. Recomputed from current state after
*every* mutation (upload, inline edit, partial merge, photo assignment), so
the report is always a true function of the data. Re-validation deliberately
reuses stored OCR results — the vision model never re-runs for a price edit.
Two correction paths, neither requiring a full re-upload: (a) inline
single-row edit via `PATCH`, which re-runs the rule table; (b) partial
re-upload merged by `item_id`, where only the columns present overwrite.

**Step 7 — Live preview**. Rendered from the same JSON that will be published
(`build_preview` reads the stored items, not a parallel representation),
including the Featured Items strip driven by `is_featured`. Gated: preview
returns 409 while any hard error remains.

**Step 8 — Publish**. Blocked until zero hard errors (warnings never block).
Publishing is a single SQLite transaction: version row, per-item records,
embeddings, and the active-version pointer switch atomically — a reader either
sees the old or the new menu, never a half-written one. Downstream jobs:
image resize/compression is **real** (Pillow, 640px, quality-82 JPEGs into the
version's image dir); TTS is a **stub with the real job shape** (a `tts` job
row is logged, `voice.status` stays `not_generated`; production swaps in a
batch call to a TTS service and flips the status); the embedding index refresh
writes per-item vectors. Versions are append-only; rollback is another atomic
pointer switch.

## Key decisions and why

**SQLite over Postgres.** One process, one file, zero setup; the spec's scale
is one tenant's ~90 rows and a hard 500-row ceiling. The access layer is
small (`app/db.py`), all writes go through `db.execute`/`db.executemany`
(batched — the 500-row load test needs one transaction per pass, not 3,500
commits), so moving to Postgres is an adapter change, not a rewrite.

**Local embeddings over API embeddings.** `all-MiniLM-L6-v2` via
sentence-transformers is free per call, deterministic, and reproduces the
spec's "≥ 0.85 cosine similarity" exactly. It powers column mapping, category
fuzzy-matching, and the per-item RAG vectors (stored with model, vector_id and
source_text in each record's `embedding` block). When the package isn't
installed (or `SPICEHUB_EMBEDDING_PROVIDER=fallback` for fast tests), a
deterministic hashed-trigram embedder with a token-overlap blend takes over —
the system never hard-fails on a missing ML dependency.

**LLM usage is cost-aware and degrades gracefully.** With `OPENAI_API_KEY`,
blank descriptions go to a gpt-4o-mini class chat model and *every call* is
logged to the `llm_usage` table (provider, model, input/output tokens, item) —
the accounting pattern you'd need before billing tenants. Without a key, a
deterministic template generator fills in; in both cases
`description_auto_generated: true` marks the record and the report tells the
owner to skim it. The vision pass (photo-vs-spreadsheet conflicts) prefers the
OpenAI vision model, falls back to local OCR (`rapidocr-onnxruntime`, an
optional dependency), and finally skips with a note in the report. The
prototype is fully runnable with zero API keys.

**The `auto_generated` / flagged-probabilistic philosophy.** Anything
probabilistic (auto-generated descriptions, fuzzy-matched categories or
columns) is flagged to the owner rather than silently applied — except the
unambiguous ≥ 0.85 category match, which auto-maps but stays visible as a
soft note. "No engineer touches this" is achieved by making the owner's
review step the safety net, exactly as the spec's §10 argues.

**Rule-table validation, tenant-scoped config.** Validation logic is data: a
list of `Rule` objects over tenant YAML (categories, allergens, dietary types,
spice levels, thresholds, limits). Adding a burger restaurant is a new YAML
file; the pipeline is cuisine-agnostic by construction.

**Human-in-the-loop surface.** Every path an owner can take ends in either a
resolved row or an actionable explanation: hard errors come with plain-language
messages *and* suggested fixes (including machine-readable `extra.options` the
dashboard turns into one-click buttons); conflicts present both values as
buttons; orphan photos get a gallery with an item dropdown; the featured-count
warning explains itself and never blocks.

**Local filesystem standing in for S3.** `LocalStorageBackend` mirrors the
production layout (`tenants/{id}/menu/...`) behind a small class; swapping in
boto3 is a one-class change. The media route resolves and confinement-checks
paths against the storage root, so no traversal is possible.

**What's stubbed (by design) and where production plugs in:**
malware scanning (ClamAV), TTS generation (any batch TTS), signed upload URLs,
multi-worker job queue for downstream jobs, and authentication (endpoints take
`tenant_id` from the path; production scopes by the signed owner session).

## Known simplifications

- Publish runs synchronously in-request; at 500 rows this is seconds, so a
  queue isn't justified yet (the load test documents the timing).
- The `llm_usage` accounting only records OpenAI-compatible calls; the local
  fallbacks are free and thus unlogged.
- Near-duplicate detection compares within a category using token-sorted
  string similarity at 0.95 — tuned so real SpiceHub near-names
  ("Chicken Fried Rice" vs "Chicken 65 Fried Rice") don't warn while true
  misspellings do. It's a warning, never a block.
- Description over-200-chars is a *soft* error (the spec only prescribes
  auto-fix for the blank case); the kiosk truncates gracefully either way.
