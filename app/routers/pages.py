"""Owner dashboard pages (server-rendered Jinja2 + light vanilla JS + Tailwind CDN)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import audit, db, pipeline, storage
from ..errors import AppError
from ..tenant_config import available_tenants, get_tenant

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).parent.parent / "templates"))

DEFAULT_TENANT = "spicehub-kitchen-troy"


@router.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "tenants": available_tenants(),
            "default_tenant": DEFAULT_TENANT,
        },
    )


@router.get("/tenants/{tenant_id}/uploads/{upload_id}/report")
def report_page(request: Request, tenant_id: str, upload_id: str):
    tenant = get_tenant(tenant_id)
    report = pipeline.get_report(tenant_id, upload_id)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "tenant": tenant.__dict__,
            "report": report,
            "categories": tenant.categories,
            "dietary_types": tenant.dietary_types,
            "spice_levels": tenant.spice_levels,
            "allergens": tenant.allergens,
        },
    )


@router.get("/tenants/{tenant_id}/uploads/{upload_id}/preview")
def preview_page(request: Request, tenant_id: str, upload_id: str):
    tenant = get_tenant(tenant_id)
    report = pipeline.get_report(tenant_id, upload_id)
    if report["hard_errors"] > 0:
        return RedirectResponse(
            f"/tenants/{tenant_id}/uploads/{upload_id}/report?blocked=1", status_code=303
        )
    preview = pipeline.build_preview(tenant, upload_id)
    return templates.TemplateResponse(request, "preview.html", {"menu": preview, "mode": "preview"})


@router.get("/tenants/{tenant_id}/kiosk")
def kiosk_page(request: Request, tenant_id: str):
    tenant = get_tenant(tenant_id)
    state = db.query_one("SELECT active_version FROM tenant_state WHERE tenant_id=?", (tenant_id,))
    if state is None or state["active_version"] is None:
        return templates.TemplateResponse(
            request, "kiosk_empty.html", {"tenant": tenant.__dict__}
        )
    doc = storage.storage.read_menu_doc(tenant_id, state["active_version"])
    preview = _document_to_preview(tenant, doc)
    return templates.TemplateResponse(
        request, "preview.html", {"menu": preview, "mode": "active", "version": doc.get("version")}
    )


def _document_to_preview(tenant, doc: dict) -> dict:
    by_category: dict[str, list[dict]] = {}
    for item in doc.get("items", []):
        by_category.setdefault(item.get("category") or "Uncategorized", []).append(item)
    order = {c: i for i, c in enumerate(tenant.categories)}

    def to_preview_item(i: dict) -> dict:
        return {
            "item_id": i["item_id"],
            "item_name": i["item_name"],
            "description": i["description"],
            "price": i["price"],
            "currency": i.get("currency", "USD"),
            "dietary_type": i["dietary_type"],
            "spice_level": i.get("spice_level", "None"),
            "is_featured": i["is_featured"],
            "is_available": i["is_available"],
            "image_url": i["image"]["url"].replace("/media/tenants", "/media/tenants"),
        }

    categories = [
        {"name": c, "items": sorted(
            (to_preview_item(i) for i in items), key=lambda i: i.get("sort_order") or 0
        )}
        for c, items in sorted(by_category.items(), key=lambda kv: order.get(kv[0], 999))
    ]
    featured = [to_preview_item(i) for i in doc.get("items", []) if i.get("is_featured")]
    return {
        "tenant": {
            "tenant_id": tenant.tenant_id,
            "restaurant_name": tenant.restaurant_name,
            "city": tenant.city,
        },
        "featured": featured,
        "categories": categories,
    }


@router.get("/tenants/{tenant_id}/versions")
def versions_page(request: Request, tenant_id: str):
    tenant = get_tenant(tenant_id)
    versions = db.query(
        "SELECT version_no, upload_id, published_by, created_at, is_active"
        " FROM menu_versions WHERE tenant_id=? ORDER BY version_no DESC",
        (tenant_id,),
    )
    jobs = db.query(
        "SELECT * FROM jobs WHERE tenant_id=? ORDER BY id DESC LIMIT 30", (tenant_id,)
    )
    return templates.TemplateResponse(
        request,
        "versions.html",
        {
            "tenant": tenant.__dict__,
            "versions": [dict(v) for v in versions],
            "jobs": [dict(j) for j in jobs],
            "audit_log": audit.recent(tenant_id, limit=50),
        },
    )


@router.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------------
# Media: serve stored images for the dashboard / kiosk preview
# --------------------------------------------------------------------------

@router.get("/media/tenants/{tenant_id}/{rest:path}")
def media(tenant_id: str, rest: str):
    # Path-traversal protection: resolve and confirm the file stays in storage.
    base = (storage.storage.root() / "tenants" / tenant_id).resolve()
    target = (base / rest).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise AppError("Not found.", code="media_not_found")
    return FileResponse(target)
