# SpiceHub Menu Ingestion — Kiosk SDK block (SoraMinds technical assignment)

A restaurant owner (non-technical) uploads a menu spreadsheet (`.xlsx`/`.csv`) and a folder
of item photos. The system ingests, normalizes, validates and repairs the data, guides the
**owner** — never an engineer — through resolving every error, ambiguity and conflict, and
publishes a validated, structured, kiosk-ready menu.

```
Upload → Column Mapping → Field Validation → Auto-Repair →
Structured Output (+ RAG index) → Owner Validation Report →
Live Preview → Publish → Downstream Jobs
```

Reference: `Menu_Ingestion_Engineering_Spec.md` (the spec is the primary contract;
this README explains how to run the implementation).

---

## Quickstart (both demo paths in under 5 minutes)

```bash
# 1. Install (Python 3.11+)
python -m venv .venv && source .venv/bin/activate  <--Linux
python -m venv .venv; .\.venv\Scripts\Activate.ps1 <-Windows
pip install -r requirements.txt

# 2. Generate the demo data (messy + clean menus, 29 synthetic photos, zips)
python scripts/seed_demo.py

# 3. Run the headless end-to-end demo through the real API
python scripts/demo_run.py

# 4. Or drive it yourself in the browser:
python -m uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

For the browser demo, upload `demo/messy_menu.xlsx` + `demo/photos.zip` on the upload page
(default tenant `spicehub-kitchen-troy`), fix the flagged rows with the inline editors,
hit **Live preview**, then **Publish**. `demo/clean_menu.xlsx` + `demo/photos_clean.zip` is
the happy path that publishes with zero fixes.

The demo run writes the final kiosk-ready JSON to `demo/output/`:

- `kiosk_menu_v1_messy_fixed.json` — published after the owner resolved every issue
- `kiosk_menu_v2_clean.json` — the clean happy-path publish

### What the messy path demonstrates

| Deliberate issue | How it surfaces | Owner resolution |
|---|---|---|
| Missing price | hard error, row flagged | inline price edit |
| Bad category `Currys` (ambiguous: Veg/Non Veg) | hard error with suggestions | category dropdown |
| Price `$14,99` | auto-repaired to 14.99 → then a *photo conflict* | pick photo or sheet value |
| Blank description | auto-generated, flagged `auto_generated: true` | editable, review flag |
| Photo/sheet price conflict ($11.99 vs $13.99) | hard error from the vision pass | pick either value |
| Row missing its photo + orphan photo | hard error + orphan gallery | assign photo to item |
| Near-duplicate rows | warning (never blocks) | partial re-upload rename |
| 12 featured items | upload-level warning | informational |
| Malformed calorie range | soft error | inline edit |
| Unknown allergen | soft error with suggestion | inline edit |
| Lowercase `non-veg`, blank sort/spice/defaults | auto-repair (audited per row) | nothing — already fixed |

No API key is required for any of this: descriptions fall back to a deterministic
template generator and the vision pass falls back to local OCR (see below).

---

## Running the tests

```bash
python -m pytest tests/ -v
```

The suite covers every validation rule (one test per rule), the auto-repair
transformations, the re-validate-after-edit path, publish blocking, partial re-upload
merge, orphan photo assignment, versioning/rollback, and the worst-case load test
(500 rows, every row erroneous — must complete fast and report every error).

## Configuration

Everything tenant-specific lives in `config/tenants/<tenant_id>.yaml`: the category
list, allergen list, dropdown options, limits (5 MB / 500 rows) and thresholds
(0.85 fuzzy-match, ambiguity gap, duplicate similarity, featured warning count).
A different cuisine is a new YAML file, not a code change.

Regenerate an owner template for any tenant with data-validation dropdowns:

```bash
python scripts/generate_template.py spicehub-kitchen-troy my_template.xlsx
```

## Environment variables

Copy `.env.example` to `.env` (never commit real keys):

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Optional. Enables gpt-4o-mini description generation and the OpenAI vision pass; every call is logged to the `llm_usage` table (provider, model, tokens). |
| `SPICEHUB_EMBEDDING_PROVIDER` | `auto` (default: sentence-transformers if installed, else a deterministic local fallback), `local`, or `fallback`. |
| `SPICEHUB_EMBEDDING_MODEL` | Defaults to `all-MiniLM-L6-v2`. |
| `OPENAI_BASE_URL`, `OPENAI_CHAT_MODEL`, `OPENAI_VISION_MODEL` | Point at any OpenAI-compatible endpoint/model. |

Without `OPENAI_API_KEY` the system degrades gracefully: descriptions come from a
deterministic template (still flagged `auto_generated: true`), and photo-vs-sheet
conflict detection uses local OCR (`rapidocr-onnxruntime`, optional install). With
neither, the vision pass is skipped and noted in the report — the prototype stays
fully runnable.

## API reference

| Endpoint | Purpose |
|---|---|
| `POST /tenants/{id}/menu/upload` | Multipart: `file` (xlsx/csv) + `photos[]` (files and/or one .zip). Runs Steps 1–5, returns `uploadId`. |
| `GET /tenants/{id}/menu/uploads/{uploadId}/validation-report` | Per-row status from Step 6. |
| `PATCH /tenants/{id}/menu/uploads/{uploadId}/items/{row}` | Inline single-row fix; re-validates that row. Body: `{"changes": {...}}`. |
| `POST /tenants/{id}/menu/uploads/{uploadId}/items/{row}/dismiss-conflict` | Owner confirms the sheet value; photo is stale. |
| `POST /tenants/{id}/menu/uploads/{uploadId}/photos/{filename}/assign` | Assign an unmatched photo to a row. Body: `{"row_index": n}`. |
| `POST /tenants/{id}/menu/uploads/{uploadId}/preview` | Step 7 kiosk preview JSON. 409 while hard errors remain. |
| `POST /tenants/{id}/menu/uploads/{uploadId}/partial-reupload` | Merge a partial spreadsheet by `item_id` (correction path b). |
| `POST /tenants/{id}/menu/uploads/{uploadId}/publish` | Step 8. 409 while hard errors remain; returns the new version. |
| `GET /tenants/{id}/menu/active` | Current live structured menu document. |
| `GET /tenants/{id}/menu/featured` | Featured strip (`is_featured: true` from the active menu). |
| `GET /tenants/{id}/menu/versions` / `.../versions/{n}/doc` | Version history / immutable raw documents. |
| `POST /tenants/{id}/menu/versions/{n}/rollback` | Atomic rollback to any prior version. |

Interactive docs: `http://127.0.0.1:8000/docs` (FastAPI/OpenAPI).

