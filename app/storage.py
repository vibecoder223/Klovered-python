"""Local-disk object storage (replaces Supabase Storage / Spaces).

Files are keyed by a relative path like ``<deal_id>/<ts>-<name>``. The single
chokepoint here means swapping to an S3/Spaces adapter later is a one-file
change — callers never see the filesystem.
"""

import os
import shutil
from pathlib import Path

from .config import get_settings


def _base() -> Path:
    base = Path(get_settings().storage_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve(rel_path: str) -> Path:
    base = _base()
    dest = (base / rel_path).resolve()
    # Path-traversal guard: dest must stay under the storage root.
    if not str(dest).startswith(str(base) + os.sep):
        raise ValueError("invalid storage path")
    return dest


def save(rel_path: str, data: bytes) -> None:
    dest = _resolve(rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def read(rel_path: str) -> bytes:
    return _resolve(rel_path).read_bytes()


def delete(rel_path: str) -> None:
    dest = _resolve(rel_path)
    if dest.exists():
        dest.unlink()


def delete_dir(rel_path: str) -> int:
    """Remove an entire directory (e.g. a deal's upload folder). Returns the
    number of files removed."""
    dest = _resolve(rel_path)
    if not dest.exists():
        return 0
    count = sum(1 for p in dest.rglob("*") if p.is_file())
    shutil.rmtree(dest, ignore_errors=True)
    return count
