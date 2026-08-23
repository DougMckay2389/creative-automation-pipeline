"""Where a run's artifacts go, behind one interface.

The brief lists Storage as a data source alongside user inputs and GenAI:
somewhere to keep generated or transient assets. In a real engagement that is
never a question of *which* vendor -- it is whichever one the client already
pays for -- so the pipeline talks to a `Storage`, and which class that is gets
decided by a flag.

The same argument as the provider adapters, for the same reason: the
interesting work (reuse, composition, compliance) must not be entangled with
somebody's SDK.

**Local storage is not a stub.** The task also requires outputs saved to a
folder organised by product and aspect ratio, so the filesystem backend is a
real destination that always runs, and a cloud backend MIRRORS to it rather
than replacing it. A run that uploads to S3 still leaves the folder behind.
"""
from __future__ import annotations

from dataclasses import dataclass


class StorageError(RuntimeError):
    """Raised when a backend cannot store or fetch an object."""


# The one key prefix a backend is allowed to expose to anonymous readers.
#
# Defined HERE rather than in s3.py, even though S3 is the only backend that
# does anything with it, because the runner needs it to build keys and the S3
# adapter is imported behind a try/except -- a backend whose dependency is
# missing must not be able to take the whole run down. base.py imports nothing
# outside the standard library, so it is always safe to reach for.
PUBLIC_ROOT = "public"


@dataclass(frozen=True)
class StoredObject:
    """Where something ended up, and how to find it again."""
    key: str
    uri: str                      # s3://bucket/key, or an absolute file path
    size: int
    backend: str


class Storage:
    """The whole contract. Four methods, deliberately.

    Anything larger starts encoding one vendor's ideas about buckets,
    containers, prefixes and ACLs into the pipeline, which is the thing this
    exists to prevent.
    """

    name = "storage"

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def uri(self, key: str) -> str:
        raise NotImplementedError

    def share_url(self, key: str, expires: int = 3600) -> str:
        """A link a person can open, or "" if this backend has no such thing.

        Defined on the base class with a working default so the runner can
        call it on any backend without a `hasattr` dance. The local filesystem
        genuinely has no shareable link -- a path on one machine is not a URL
        anywhere else -- and the honest answer to that is an empty string,
        which the manifest and the UI both read as "no link", rather than a
        `file://` URI that looks like one and works for nobody.
        """
        return ""
