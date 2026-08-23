"""Generative image providers behind one interface.

The single most important design decision in this repo lives here.

The pipeline never calls an image API directly. It calls `Provider.generate()`
and gets back PNG bytes. Everything else -- rate limits, async job polling,
auth, which vendor -- is the adapter's problem.

Three consequences, and they are the reason to do it this way:

1. **A reviewer can run this in thirty seconds with no credentials.** The
   `mock` provider renders real pixels deterministically. The pipeline it
   exercises is the same pipeline, byte for byte, that runs against a real
   API. Nothing is stubbed out except the vendor call.

2. **Swapping the model is a flag, not a refactor.** `--provider firefly`
   changes which class is constructed and nothing else. When a client already
   pays for Adobe, that is a one-word migration rather than a project.

3. **The interesting engineering stops being hidden by the API call.** Reuse
   logic, cost control, composition and compliance are where the value is, and
   they are all provider-agnostic.
"""
from __future__ import annotations

from .base import GenerationRequest, GenerationResult, Provider, ProviderError

__all__ = [
    "Provider", "GenerationRequest", "GenerationResult", "ProviderError",
    "get_provider", "available_providers",
]

_REGISTRY = {}


def _register():
    """Import adapters lazily so a missing optional dependency (or a missing
    API key) can never stop the mock provider from working."""
    from .mock import MockProvider
    _REGISTRY["mock"] = MockProvider
    try:
        from .gemini import GeminiProvider
        _REGISTRY["gemini"] = GeminiProvider
    except Exception:                                   # pragma: no cover
        pass
    try:
        from .firefly import FireflyProvider
        _REGISTRY["firefly"] = FireflyProvider
    except Exception:                                   # pragma: no cover
        pass
    try:
        from .cloudflare import CloudflareProvider
        _REGISTRY["cloudflare"] = CloudflareProvider
    except Exception:                                   # pragma: no cover
        pass


def available_providers() -> list[str]:
    if not _REGISTRY:
        _register()
    return sorted(_REGISTRY)


def get_provider(name: str, **kwargs) -> Provider:
    if not _REGISTRY:
        _register()
    if name not in _REGISTRY:
        raise ProviderError(
            f"unknown provider '{name}'. available: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[name](**kwargs)
