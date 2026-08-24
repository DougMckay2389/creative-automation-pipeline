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

# What `default_discovery()` will pick ON ITS OWN. Apify only.
#
# Playwright is deliberately NOT here, and the reason is a bug this list
# already had. It declares no required credentials -- there is nothing to
# configure, it just needs the package -- so `all(...)` over an empty list is
# True and it was ALWAYS "configured". Anyone who happened to have playwright
# installed silently got the slow, fragile, bot-detectable browser path as
# their default, including a reviewer with no keys who should have got the
# instant offline one. The README promised synthetic in that case and the code
# did something else.
#
# So it stays a real backend you can ask for by name, and never one you get by
# accident. Synthetic is the floor for the same reason `local` is in the
# storage registry: choosing it explicitly would mean "invent the evidence"
# was a decision rather than a fallback.
DISCOVERY_PREFERENCE = ("apify",)

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
                    # Reported, so the UI can show that playwright is usable
                    # without implying it is what a run will actually use.
                    "auto": name in DISCOVERY_PREFERENCE,
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


def get_discovery(name: str = "", root: str = ".") -> Discovery:
    _register()
    name = name or default_discovery()
    if name not in _REGISTRY:
        raise DiscoveryError(
            f"unknown discovery backend '{name}'. "
            f"available: {', '.join(available_discoveries())}")
    cls = _REGISTRY[name]
    # Only the synthetic backend needs to know where the repo is -- it borrows
    # real creatives from output/ to stand in for competitor covers.
    try:
        return cls(root=root)
    except TypeError:
        return cls()


# --------------------------------------------------------------------------
# The cached entry point everything else should call
# --------------------------------------------------------------------------

def _cache_path(root: str, req: DiscoveryRequest, backend: str) -> str:
    key = "|".join([backend, req.product_id, req.locale, req.channel,
                    req.category, req.audience, str(req.limit)])
    h = hashlib.sha256(key.encode()).hexdigest()[:20]
    return os.path.join(root, ".cache", "discovery", f"{h}.json")


THUMB_TIMEOUT_S = 12
THUMB_MAX_BYTES = 4 * 1024 * 1024


def _cache_thumbs(rows: list, root: str) -> None:
    """Pull each look-alike's cover image down once, and serve it from here.

    Hotlinking a social CDN from a local tool fails in three ways that all
    look like "the images are broken": referer checks, signed URLs that expire
    within the hour, and no network at all. Caching also means the evidence
    behind a strategy is still there when somebody opens the run next month,
    which is the whole point of recording evidence.

    Best-effort by design. A cover that will not download costs one thumbnail,
    never the discovery result -- the numbers and the link are the substance
    and they are already in hand.
    """
    import urllib.request

    out = os.path.join(root, ".cache", "discovery", "thumbs")
    os.makedirs(out, exist_ok=True)
    for r in rows:
        url = getattr(r, "thumb_url", "") or ""
        if not url:
            continue
        name = hashlib.sha256(url.encode()).hexdigest()[:20] + ".jpg"
        dest = os.path.join(out, name)
        rel = os.path.relpath(dest, root).replace("\\", "/")
        if os.path.isfile(dest):
            object.__setattr__(r, "thumb", rel)
            continue
        try:
            # A user agent, because several of these CDNs return 403 to the
            # default urllib one -- which reads as "the image is broken"
            # rather than as "you were refused".
            rq = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; creative-automation/1.0)"})
            with urllib.request.urlopen(rq, timeout=THUMB_TIMEOUT_S) as resp:  # noqa: S310
                data = resp.read(THUMB_MAX_BYTES + 1)
            if not data or len(data) > THUMB_MAX_BYTES:
                continue
            from PIL import Image
            import io as _io
            with Image.open(_io.BytesIO(data)) as im:
                # One image, resized then saved. `thumbnail()` mutates in
                # place and returns None, so converting twice would have
                # saved the FULL-SIZE original -- a working-looking bug that
                # quietly stores megabytes per row.
                small = im.convert("RGB")
                small.thumbnail((480, 480), Image.LANCZOS)
                small.save(dest, "JPEG", quality=82, optimize=True)
            object.__setattr__(r, "thumb", rel)
        except Exception:                                    # noqa: BLE001
            continue


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
        rows = get_discovery(name, root).find(req)
        used = name
    except Exception as exc:                                 # noqa: BLE001
        if name == "synthetic":
            raise
        fell_back = f"{type(exc).__name__}: {exc}"
        rows = get_discovery("synthetic", root).find(req)
        used = "synthetic"

    # Covers come down once, here, so every backend's rows reach the UI the
    # same way and the cache file records the local path rather than a signed
    # URL that will be dead by the time anyone re-opens the run.
    _cache_thumbs(rows, root)

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
