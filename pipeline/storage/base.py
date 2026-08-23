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
