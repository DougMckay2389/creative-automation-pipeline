"""Cloudflare Workers AI image adapter.

Added last, in one file, without touching a single line of the pipeline --
which is the clearest possible demonstration of why the provider interface is
worth having. Everything upstream (reuse, cache keys, rate limiting) and
downstream (composition, checks, report) is unchanged.

    export CLOUDFLARE_ACCOUNT_ID=...
    export CLOUDFLARE_API_TOKEN=...        # needs "Workers AI: Read" permission
    python run.py run campaigns/aurora-spring.yaml --provider cloudflare

Why this one is a good fit for this pipeline specifically:

* **Cheap, fast and edge-hosted**, with no region to provision -- which for a
  proof of concept somebody else might run matters more than it sounds.
* **It is cheap enough to be honest about.** Roughly $0.000053 per 512x512
  tile plus $0.00011 per step, so a 4-step 1024x1024 generation is a fraction
  of a cent. `cost_units` carries the estimate through to the run report
  instead of the report pretending generation is free.

Two response shapes exist and both are handled:

* FLUX returns ``{"result": {"image": "<base64>"}}``.
* The Stable Diffusion XL models return **raw image bytes**, not JSON.

Getting that wrong produces a confusing "not valid JSON" error on a request
that actually succeeded, so the adapter branches on content type rather than
assuming.

Earned the hard way, against the live API
-----------------------------------------
**The published schema for ``flux-1-schnell`` lists ``seed``. The endpoint
rejects it.** Sending ``seed`` -- or ``negative_prompt`` -- returns a bare 400
with an empty body, which is a miserable thing to debug because the request
looks correct and the error says nothing. Verified directly:

    prompt + steps               -> 200
    prompt + steps + seed        -> 400
    prompt + steps + negative    -> 400

So this adapter keeps a per-model capability set and sends only fields that
model actually accepts, rather than trusting the documentation. It also
retries once with the minimal body if a 400 comes back, so a model whose
schema changes underneath us degrades instead of failing.

**The consequence is worth stating plainly:** on ``flux-1-schnell`` this
pipeline loses seeded reproducibility, because the model will not take a seed.
That is a real trade against one of the design goals, not something to paper
over -- ``GenerationResult.seed`` is set to 0 when no seed was sent, so the
manifest records honestly that the image is not reproducible. Choose an SDXL
model instead when reproducibility matters more than quality.
"""
from __future__ import annotations

import base64
import os
import time

import requests

from .base import GenerationRequest, GenerationResult, ProviderError, RateLimiter

API_BASE = "https://api.cloudflare.com/client/v4/accounts"
# Leonardo Phoenix, not FLUX schnell.
#
# schnell was the default until it refused the sample brief outright: every
# call for the lipstick product came back 400 "Input prompt contains NSFW
# content" on a plain cosmetics prompt. Measured 8/8 refusals, and 8/8 again
# after rewording the subject to remove anything a classifier could object to
# -- so it is not a prompt that can be written around.
#
# Worse, that message is not always about content. Sending schnell a parameter
# it does not accept produces the SAME "NSFW content" error, which is how a
# schema problem ends up looking like a moderation problem. Do not trust the
# error text to tell you which one you have; change one variable at a time.
#
# Phoenix takes the same prompt 8/8, and honours `seed` -- two runs at a fixed
# seed returned byte-identical images. That matters more here than model
# preference: the whole repo claims the same brief regenerates the same pixels.
DEFAULT_MODEL = "@cf/leonardo/phoenix-1.0"

# What each model will actually accept -- established by trying it, not by
# reading the schema. Anything not listed here gets the conservative minimum.
MODEL_CAPS: dict[str, set[str]] = {
    # Verified by probing the live endpoint, not read off the docs page.
    "@cf/leonardo/phoenix-1.0": {"prompt", "steps", "seed"},
    "@cf/black-forest-labs/flux-1-schnell": {"prompt", "steps"},
    "@cf/stabilityai/stable-diffusion-xl-base-1.0": {
        "prompt", "negative_prompt", "seed", "num_steps", "width", "height"},
    "@cf/bytedance/stable-diffusion-xl-lightning": {
        "prompt", "negative_prompt", "seed", "num_steps", "width", "height"},
}
MINIMAL = {"prompt", "steps"}

# Published pricing, used only to estimate a per-call cost for the run report.
# Wrong-but-declared beats absent: a report that shows generation as free
# teaches the customer the wrong thing about their own spend.
USD_PER_TILE_512 = 0.000053
USD_PER_STEP = 0.00011


