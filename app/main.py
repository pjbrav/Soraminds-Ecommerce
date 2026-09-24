"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import settings
from .errors import AppError
from .routers import menu, pages

app = FastAPI(
    title="SpiceHub Menu Ingestion",
    description=(
        "Owner-driven menu ingestion: upload a spreadsheet + photos, normalize, "
        "validate, resolve every issue in the dashboard, publish a kiosk-ready menu. "
        "Spec: Menu_Ingestion_Engineering_Spec.md"
    ),
    version="0.1.0",
)


@app.exception_handler(AppError)
def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


app.include_router(menu.router)
app.include_router(pages.router)
app.mount("/static", StaticFiles(directory=str(settings.REPO_ROOT / "app" / "static")), name="static")


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    from . import db

    db.connect()  # create schema on first boot
