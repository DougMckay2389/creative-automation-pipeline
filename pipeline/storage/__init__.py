"""Object storage behind one interface, chosen by a flag.

Mirrors the provider registry deliberately: same shape, same credential
reporting, same "it can import but that does not mean it can run" distinction.
"""
from __future__ import annotations

import os

from .base import Storage, StorageError, StoredObject
from .local import LocalStorage

__all__ = ["Storage", "StorageError", "StoredObject", "get_storage",
           "available_storages", "storage_status", "STORAGE_CREDENTIALS"]

_REGISTRY: dict[str, type] = {"local": LocalStorage}

# `local` needs nothing -- it is the filesystem, and it is also a hard
# requirement of the task, so it can never be unavailable.
STORAGE_CREDENTIALS: dict[str, list[str]] = {
    "local": [],
    "s3": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET"],
}


def _register() -> None:
    try:
        from .s3 import S3Storage
        _REGISTRY["s3"] = S3Storage
    except Exception:                                   # pragma: no cover
        pass


def available_storages() -> list[str]:
    if "s3" not in _REGISTRY:
        _register()
    return sorted(_REGISTRY)


def storage_status() -> list[dict]:
    """Which backends could actually run, and what each is missing."""
    out = []
    for name in available_storages():
        needs = STORAGE_CREDENTIALS.get(name, [])
        missing = [k for k in needs if not (os.environ.get(k) or "").strip()]
        out.append({"name": name, "requires": needs, "missing": missing,
                    "configured": not missing})
    return out


def get_storage(name: str = "local", **kwargs) -> Storage:
    if name not in _REGISTRY:
        _register()
    if name not in _REGISTRY:
        raise StorageError(
            f"unknown storage '{name}'. available: {', '.join(available_storages())}")
    return _REGISTRY[name](**kwargs)
