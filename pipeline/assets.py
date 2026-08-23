"""Deciding what NOT to generate.

The brief says: "Accept input assets and reuse them when available. When
assets are missing, generate new ones."

That sentence hides the most valuable behaviour in the pipeline. Generation is
the slow, expensive, rate-limited step; every other stage is microseconds of
local compute. So the job of this module is to spend as few generative calls
as possible while still producing every requested deliverable.

Three rules, in priority order:

1. **If the brief points at a real file, use it.** No call. The creative team's
   own photography always wins over a generated approximation.
2. **If we generated this exact thing before, reuse it.** Keyed by a hash of
   everything that could change the pixels (prompt, seed, size, provider,
   model). Re-running a brief after fixing a typo in the copy costs nothing.
3. **Only then generate** -- once per product, at master resolution, never
   once per aspect ratio.

Rule 3 is the one worth arguing about in a review. A naive implementation
generates product x market x ratio images. This one generates one master per
product that lacks an asset, and composes everything else from it. For the
sample brief that is 2 products x 3 markets x 3 ratios = 18 deliverables from
**1** generative call.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .brief import Product
from .providers import GenerationRequest, Provider


@dataclass
class MasterAsset:
    """The one high-resolution image every variant of a product is cut from."""
    product_id: str
    path: str
    origin: str              # "brief" | "cache" | "generated"
    provider: str = ""
    model: str = ""
    prompt: str = ""
    seed: int = 0
    latency_s: float = 0.0


def build_prompt(product: Product) -> str:
    """Turn a product entry into a generation prompt.

    Kept in one place so the prompt is a reviewable artifact rather than an
    f-string buried in a call site. In a real engagement this is the thing a
    brand's creative director wants to read and edit, and the vocabulary here
    should match the captions used to train any custom model -- otherwise you
    have a model trained in one language being prompted in another.
    """
    return (
        f"{product.subject}, product photography, centred, "
        f"resting on {product.surface}, soft directional daylight from upper left, "
        f"shallow depth of field, clean neutral background with generous negative space, "
        f"no text, no logos, no people"
    )


NEGATIVE = "text, watermark, logo, hands, people, clutter, busy background"


class AssetResolver:
    """Resolves each product to exactly one master image, cheaply."""

    def __init__(self, provider: Provider, cache_dir: str = ".cache/masters",
                 master_size: tuple[int, int] = (1600, 1600), log=None,
                 force: bool = False):
        self.provider = provider
        self.cache_dir = cache_dir
        self.master_size = master_size
        self.log = log or (lambda *a, **k: None)
        # force: regenerate every product on every run, ignoring both the
        # asset on disk and the cache. Off by default, because reuse is the
        # cost argument this whole pipeline is built on -- one hero becomes
        # every deliverable, and paying a model twice for the same picture is
        # the waste it exists to remove. It is worth having anyway: while you
        # are ITERATING on a prompt, "did my edit do anything" is the only
        # question, and a cache that answers it with yesterday's image is
        # actively in the way.
        self.force = force
        os.makedirs(self.cache_dir, exist_ok=True)

    def _cache_key(self, prompt: str, seed: int) -> str:
        """Hash everything that could change the pixels -- and nothing else.

        Including the provider and model means switching vendors correctly
        misses the cache instead of silently serving the old vendor's image.
        Excluding things like the campaign name means a rename does not throw
        away work.
        """
        payload = json.dumps({
            "prompt": prompt,
            "seed": seed,
            "size": list(self.master_size),
            "provider": getattr(self.provider, "name", "?"),
            "model": getattr(self.provider, "model", "?"),
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    def resolve(self, product: Product, seed: int) -> MasterAsset:
        # --- 1. the creative team's own asset, if it is really there --------
        # Skipped under force: an asset on disk short-circuits everything, so
        # while it is there the product's `subject` and `surface` are never
        # even read -- which is baffling if you have just edited them.
        if product.has_asset() and not self.force:
            self.log("reuse", product=product.id, source=product.asset)
            return MasterAsset(product.id, product.asset, origin="brief")

        prompt = build_prompt(product)
        key = self._cache_key(prompt, seed)
        cached = os.path.join(self.cache_dir, f"{product.id}-{key}.png")

        # --- 2. something we generated earlier, unchanged -------------------
        if os.path.isfile(cached) and os.path.getsize(cached) > 0 and not self.force:
            self.log("cache-hit", product=product.id, source=cached)
            return MasterAsset(product.id, cached, origin="cache", prompt=prompt, seed=seed)

        # --- 3. spend a generative call -------------------------------------
        self.log("generate", product=product.id, prompt=prompt, seed=seed)
        res = self.provider.generate(GenerationRequest(
            prompt=prompt, seed=seed, size=self.master_size, negative=NEGATIVE))

        # Write to a temp name then rename. A crash mid-write must not leave a
        # truncated file in the cache that every future run happily reuses.
        tmp = cached + ".part"
        with open(tmp, "wb") as fh:
            fh.write(res.png_bytes)
        os.replace(tmp, cached)

        return MasterAsset(
            product_id=product.id, path=cached, origin="generated",
            provider=res.provider, model=res.model, prompt=prompt,
            seed=seed, latency_s=res.latency_s)
