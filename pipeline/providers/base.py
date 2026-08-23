"""The provider contract, plus the rate limiter every adapter shares."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol


class ProviderError(RuntimeError):
    """Any failure to produce an image. Adapters normalise vendor errors into this."""


@dataclass(frozen=True)
class GenerationRequest:
    """What the pipeline asks for. Note what is NOT here: no vendor concepts.

    `size` is a hint, not a contract -- providers return their natural size and
    the compose step is responsible for producing exact delivery dimensions.
    Asking the model for an exact spec size is how you end up paying for one
    generation per aspect ratio.
    """
    prompt: str
    seed: int
    size: tuple[int, int] = (1600, 1600)
    negative: str = ""


@dataclass
class GenerationResult:
    png_bytes: bytes
    provider: str
    model: str
    prompt: str
    seed: int
    latency_s: float
    # Credits/cost if the vendor reports it. Kept as a float so the report can
    # total it; None means "the vendor did not tell us".
    cost_units: float | None = None


@dataclass(frozen=True)
class EditRequest:
    """Change part of an existing image instead of inventing a whole one.

    Separate from GenerationRequest on purpose. They look similar -- both carry
    a prompt, a seed and a size -- but they are not substitutable, and folding
    the reference into the generate path as an optional field would let a
    provider that cannot edit silently ignore it and return a text-to-image
    result. The pipeline would then serve a freshly invented bottle under the
    banner "your approved product, new surface", which is the worst outcome
    available: wrong, confident, and invisible.

    A distinct type means a provider without `edit()` fails at the call, loudly.

    `reference_png` is the approved asset, already sized for a reference slot
    by `resurface.prepare_reference`.
    """
    prompt: str
    reference_png: bytes
    seed: int = 0
    size: tuple[int, int] = (1024, 1024)


class Provider(Protocol):
    """Every adapter implements exactly this."""

    name: str
    model: str
    # Whether this adapter implements `edit()`. Declared as a class attribute
    # so callers can ask BEFORE spending a call, and so a provider that gains
    # the capability later announces it by flipping one flag.
    supports_edit: bool

    def generate(self, req: GenerationRequest) -> GenerationResult: ...


class RateLimiter:
    """A token bucket, because generative APIs are rate limited and queues beat retries.

    Firefly Services documents 4 requests/minute on a default entitlement. That
    is an architectural constraint, not a footnote: at 4 rpm a wasted call is a
    wasted minute, which is why the pipeline decides what NOT to generate
    before it decides what to generate.

    Thread-safe so an adapter can be used from a worker pool later without a
    rewrite.
    """

    def __init__(self, rpm: float):
        self.rpm = float(rpm)
        self._interval = 60.0 / self.rpm if self.rpm > 0 else 0.0
        self._next_at = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a call is allowed. Returns seconds actually waited."""
        if self._interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait:
            time.sleep(wait)
        return wait
