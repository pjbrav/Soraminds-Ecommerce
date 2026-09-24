# Menu Ingestion — Engineering Specification

**Block:** Menu Ingestion (Onboarding Layer, SDK Block Breakdown)
**Example restaurant:** SpiceHub Kitchen (Troy, MI) — South Indian / Indo-Chinese, ~90 live menu items across 10 categories. Categories, item names, descriptions, and prices below are pulled directly from SpiceHub's real live ordering pages (their direct site and their DoorDash listing), not invented, so the template and examples reflect an actual restaurant's actual structure.

**Goal:** A restaurant owner uploads a spreadsheet + a folder of photos and ends up with a validated, structured, kiosk-ready menu — with **zero engineer or internal-staff involvement**. Every error, ambiguity, or missing field is resolved by the *owner*, in the dashboard, not by a person on the SoraMinds side.

---

## 1. What the reference UI taught us

Looking at SpiceHub's live ordering pages (both their own site and DoorDash) surfaced two structural details worth building in from day one:

- **"Featured Items" is an owner-curated highlight list**, separate from category browsing — SpiceHub hand-picks ~6 items (Goat Biryani, Chicken Biryani, Madras Chicken 65, etc.) to show first. This is an **input** the owner controls, so the template needs a field for it (`is_featured`).
- **"Most Liked" badges and like-percentages (e.g., "#1 Most liked", "85% (7)") are *not* something an owner sets.** They're computed from real order/feedback history over time. This is a **derived, runtime field owned by the Analytics/Feedback blocks**, not the Menu Ingestion block — it must never appear as an upload column, because there's nothing for the owner to correctly fill in at upload time (no orders have happened yet). Flagging this distinction now avoids a confused "why is this column always blank" support question later.

---

## 2. Inputs

| Input | Format | Who provides it |
|---|---|---|
| Menu data | `.xlsx` (the template below) or `.csv` | Restaurant owner |
| Item photos | Individual files or one `.zip`, referenced by filename in the sheet | Restaurant owner |

---

## 3. The Excel Template

Attached: **`SpiceHub_Kitchen_Menu_Upload_Template.xlsx`** — pre-loaded with 27 of SpiceHub's real menu items spanning all 10 of their actual categories, so an owner sees a filled-in model built from a real restaurant, not a placeholder.

**Three sheets:**

1. **Instructions** — plain-language walkthrough, one row per rule.
2. **Category Reference** — matches SpiceHub's actual 10-category structure exactly (see §4), so the dropdown reflects a real, working taxonomy rather than a generic invented one.
3. **Menu Items** — the working sheet. 27 real example rows, followed by 30 blank rows.

### Column schema

| Column | Required | Type | Notes |
|---|---|---|---|
| `item_id` | No | Text | Auto-generated (`MENU-0001`) if left blank |
| `category` | **Yes** | Dropdown | One of the 10 categories in §4 |
| `subcategory` | No | Text | Free text, display grouping only — SpiceHub's real menu doesn't need this level, but it's available for restaurants with deeper hierarchies |
| `item_name` | **Yes** | Text, ≤ 60 chars | Include quantity in the name where the restaurant does, e.g. `"Samosa (2)"`, `"Gulab Jamun (3)"` — matches how SpiceHub's own menu communicates portion count |
| `description` | No | Text, ≤ 200 chars | Auto-generated from name/category/tags if blank |
| `price` | **Yes** | Number, > 0 | No currency symbol; 2 decimals |
| `dietary_type` | **Yes** | Dropdown | Veg / Non-Veg / Egg / Vegan — kept independent of `category`, since e.g. Egg Masala lives in "Non Veg Curries" on the real menu but isn't meat |
| `spice_level` | No | Dropdown | None / Mild / Medium / Hot / Extra Hot — defaults to None |
| `calorie_range` | No | Text | Format `NNN-NNN` |
| `allergens` | No | Text | Comma-separated, validated against a known list |
| `image_filename` | **Yes** | Text | Must match an uploaded photo exactly (case-insensitive) |
| `is_featured` | No | Dropdown | Yes / No — defaults to No. **Owner-controlled**, maps directly to the "Featured Items" strip at the top of the kiosk menu |
| `is_available` | No | Dropdown | Yes / No — defaults to Yes |
| `sort_order` | No | Number | Auto-assigned by row order if blank |
| `tags` | No | Text | Comma-separated; feeds search and the embedding index (§7) |

