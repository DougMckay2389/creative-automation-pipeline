"""Google Gemini image adapter ("nano banana" family).

The live path used when GEMINI_API_KEY is set. It exists to prove the point
the mock provider can only assert: that the pipeline really is
provider-agnostic, and that swapping vendors touches this file and nothing
else.

    export GEMINI_API_KEY=...
    python run.py run campaigns/aurora-spring.yaml --provider gemini
"""
from __future__ import annotations

import base64
import os
import time

import requests

from .base import GenerationRequest, GenerationResult, ProviderError, RateLimiter

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash-image"


class GeminiProvider:
    name = "gemini"

    def __init__(self, rpm: float = 10.0, timeout_s: float = 120.0,
                 model: str | None = None, **_ignored):
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ProviderError("gemini provider needs GEMINI_API_KEY")
        # Argument, then environment, then default -- so the app's dropdown can
        # change the model per run without editing .env and restarting.
        self.model = model or os.environ.get("GEMINI_IMAGE_MODEL") or DEFAULT_MODEL
        self.limiter = RateLimiter(rpm)
        self.timeout_s = timeout_s

    # A seed is accepted and recorded, but Gemini's image endpoint takes no
    # seed parameter -- so unlike the Cloudflare models, two runs of the same
    # brief here are NOT byte-identical. Recorded honestly rather than implied.
    honours_seed = False

    def generate(self, req: GenerationRequest) -> GenerationResult:
        t0 = time.monotonic()
        self.limiter.acquire()

        url = f"{API_BASE}/models/{self.model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": req.prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        r = requests.post(url, params={"key": self.api_key}, json=body,
                          timeout=self.timeout_s)
        if r.status_code != 200:
            raise ProviderError(f"gemini generate failed: {r.status_code} {r.text[:300]}")

        png = self._extract_image(r.json())
        return GenerationResult(
            png_bytes=png,
            provider=self.name,
            model=self.model,
            prompt=req.prompt,
            seed=req.seed,
            latency_s=time.monotonic() - t0,
            cost_units=None,
        )

    @staticmethod
    def _extract_image(payload: dict) -> bytes:
        """Walk the candidate parts for inline image data.

        Written defensively on purpose: response shapes for image modalities
        move between API revisions, and a KeyError three layers deep is a
        miserable thing to debug at 4rpm.
        """
        for cand in payload.get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise ProviderError(f"no inline image in response: {str(payload)[:300]}")


def list_image_models(timeout_s: float = 20.0) -> list[dict]:
    """Google's image-capable models, asked of the account.

    Same argument as the Cloudflare adapter: a menu baked into the source is
    wrong the week the vendor ships something. Filtered to models that can
    actually RETURN an image -- most of the catalogue is text-only, and
    offering those would be offering a guaranteed failure.
    """
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return []
    try:
        r = requests.get(f"{API_BASE}/models", params={"key": key, "pageSize": 200},
                         timeout=timeout_s)
        if r.status_code != 200:
            return []
        out = []
        for m in (r.json().get("models") or []):
            name = (m.get("name") or "").split("/")[-1]
            if "image" not in name:
                continue
            if not any("generateContent" in a
                       for a in (m.get("supportedGenerationMethods") or [])):
                continue
            label = name
            if name.startswith("gemini-2.5-flash-image"):
                label = f"{name}  (nano banana)"
            out.append({"name": name, "label": label, "vendor": "google",
                        "default": name == DEFAULT_MODEL})
        return sorted(out, key=lambda x: x["name"])
    except requests.RequestException:
        return []
