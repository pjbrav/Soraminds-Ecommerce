"""Local-filesystem storage standing in for S3 object storage.

Every path is namespaced by tenant, mirroring the production layout:
    s3://sora-tenant-uploads/{tenantId}/menu/raw/{uploadId}.xlsx
    s3://sora-tenant-data/{tenantId}/menu/menu-v{n}.json

Swapping in real object storage later is a one-class change: implement the
same StorageBackend interface against boto3 (see ARCHITECTURE.md).
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from . import settings
from .errors import BadFileError


class LocalStorageBackend:
    """Thin wrapper over a local directory tree; the S3 stand-in."""

    def root(self) -> Path:
        return settings.STORAGE_ROOT

    def tenant_root(self, tenant_id: str) -> Path:
        p = settings.STORAGE_ROOT / "tenants" / tenant_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save_raw_upload(self, tenant_id: str, upload_id: str, filename: str, data: bytes) -> Path:
        raw_dir = self.tenant_root(tenant_id) / "menu" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix or ".bin"
        path = raw_dir / f"{upload_id}{suffix}"
        path.write_bytes(data)
        return path

    def save_photo(self, tenant_id: str, upload_id: str, filename: str, data: bytes) -> Path:
        img_dir = self.tenant_root(tenant_id) / "menu" / "uploads" / upload_id / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        safe = Path(filename).name  # no path traversal
        path = img_dir / safe
        path.write_bytes(data)
        return path

    def photo_path(self, tenant_id: str, upload_id: str, filename: str) -> Path | None:
        path = (
            self.tenant_root(tenant_id) / "menu" / "uploads" / upload_id / "images" / Path(filename).name
        )
        return path if path.exists() else None

    def menu_doc_path(self, tenant_id: str, version_no: int) -> Path:
        return self.tenant_root(tenant_id) / "menu" / f"menu-v{version_no}.json"

    def write_menu_doc(self, tenant_id: str, version_no: int, document: dict) -> Path:
        path = self.menu_doc_path(tenant_id, version_no)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_pretty(document), encoding="utf-8")
        return path

    def read_menu_doc(self, tenant_id: str, version_no: int) -> dict | None:
        path = self.menu_doc_path(tenant_id, version_no)
        if not path.exists():
            return None
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def resized_dir(self, tenant_id: str, version_no: int) -> Path:
        p = self.tenant_root(tenant_id) / "menu" / f"menu-v{version_no}" / "images"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _pretty(document: dict) -> str:
    import json

    return json.dumps(document, ensure_ascii=False, indent=2)


storage = LocalStorageBackend()


def extract_zip(data: bytes) -> dict[str, bytes]:
    """Safely unpack an uploaded .zip of photos -> {filename: bytes}.

    Rejects path traversal, non-image entries and nested directories.
    """
    images: dict[str, bytes] = {}
    allowed_ext = {".jpg", ".jpeg", ".png", ".webp"}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if Path(name).suffix.lower() not in allowed_ext:
                    continue  # skip stray files (e.g. .DS_Store) harmlessly
                images[name] = zf.read(info)
    except zipfile.BadZipFile as exc:
        raise BadFileError("Uploaded .zip is corrupt or not a zip archive.") from exc
    if not images:
        raise BadFileError("The uploaded .zip contained no image files (jpg/png/webp).")
    return images
