"""Pydantic models for API boundaries."""
from __future__ import annotations

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    uploadId: str = Field(..., description="Handle for all follow-up endpoints")
    tenantId: str
    rows: int
    counts: dict
    hardErrors: int
    publishable: bool
    visionProvider: str | None = None
    message: str = ""


class IssueView(BaseModel):
    field: str | None = None
    code: str
    severity: str
    message: str
    suggestion: str | None = None
    extra: dict | None = None


class RowView(BaseModel):
    row_index: int
    item_id: str
    status: str
    fields: dict
    issues: list[IssueView]


class ValidationReport(BaseModel):
    upload_id: str
    tenant_id: str
    filename: str
    status: str
    counts: dict
    hard_errors: int
    publishable: bool
    rows: list[RowView]
    upload_issues: list[IssueView]
    photos: list[dict]


class PatchItemRequest(BaseModel):
    """Inline single-row fix. Only known fields are accepted; the row is
    re-validated on save."""

    changes: dict = Field(default_factory=dict)


class AssignPhotoRequest(BaseModel):
    row_index: int


class PublishResponse(BaseModel):
    version: int
    item_count: int
    resized_images: int
    message: str = ""


class RollbackResponse(BaseModel):
    active_version: int
    message: str