## Repository layout

```
app/
  main.py            FastAPI app + exception mapping
  settings.py        env-driven settings (no secrets in code)
  tenant_config.py   tenant YAML loader
  db.py              SQLite schema + batched-write helpers
  storage.py         local filesystem standing in for S3 (one-class swap)
  security.py        MIME + magic-byte + size + malware-scan (stub) intake
  pipeline.py        the 8 pipeline steps, owner corrections, publish
  records.py         canonical kiosk-ready record builder (spec §7)
  models.py          Pydantic API boundary models
  audit.py           audit log for every mutating action
  ingest/            sheet parsing, column mapping, rule table, auto-repair,
                     embeddings, LLM/vision services with graceful degradation
  routers/           menu API + Jinja2 dashboard pages + media
  templates/         upload / validation report / kiosk preview / versions
scripts/
  seed_demo.py       demo menus + synthetic photos (incl. orphan + conflict)
  demo_run.py        headless end-to-end demo through the real API
  generate_template.py  tenant-scoped xlsx template with dropdowns
config/tenants/      tenant-scoped category/allergen/threshold YAML
demo/                generated demo assets + committed kiosk-ready JSON output
tests/               pytest suite (per-rule, auto-repair, API flow, 500-row load)
```

`ARCHITECTURE.md` explains the pipeline, the key technical decisions and their
trade-offs. `Menu_Ingestion_Engineering_Spec.md` is the spec this implements.
