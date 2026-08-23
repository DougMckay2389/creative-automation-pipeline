"""The filesystem. The default, and never a stub.

The task requires outputs "saved to a folder, clearly organized by product and
aspect ratio", so this backend is a real requirement rather than a fallback --
and it is why a cloud backend mirrors rather than replaces.
"""
from __future__ import annotations

import os

from .base import Storage, StorageError, StoredObject


class LocalStorage(Storage):
    name = "local"

    def __init__(self, root: str = "output", **_ignored):
        self.root = os.path.abspath(root)

    def _path(self, key: str) -> str:
        # A key is always POSIX-ish; the filesystem decides its own separator.
        p = os.path.abspath(os.path.join(self.root, *key.split("/")))
        # Keys come from the brief, which is user input. A product id of
        # "../../etc" must not write outside the output folder.
        if not p.startswith(self.root + os.sep) and p != self.root:
            raise StorageError(f"key escapes the storage root: {key!r}")
        return p

    def put(self, key, data, content_type="application/octet-stream"):
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)        # never leave a half-written file behind
        return StoredObject(key=key, uri=p, size=len(data), backend=self.name)

    def get(self, key):
        try:
            with open(self._path(key), "rb") as fh:
                return fh.read()
        except OSError as exc:
            raise StorageError(f"cannot read {key}: {exc}") from exc

    def exists(self, key):
        return os.path.isfile(self._path(key))

    def uri(self, key):
        return self._path(key)
