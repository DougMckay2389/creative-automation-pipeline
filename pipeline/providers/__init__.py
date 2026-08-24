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

import os

from .base import (EditRequest, GenerationRequest, GenerationResult, Provider,
                   ProviderError)

__all__ = [
    "Provider", "GenerationRequest", "GenerationResult", "EditRequest",
    "ProviderError",
    "get_provider", "available_providers", "provider_status", "default_provider",
    "supports_edit", "CREDENTIALS",
]

# What each adapter needs in the environment before it can do anything.
#
# Registration only fails on an IMPORT error, so every adapter shows up in
# available_providers() whether or not it can run -- which is how the app
# ended up offering `gemini` in a dropdown that could only answer
# "unknown provider". Credentials are checked in each adapter's __init__,
# which is the right place to enforce them and the wrong place to ask.
CREDENTIALS: dict[str, list[str]] = {
    "mock": [],
    "cloudflare": ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"],
    "gemini": ["GEMINI_API_KEY"],
    "firefly": ["FIREFLY_CLIENT_ID", "FIREFLY_CLIENT_SECRET"],
}

# Preferred when more than one is usable. Gemini first, on Doug's explicit
# preference for the "nano banana" family (gemini-2.5-flash-image) -- it does
# not carry Cloudflare Workers AI's NSFW classifier, which false-positived on
# ordinary beauty-photography language ("dewy", "sheen") and 400'd a real
# campaign run. Cloudflare stays next: it is the one this repo is set up
# against by default, it is cheap, and it honours a seed. Firefly, the actual
# target product for this exercise, is deliberately still in the running --
# whichever of the three actually has a key wins, in this order.
PREFERENCE = ("gemini", "cloudflare", "firefly")

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


def provider_status() -> list[dict]:
    """Every adapter, and whether it could actually run right now.

    Reports what is MISSING by name rather than a bare boolean, so a caller
    can tell somebody which variable to set instead of "not configured".
    Values are never read back out -- only presence is ever reported.
    """
    out = []
    for name in available_providers():
        needs = CREDENTIALS.get(name, [])
        missing = [k for k in needs if not (os.environ.get(k) or "").strip()]
        out.append({"name": name, "requires": needs, "missing": missing,
                    "configured": not missing,
                    # Carried here so the app can enable or grey out the
                    # "generate a new surface" control in the same request
                    # that populates the provider dropdown.
                    "supports_edit": supports_edit(name)})
    return out


def supports_edit(name: str) -> bool:
    """Can this adapter take a reference image?

    Read off the CLASS, not an instance, so the UI can ask before anyone has
    supplied credentials -- constructing a provider raises without a key, and
    "you must configure Cloudflare before we can tell you whether Cloudflare
    can do this" is a silly thing to say to somebody choosing an option.
    """
    if not _REGISTRY:
        _register()
    return bool(getattr(_REGISTRY.get(name), "supports_edit", False))


def default_provider() -> str:
    """The best provider that can actually run.

    Falls back to `mock`, which needs nothing -- so a reviewer who cloned this
    two minutes ago still gets a working default, and somebody with a key gets
    the real model without having to remember a flag.
    """
    have = {p["name"] for p in provider_status() if p["configured"]}
    for name in PREFERENCE:
        if name in have:
            return name
    return "mock"


def get_provider(name: str, **kwargs) -> Provider:
    if not _REGISTRY:
        _register()
    if name not in _REGISTRY:
        raise ProviderError(
            f"unknown provider '{name}'. available: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[name](**kwargs)