**Not a column, on purpose:** popularity rank, like-percentage, review count, "Most Ordered" status. These are computed by the Analytics block from real order data after the restaurant is live (§1) — including them here would either be permanently blank at upload time or actively misleading if an owner tried to guess a value.

---

## 4. Category Reference (matches SpiceHub's real structure)

| Category | Example items on SpiceHub's real menu |
|---|---|
| Veg Appetizers | Samosa, Gobi Manchurian, Chilli Paneer |
| Non Veg Appetizers | Chicken 65, Tandoori Shrimp, Chicken Lollipop |
| Veg Curries | Paneer Butter Masala, Dal Makhani, Malai Kofta |
| Non Veg Curries | Butter Chicken, Egg Masala, Chettinad Goat Curry |
| Rice | Veg Fried Rice, Chicken Fried Rice |
| Hyderabadi Dum Biryani | Chicken Biryani, Goat Biryani, Paneer Biryani |
| Breads | Naan, Garlic Naan, Roti |
| Desserts | Gulab Jamun, Rasmalai |
| Drinks | Mango Lassi |
| Soups | Veg Soup, Sweet Corn Soup |

This list is itself a config value, not a hardcoded constant — a different cuisine (e.g., a burger place) needs its own category set. The ingestion pipeline reads whichever category list is configured for that tenant; SpiceHub's list happens to be Indian-cuisine-shaped because that's their real menu.

---

## 5. Pipeline Architecture — Step by Step

```
Upload → Column Mapping → Field Validation → Auto-Repair →
Structured Output (+ RAG index) → Owner Validation Report →
Live Preview → Publish → Downstream Jobs
```

### Step 1 — File Intake

