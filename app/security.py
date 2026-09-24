"""File-intake security: type checks, limits, malware-scan stage.

Rejection happens BEFORE any parser opens the file:
  1. extension check
  2. declared MIME type check
  3. magic-bytes (content signature) check
  4. size limit
  5. malware scan (stub layer with a real interface — see below)
"""
from __future__ import annotations

import io
from typing import Protocol

from .errors import BadFileError

XLSX_MAGIC = b"PK\x03\x04"  # xlsx/xlsx-family files are zip archives
IMAGE_MAGICS = {
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"\x89PNG": ("png", "image/png"),
    b"RIFF": ("webp", "image/webp"),  # RIFF....WEBP
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def check_menu_file(filename: str, content_type: str | None, data: bytes, max_mb: int) -> None:
    """Validate a menu spreadsheet upload (xlsx/csv). Raises BadFileError."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        if content_type and content_type not in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",
            "application/zip",
            "application/vnd.ms-excel",
        ):
            raise BadFileError(
                f"'{filename}' declares MIME type '{content_type}', which is not "
                "an Excel spreadsheet."
            )
        if not data.startswith(XLSX_MAGIC):
            raise BadFileError(
                f"'{filename}' is not a real .xlsx file (content signature does not "
                "match a zip/OOXML container)."
            )
    elif name.endswith(".csv"):
        if content_type and content_type not in (
            "text/csv",
            "text/plain",
            "application/vnd.ms-excel",
            "application/octet-stream",
        ):
            raise BadFileError(
                f"'{filename}' declares MIME type '{content_type}', which is not CSV."
            )
        if b"\x00" in data[:4096]:
            raise BadFileError(f"'{filename}' does not look like text CSV (binary content).")
        try:
            data[:4096].decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BadFileError(f"'{filename}' is not valid UTF-8 CSV text.") from exc
    else:
        raise BadFileError(
            f"Unsupported file type '{filename}'. Only .xlsx and .csv menu files are accepted."
        )
    if len(data) > max_mb * 1024 * 1024:
        raise BadFileError(f"'{filename}' is larger than the {max_mb} MB limit.")


def check_image_file(filename: str, data: bytes, max_mb: int = MAX_IMAGE_BYTES) -> str:
    """Validate one uploaded image. Returns the normalized extension."""
    _check_size(filename, data, max_mb)
    name = (filename or "").lower()
    if name.endswith((".jpg", ".jpeg")):
        if not data.startswith(b"\xff\xd8\xff"):
            raise BadFileError(f"'{filename}' is not a real JPEG image (bad signature).")
        return ".jpg"
    if name.endswith(".png"):
        if not data.startswith(b"\x89PNG"):
            raise BadFileError(f"'{filename}' is not a real PNG image (bad signature).")
        return ".png"
    if name.endswith(".webp"):
        if not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
            raise BadFileError(f"'{filename}' is not a real WebP image (bad signature).")
        return ".webp"
    raise BadFileError(
        f"Unsupported image type '{filename}'. Accepted: .jpg, .jpeg, .png, .webp, or a .zip of them."
    )


def _check_size(filename: str, data: bytes, max_mb: int) -> None:
    if len(data) > max_mb * 1024 * 1024:
        raise BadFileError(f"'{filename}' is larger than the {max_mb} MB image limit.")


def validate_image_bytes(filename: str, data: bytes) -> None:
    """Verify the bytes decode as an image (Pillow open). Rejects corrupt files."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad upload
        raise BadFileError(f"'{filename}' is corrupt or not a readable image.") from exc


class MalwareScanner(Protocol):
    """Interface for the malware-scan pipeline stage.

    PRODUCTION NOTE: swap ClamAVStubScanner for ClamAVScanner, which sends the
    raw bytes to a clamd socket (clamd INSTREAM protocol) and raises on any
    non-clean result, e.g. via the `clamd` python package:
        clamd_socket = clamd.ClamdNetworkSocket(host='clamav.internal', port=3310)
        result = clamd_socket.instream(BytesIO(data))  # {'stream': ('OK', None)}
    """

    def scan(self, filename: str, data: bytes) -> None:
        """Raise BadFileError if the file is infected; return silently if clean."""
        ...


class ClamAVStubScanner:
    """Stub that satisfies the MalwareScanner interface and always passes.

    The stage exists so the pipeline hook is real; only the engine is stubbed.
    """

    def scan(self, filename: str, data: bytes) -> None:
        return None


malware_scanner: MalwareScanner = ClamAVStubScanner()
