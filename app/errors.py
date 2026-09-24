"""Domain errors mapped to HTTP responses by the API layer."""
from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class TenantNotFoundError(AppError):
    status_code = 404
    code = "tenant_not_found"


class UploadNotFoundError(AppError):
    status_code = 404
    code = "upload_not_found"


class RowNotFoundError(AppError):
    status_code = 404
    code = "row_not_found"


class BadFileError(AppError):
    status_code = 400
    code = "bad_file"


class PublishBlockedError(AppError):
    status_code = 409
    code = "publish_blocked"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