class CloudflareProvider:
    name = "cloudflare"

    def __init__(self, rpm: float = 30.0, timeout_s: float = 120.0,
                 steps: int = 4, model: str | None = None, **_ignored):
        self.account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not (self.account_id and self.token):
            raise ProviderError(
                "cloudflare provider needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN")
        # Explicit argument wins, then the environment, then the default. The
        # argument exists so the model can be chosen per RUN -- from the app's
        # dropdown -- without editing .env and restarting.
        self.model = model or os.environ.get("CLOUDFLARE_IMAGE_MODEL") or DEFAULT_MODEL
        # FLUX schnell caps steps at 8; more steps is more money for less and
        # less return, so the default stays low and is overridable.
        self.steps = max(1, min(int(steps), 8))
        self.limiter = RateLimiter(rpm)
        self.timeout_s = timeout_s

    # ----------------------------------------------------------------------

    def _url(self) -> str:
        return f"{API_BASE}/{self.account_id}/ai/run/{self.model}"

    def _estimate_cost(self, w: int, h: int) -> float:
        tiles = max(1, (w * h) / (512 * 512))
        return tiles * USD_PER_TILE_512 + self.steps * USD_PER_STEP

    def generate(self, req: GenerationRequest) -> GenerationResult:
        t0 = time.monotonic()
        self.limiter.acquire()

        caps = MODEL_CAPS.get(self.model, MINIMAL)

        body: dict = {"prompt": req.prompt}
        if "steps" in caps:
            body["steps"] = self.steps
        if "num_steps" in caps:
            body["num_steps"] = self.steps
        # Only send a seed to a model that takes one. flux-1-schnell 400s.
        sent_seed = 0
        if req.seed and "seed" in caps:
            sent_seed = int(req.seed) % (2 ** 31)
            body["seed"] = sent_seed
        if req.negative and "negative_prompt" in caps:
            body["negative_prompt"] = req.negative
        if "width" in caps:
            body["width"], body["height"] = req.size

        r = self._post(body)
        if r.status_code == 400 and set(body) - MINIMAL:
            # The schema moved, or this model is stricter than we knew. Fall
            # back to the smallest request that has ever worked rather than
            # failing the whole run on an optional field.
            r = self._post({k: v for k, v in body.items() if k in MINIMAL})
            sent_seed = 0

        if r.status_code != 200:
            raise ProviderError(
                f"workers ai returned {r.status_code} for {self.model}: "
                f"{(r.text or '<empty body>')[:300]}")

        png = self._extract(r)
        return GenerationResult(
            png_bytes=png,
            provider=self.name,
            model=self.model,
            prompt=req.prompt,
            # 0 means "this image is NOT reproducible" -- the manifest should
            # say so rather than record a seed that was never sent.
            seed=sent_seed,
            latency_s=time.monotonic() - t0,
            cost_units=round(self._estimate_cost(*req.size), 6),
        )

    def _post(self, body: dict):
        return requests.post(
            self._url(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            json=body, timeout=self.timeout_s)

    @staticmethod
    def _extract(resp) -> bytes:
        """Two shapes, decided by content type -- never by guessing."""
        ctype = (resp.headers.get("Content-Type") or "").lower()

        if ctype.startswith("image/"):
            # SDXL family: the body IS the image.
            return resp.content

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"unexpected non-JSON, non-image response ({ctype}): "
                f"{resp.content[:120]!r}") from exc

        if not payload.get("success", True):
            errs = payload.get("errors") or payload
            raise ProviderError(f"workers ai error: {str(errs)[:300]}")

        result = payload.get("result") or {}
        b64 = result.get("image") if isinstance(result, dict) else None
        if not b64:
            raise ProviderError(f"no image in response: {str(payload)[:300]}")
        try:
            return base64.b64decode(b64)
        except Exception as exc:                                   # noqa: BLE001
            raise ProviderError(f"image was not valid base64: {exc}") from exc


def list_image_models(timeout_s: float = 30.0) -> list[dict]:
    """Ask the account which text-to-image models it can actually run.

    Hardcoding a menu means it is wrong the week Cloudflare adds something --
    flux-2 and lucid-origin both landed after this adapter was written. Asking
    costs one request and can never be out of date.

    img2img and inpainting models are filtered out: they are text-to-image by
    task label but need a source image, and offering them in a dropdown that
    only ever sends a prompt is offering a guaranteed failure.
    """
    acc = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    tok = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not (acc and tok):
        return []
    try:
        r = requests.get(f"{API_BASE}/{acc}/ai/models/search",
                         headers={"Authorization": f"Bearer {tok}"},
                         params={"per_page": 200}, timeout=timeout_s)
        if r.status_code != 200:
            return []
        out = []
        for m in (r.json().get("result") or []):
            name = m.get("name") or ""
            task = ((m.get("task") or {}).get("name") or "")
            if task.lower() != "text-to-image":
                continue
            if "img2img" in name or "inpainting" in name:
                continue
            out.append({"name": name,
                        "label": name.split("/")[-1],
                        "vendor": name.split("/")[1] if name.count("/") > 1 else "",
                        "default": name == DEFAULT_MODEL})
        return sorted(out, key=lambda x: x["name"])
    except requests.RequestException:
        return []
