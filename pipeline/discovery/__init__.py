"""Look-alike discovery behind one interface, chosen by what is configured.

Same shape as `providers/` and `storage/`, for the same reason: the thing that
varies here is *where the evidence came from*, and everything downstream --
strategy, generation, the UI -- should not have to care.

`default_discovery()` prefers a live backend when one is usable and falls back
to `synthetic`, which always runs. That ordering is the same argument as
`default_provider()`: a backend that is configured and then not used because
nobody passed a flag is a footgun, not a safety feature.

Results are cached to `.cache/discovery/` because live discovery is slow and
metered. Re-planning the same product in the same market must not re-run four
scrapers -- the same argument as the master-image cache in `assets.py`.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from .base import (CHANNEL_NAMES, CHANNEL_RATIO, CHANNELS,
                   DISCOVERY_CREDENTIALS, Discovery, DiscoveryError,
                   DiscoveryRequest, Lookalike)
from .synthetic import SyntheticDiscovery

__all__ = ["Discovery", "DiscoveryError", "DiscoveryRequest", "Lookalike",
           "CHANNELS", "CHANNEL_NAMES", "CHANNEL_RATIO",
           "DISCOVERY_CREDENTIALS", "get_discovery", "available_discoveries",
           "discovery_status", "default_discovery", "discover"]

# Tried in order. Apify first because someone else maintains those scrapers;
# playwright second because it keeps everything on this machine but breaks
# more often; synthetic is the floor and never a candidate here, since
# choosing it explicitly would mean "invent the evidence" was a decision
# rather than a fallback.
DISCOVERY_PREFERENCE = ("apify", "playwright")

_REGISTRY: dict[str, type] = {"synthetic": SyntheticDiscovery}
_REGISTERED = False

CACHE_TTL_S = 6 * 60 * 60


def _register() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    try:
        from .apify import ApifyDiscovery
        _REGISTRY["apify"] = ApifyDiscovery
    except Exception:                                        # pragma: no cover
        pass
    try:
        from .playwright_crawl import PlaywrightDiscovery, available
        if available():
            _REGISTRY["playwright"] = PlaywrightDiscovery
    except Exception:                                        # pragma: no cover
        pass


def available_discoveries() -> list[str]:
    _register()
    return sorted(_REGISTRY)


def discovery_status() -> list[dict]:
    """Which backends could actually run, and what each is missing.

    "Imported" and "can run" are different questions, and conflating them is
    how you get a stack trace thirty seconds into a run instead of a sentence
    before it starts.
    """
    _register()
    out = []
    for name in available_discoveries():
        needs = DISCOVERY_CREDENTIALS.get(name, [])
        missing = [k for k in needs if not (os.environ.get(k) or "").strip()]
        out.append({"name": name, "requires": needs, "missing": missing,
                    "configured": not missing,
                    "synthetic": name == "synthetic"})
    return out


def default_discovery() -> str:
    _register()
    for name in DISCOVERY_PREFERENCE:
        if name not in _REGISTRY:
            continue
        needs = DISCOVERY_CREDENTIALS.get(name, [])
        if all((os.environ.get(k) or "").strip() for k in needs):
            return name
    return "synthetic"


def get_discovery(name: str = "") -> Discovery:
    _register()
    name = name or default_discovery()
    if name not in _REGISTRY:
        raise DiscoveryError(
            f"unknown discovery backend '{name}'. "
            f"available: {', '.join(available_discoveries())}")
    return _REGISTRY[name]()


# --------------------------------------------------------------------------
# The cached entry point everything else should call
# --------------------------------------------------------------------------

def _cache_path(root: str, req: DiscoveryRequest, backend: str) -> str:
    key = "|".join([backend, req.product_id, req.locale, req.channel,
                    req.category, req.audience, str(req.limit)])
    h = hashlib.sha256(key.encode()).hexdigest()[:20]
    return os.path.join(root, ".cache", "discovery", f"{h}.json")


def discover(req: DiscoveryRequest, root: str = ".", backend: str = "",
             use_cache: bool = True) -> dict:
    """Find look-alikes, and say plainly where they came from.

    Returns a dict rather than a bare list because the PROVENANCE is not
    decoration -- "eight posts, from Apify, twelve minutes ago" and "eight
    posts, invented from a fixed seed" are different enough that the caller
    must not be able to treat them the same by accident.

    A live backend that fails falls back to synthetic and records why. The
    alternative -- failing the whole run because one channel's scraper broke
    -- would make the engine useless exactly when a site ships new markup,
    and the fallback is visible in the payload rather than silent.
    """
    name = backend or default_discovery()
    path = _cache_path(root, req, name)

    if use_cache and os.path.isfile(path):
        try:
            age = time.time() - os.path.getmtime(path)
            if age < CACHE_TTL_S:
                d = json.load(open(path, encoding="utf-8"))
                d["cached"] = True
                d["cache_age_s"] = int(age)
                return d
        except Exception:                                    # noqa: BLE001
            pass

    fell_back = ""
    try:
        rows = get_discovery(name).find(req)
        used = name
    except Exception as exc:                                 # noqa: BLE001
        if name == "synthetic":
            raise
        fell_back = f"{type(exc).__name__}: {exc}"
        rows = get_discovery("synthetic").find(req)
        used = "synthetic"

    out = {
        "backend": used,
        "requested_backend": name,
        "fell_back_because": fell_back,
        "synthetic": used == "synthetic",
        "channel": req.channel,
        "locale": req.locale,
        "product_id": req.product_id,
        "found_at": int(time.time()),
        "cached": False,
        "cache_age_s": 0,
        "lookalikes": [r.as_dict() for r in rows],
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(out, open(path, "w", encoding="utf-8"), indent=1)
    except OSError:
        pass
    return out