- `POST /api/v1/tenants/{tenantId}/menu/upload` — signed, tenant-scoped multipart upload endpoint.
- Reject anything that isn't `.xlsx` or `.csv` **by MIME type and magic bytes**, before the file is opened by a parser.
- Malware-scan the raw file before it touches any processing code.
- Enforce hard limits: max file size (5 MB), max row count (500 items — comfortably above SpiceHub's real ~90-item menu).
- Store the raw file at `s3://sora-tenant-uploads/{tenantId}/menu/raw/{uploadId}.xlsx`.

### Step 2 — Column Mapping

- Exact-match header row against the canonical column list (case-insensitive, trimmed).
- For any header that doesn't match: embedding similarity between the header text and each canonical field's reference phrases (e.g., `"Dish"`, `"Item"` both map to `item_name`). Auto-map above a `0.85` cosine-similarity threshold; below that, flag as `unmapped_column`.
- Persist the mapping used, for audit.
- **This only matters for CSV/non-template uploads** — an owner using the shipped `.xlsx` template never hits it, since headers are already exact.

### Step 3 — Field Validation

Run every rule below against every row; collect every error rather than stopping at the first one.

| Field | Rule | On failure |
|---|---|---|
| `item_name` | Non-empty, 1–60 chars, no HTML/script tags | Hard error |
| `category` | Exact match to the tenant's category list, or ≥ 0.85 fuzzy match | Hard error |
| `price` | Number, > 0, ≤ 2 decimals, sanity ceiling (< $500) | Hard error |
| `dietary_type` | One of Veg / Non-Veg / Egg / Vegan | Hard error |
| `image_filename` | Matches an uploaded file (case-insensitive) | Hard error |
| `description` | ≤ 200 chars if present | Auto-fix if blank |
| `spice_level` | One of the fixed list if present | Auto-fix if blank |
| `calorie_range` | Regex `^\d{2,4}-\d{2,4}$` if present | Soft error |
| `allergens` | Each comma-separated value in known allergen list | Soft error |
| `sort_order` | Integer if present | Auto-fix if blank |
| `is_featured` = Yes, count across menu | Warn if more than ~10 items flagged featured | Warning only — defeats the point of "featured" but shouldn't block |
| `item_name` + `category` combo | Near-duplicate detection | Warning only |

### Step 4 — Auto-Repair (before anything reaches the owner)

- Trim whitespace; normalize currency symbols/commas in `price`; normalize casing on dropdown fields.
- **Blank `description`:** LLM-generate from `item_name + category + dietary_type + tags`. Mark `auto_generated: true` so the owner knows to skim it before publishing — doesn't block the upload.
- **Blank `sort_order`:** sequential by row order within category.
- **Blank `spice_level` / `is_available` / `is_featured`:** apply documented defaults (`None` / `Yes` / `No`).

### Step 5 — Structured Output ("RAG-ready" knowledge base)

Every validated row becomes one canonical JSON record (schema in §7). This JSON — not the Excel file — is the system's source of truth from this point forward.

- Write the full menu as a versioned document: `s3://sora-tenant-data/{tenantId}/menu/menu-v{n}.json`.
- Write each item as an individually queryable row in the tenant's `menu_items` table.
- **Generate a text embedding per item** (from `item_name + description + tags + category`) and store it in a tenant-scoped vector index.
  - Building this now — even though v1's avatar is narration-only, not conversational — means a future "what's spicy?" filter or Q&A avatar can be added without re-processing every tenant's menu from scratch.
- Version every publish (`menu-v1`, `menu-v2`, ...). Never overwrite.

### Step 6 — Owner-Facing Validation Report (the "no human" step)

- Per-row status: `Valid` / `Warning` / `Error`, exact field(s) at fault.
- Plain-language reason + suggested fix where derivable — e.g., *"Category 'Currys' not recognized — did you mean 'Veg Curries' or 'Non Veg Curries'?"*
- **Block Publish** until zero hard errors remain. Warnings never block.
- Two correction paths, neither requiring a full re-upload: inline edit (re-validates that row only), or partial re-upload merged by `item_id`.

### Step 7 — Live Preview

- Auto-render the actual kiosk menu screens from the same structured JSON that will go to production — including the Featured Items strip at the top, pulling from `is_featured: true` items, exactly as SpiceHub's own site displays it.
- Only reachable once Step 6 shows zero hard errors.

### Step 8 — Publish

- Atomic, instant switch of `active_menu_version` — no downtime, previous versions retrievable for rollback.
- Automatically triggers: (1) batch TTS generation for new/changed items → feeds Avatar Playback, (2) image resize/compression for new images → feeds Image QA, (3) search/embedding index refresh.

---

## 6. Auto-Repair Logic (detail)

| Condition | Automated action |
|---|---|
| `description` blank | LLM-generate from `item_name`, `category`, `dietary_type`, `tags`; flag `auto_generated: true` |
| `spice_level` blank | Default to `None` |
| `is_available` blank | Default to `Yes` |
| `is_featured` blank | Default to `No` |
| `sort_order` blank | Sequential by row order within category |
| Header doesn't exact-match | Embedding fuzzy-match ≥ 0.85 → auto-map; below → flag `unmapped_column` |
| Currency symbol / commas in price | Strip and parse (`"$13.99"` → `13.99`) |
| Extra whitespace | Trim on all text fields |
| Casing mismatch on dropdown fields | Normalize to canonical casing (`"non-veg"` → `"Non-Veg"`) |

---

## 7. Menu Item JSON Schema (RAG-ready record)

Example using a real SpiceHub item:

```json
{
  "item_id": "MENU-0016",
  "tenant_id": "spicehub-kitchen-troy",
  "category": "Hyderabadi Dum Biryani",
  "subcategory": "",
  "item_name": "Chicken Biryani",
  "description": "Chicken cooked with spices and tomatoes, layered in basmati rice and exotic spices and served with boiled egg.",
  "description_auto_generated": false,
  "price": 13.99,
  "currency": "USD",
  "dietary_type": "Non-Veg",
  "spice_level": "Medium",
  "calorie_range": "550-700",
  "allergens": ["Egg"],
  "image": {
    "filename": "chicken_biryani.jpg",
    "url": "https://cdn.soraminds.com/tenants/spicehub-kitchen-troy/menu/chicken_biryani.jpg",
    "status": "approved"
  },
  "is_featured": true,
  "is_available": true,
  "sort_order": 16,
  "tags": ["signature", "dum-cooked"],
  "embedding": {
    "model": "text-embedding-v1",
    "vector_id": "vec_7c3f91",
    "source_text": "Chicken Biryani. Hyderabadi Dum Biryani. Chicken cooked with spices and tomatoes, layered in basmati rice and exotic spices and served with boiled egg. signature, dum-cooked."
  },
  "voice": {
    "audio_url": "https://cdn.soraminds.com/tenants/spicehub-kitchen-troy/voice/chicken_biryani.mp3",
    "status": "generated",
    "generated_at": "2026-09-18T04:12:00Z"
  },
  "runtime_stats": {
    "note": "Populated by the Analytics/Feedback blocks after launch, never by menu upload.",
    "popularity_rank": null,
    "like_percentage": null,
    "review_count": 0
  },
  "menu_version": 1,
  "created_at": "2026-09-18T04:10:00Z",
  "updated_at": "2026-09-18T04:10:00Z"
}
```

Note the explicit `runtime_stats` block, kept structurally separate from owner-entered fields, with a comment documenting *why* it's null at ingestion time — this is a direct fix for the "Most Liked isn't an input" finding in §1.

---

## 8. API Contract (sketch)

| Endpoint | Purpose |
|---|---|
| `POST /tenants/{id}/menu/upload` | Accept the file, kick off Steps 1–5, return `uploadId` |
| `GET /tenants/{id}/menu/uploads/{uploadId}/validation-report` | Read per-row status from Step 6 |
| `PATCH /tenants/{id}/menu/uploads/{uploadId}/items/{rowId}` | Inline single-row fix |
| `POST /tenants/{id}/menu/uploads/{uploadId}/preview` | Trigger Step 7 |
| `POST /tenants/{id}/menu/uploads/{uploadId}/publish` | Step 8 |
| `GET /tenants/{id}/menu/active` | Current live structured menu |
| `GET /tenants/{id}/menu/featured` | Convenience endpoint for the kiosk's Featured Items strip — filters `is_featured: true` from the active menu |

---

## 9. Engineer Task Checklist (build order)

1. Confirm the category list and allergen list are tenant-configurable (product decision — SpiceHub's 10 Indian-cuisine categories are one config, not the only config).
2. Build the `.xlsx` template generator with data-validation dropdowns sourced from the tenant's Category Reference.
3. Build the upload endpoint: storage, malware scan, size/row-count limits.
4. Build the column-mapping module: exact-match first, embedding fuzzy-match fallback.
5. Build the field validator as a rule table (config, not per-field hardcoded logic).
6. Build the auto-repair pass (§6).
7. Integrate the LLM description-generation service for blank descriptions.
8. Build the structured JSON writer + version pointer, including the `runtime_stats` placeholder block.
9. Build embedding generation + tenant-scoped vector index write.
10. Build the validation-report API + dashboard UI with suggested-fix text.
11. Build the inline single-row edit + re-validate endpoint.
12. Build the partial-re-upload merge logic.
13. Build the live-preview renderer, including the Featured Items strip.
14. Build the publish endpoint: atomic switch + automatic downstream job triggers.
15. Add an audit log.
16. Load-test with a worst-case file (500 rows, every row containing an error).

---

## 10. What "Zero Human" Actually Depends On

Being honest about the one soft spot: **auto-generated descriptions and fuzzy category/column matching are probabilistic, not deterministic.** "Zero human" only holds because:

- Every auto-generated or auto-matched value is clearly flagged to the *owner* — the owner catches the rare bad guess, not an engineer.
- The fuzzy-match threshold (0.85) is tuned conservatively: better to ask the owner to confirm an ambiguous mapping (e.g., "Currys" → which of two curry categories?) than to silently mis-file an item.

This is the difference between "no engineer touches this" (true, and what's built here) and "the output is never wrong" (not promised — the owner's own review step is the real safety net).
