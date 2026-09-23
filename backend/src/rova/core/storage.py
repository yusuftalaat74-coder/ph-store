"""save_upload() -> relative path under ROVA_STORAGE_DIR (A2.14)."""
import hashlib
import os
from pathlib import Path

from rova.config import get_settings


def _root() -> Path:
    p = Path(get_settings().storage_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_bytes(subdir: str, filename: str, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    ext = Path(filename).suffix
    rel = f"{subdir}/{digest}{ext}"
    full = _root() / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    return rel


def read_bytes(rel_path: str) -> bytes:
    return (_root() / rel_path).read_bytes()


def full_path(rel_path: str) -> Path:
    """Absolute filesystem path behind a stored relative path, for handlers
    that stream the file back (e.g. FileResponse) rather than reading it
    into memory."""
    return _root() / rel_path


def delete(rel_path: str) -> None:
    """R-116 — the storage path behind a transient upload must be deletable."""
    full = _root() / rel_path
    if full.exists():
        os.remove(full)
