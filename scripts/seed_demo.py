"""Seed the demo data (spec §10 / build prompt §10).

Produces, under demo/:
  - clean_menu.xlsx  — the shipped template as-is (happy path)
  - messy_menu.xlsx  — a copy with 12 deliberate, owner-resolvable issues
  - photos/          — synthetic sample photos for the 27 template items
  - photos.zip       — the same photos as one archive (what the owner uploads)
  - demo/SpiceHub_Kitchen_Menu_Upload_Template.xlsx must already exist
    (shipped with the repo; the original provided template).

Deliberate issues in messy_menu.xlsx:
  1. missing price                     (Tandoori Shrimp)
  2. bad category "Currys"             (ambiguous: Veg/Non Veg Currys)
  3. price "$14,99"                    (currency symbol + decimal comma)
  4. blank description                (auto-generation demo, Egg Masala)
  5. image_filename wrong case        (GOAT_BIRYANI.JPG)
  6. row missing its photo            (rasmalai.jpg never uploaded)
  7. two near-duplicate rows           (Paneer Biriyani vs Paneer Biryani)
  8. 12 items marked featured          (warning demo)
  9. malformed calorie_range           ("550-7")
 10. unknown allergen                  ("Spices")
 11. lowercase dietary_type            ("non-veg" — auto-repair demo)
 12. blank sort_order everywhere        (auto-repair demo)

Plus in photos/: one ORPHAN photo (chef_special.jpg, matches no row) and one
photo whose rendered price deliberately CONFLICTS with the spreadsheet
(chicken_biryani.jpg shows $11.99 vs the sheet's $13.99) for the vision-based
conflict-resolution demo.

Usage:  python scripts/seed_demo.py
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook

from app.tenant_config import get_tenant

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "demo"
TEMPLATE = DEMO / "SpiceHub_Kitchen_Menu_Upload_Template.xlsx"
FONT = Path(__file__).resolve().parent / "assets" / "DejaVuSans-Bold.ttf"

# (name, category, price, filename, featured) — the subset the photos render.
CONFLICT_PRICE = 11.99  # chicken_biryani.jpg renders this instead of 13.99
MISSING_PHOTO = "rasmalai.jpg"
ORPHAN_PHOTO = "chef_special.jpg"


def read_template_rows() -> list[dict]:
    wb = load_workbook(TEMPLATE, data_only=True)
    ws = wb["Menu Items"]
    headers = [c.value for c in ws[1]]
    rows = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not any(row):
            continue
        rows.append(dict(zip(headers, row)))
    return rows


def make_clean_menu() -> Path:
    target = DEMO / "clean_menu.xlsx"
    shutil.copy(TEMPLATE, target)
    return target


def make_messy_menu(rows: list[dict]) -> Path:
    wb = load_workbook(TEMPLATE)
    ws = wb["Menu Items"]
    headers = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(headers)}

    def set_cell(row_idx: int, header: str, value):
        # NB: ws.cell(..., value=None) is a no-op in openpyxl, so assign directly.
        cell = ws.cell(row=row_idx, column=col[header])
        cell.value = value

    by_name = {}
    for i, row in enumerate(rows):
        by_name[row["item_name"]] = i + 3  # sheet row (1 header + 1 hint row)

    # 1. missing price
    set_cell(by_name["Tandoori Shrimp"], "price", None)
    # 2. ambiguous category
    set_cell(by_name["Butter Chicken"], "category", "Currys")
    # 3. currency symbol + comma price
    set_cell(by_name["Chilli Paneer"], "price", "$14,99")
    # 4. blank description
    set_cell(by_name["Egg Masala"], "description", None)
    # 5. wrong-case image filename
    set_cell(by_name["Goat Biryani"], "image_filename", "GOAT_BIRYANI.JPG")
    # 6. row missing its photo (photo never generated/uploaded)
    set_cell(by_name["Rasmalai (3)"], "image_filename", "rasmalai.jpg")
    # 7. near-duplicate rows: append a misspelled Paneer Biryani
    dup_row = ws.max_row + 1
    source = ws[by_name["Paneer Biryani"]]
    for c in source:
        ws.cell(row=dup_row, column=c.column, value=c.value)
    set_cell(dup_row, "item_name", "Paneer Biriyani")
    set_cell(dup_row, "image_filename", "paneer_biriyani.jpg")
    set_cell(dup_row, "is_featured", "No")
    # 9. malformed calorie range
    set_cell(by_name["Chicken Biryani"], "calorie_range", "550-7")
    # 10. unknown allergen
    set_cell(by_name["Naan"], "allergens", "Gluten,Spices")
    # 11. lowercase dietary type (auto-repair demo)
    set_cell(by_name["Chicken Lollipop - Wet"], "dietary_type", "non-veg")

    # 8. 12 featured items (warning demo) — the template ships 4
    featured = 0
    for i, row in enumerate(rows):
        if row["is_featured"] == "Yes":
            featured += 1
    for i, row in enumerate(rows):
        if featured >= 12:
            break
        if row["is_featured"] != "Yes":
            set_cell(i + 3, "is_featured", "Yes")
            featured += 1

    # 12. blank sort_order everywhere (auto-assign demo)
    for i in range(len(rows) + 1):
        set_cell(i + 3, "sort_order", None)

    target = DEMO / "messy_menu.xlsx"
    wb.save(target)
    return target


def _font(size: int):
    from PIL import ImageFont

    if FONT.exists():
        return ImageFont.truetype(str(FONT), size)
    return ImageFont.load_default()


def make_photo(name: str, price, filename: str, out_dir: Path, cuisine_hint: str = "") -> Path:
    """A simple PIL-generated 'photo card': dish name + price on a colored bg.

    Real OCR can read these, which is what drives the vision conflict demo.
    """
    from PIL import Image, ImageDraw

    palette = [
        (243, 228, 207), (222, 236, 224), (247, 222, 222), (226, 232, 240),
        (245, 236, 210), (232, 226, 240),
    ]
    bg = palette[sum(ord(c) for c in filename) % len(palette)]
    img = Image.new("RGB", (640, 420), bg)
    d = ImageDraw.Draw(img)
    # plate suggestion
    d.ellipse((180, 150, 460, 380), fill=(255, 255, 255))
    d.ellipse((235, 190, 405, 330), fill=bg)

    title_font = _font(44)
    d.text((40, 50), name, font=title_font, fill=(28, 25, 23))
    if cuisine_hint:
        d.text((40, 105), cuisine_hint, font=_font(24), fill=(120, 100, 80))
    price_text = f"${price:.2f}" if price is not None else ""
    d.text((40, 185), price_text, font=_font(56), fill=(176, 32, 32))
    path = out_dir / filename
    img.save(path, "JPEG", quality=90)
    return path


def make_photos(rows: list[dict]) -> Path:
    out_dir = DEMO / "photos"
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = []
    for row in rows:
        filename = row["image_filename"]
        if not filename:
            continue
        if filename == MISSING_PHOTO:
            continue  # deliberate: this row's photo never gets uploaded
        price = row["price"]
        if filename == "chicken_biryani.jpg":
            price = CONFLICT_PRICE  # deliberate conflict with the spreadsheet
        generated.append(
            make_photo(row["item_name"], price, filename, out_dir, "SpiceHub Kitchen")
        )
    # orphan photo: matches no row
    make_photo("Chef's Special Thali", 16.99, ORPHAN_PHOTO, out_dir, "SpiceHub Kitchen")
    # the near-duplicate row's photo
    make_photo("Paneer Biriyani", 13.99, "paneer_biriyani.jpg", out_dir, "SpiceHub Kitchen")

    zip_path = DEMO / "photos.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out_dir.glob("*.jpg")):
            zf.write(p, p.name)

    # The CLEAN happy-path zip: only the 27 item photos, prices matching the
    # spreadsheet (no conflict photo, no orphan, no near-duplicate extra).
    clean_dir = DEMO / "photos_clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        filename = row["image_filename"]
        if filename:  # ALL 27 photos, prices matching the spreadsheet
            make_photo(row["item_name"], row["price"], filename, clean_dir, "SpiceHub Kitchen")
    clean_zip = DEMO / "photos_clean.zip"
    with zipfile.ZipFile(clean_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(clean_dir.glob("*.jpg")):
            zf.write(p, p.name)
    return zip_path


def main() -> None:
    if not TEMPLATE.exists():
        raise SystemExit(
            "demo/SpiceHub_Kitchen_Menu_Upload_Template.xlsx is missing — "
            "restore it from the repo (it is the shipped template)."
        )
    rows = read_template_rows()
    print(f"template rows: {len(rows)}")
    clean = make_clean_menu()
    messy = make_messy_menu(rows)
    zip_path = make_photos(rows)
    print(f"wrote {clean}")
    print(f"wrote {messy}")
    print(f"wrote {DEMO/'photos'} and {zip_path}")
    _ = get_tenant("spicehub-kitchen-troy")  # sanity: tenant config loads


if __name__ == "__main__":
    main()
