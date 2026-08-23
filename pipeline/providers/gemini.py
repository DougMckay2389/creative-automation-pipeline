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

    def __init__(self, rpm: float = 10.0, timeout_s: float = 120.0, **_ignored):
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ProviderError("gemini provider needs GEMINI_API_KEY")
        self.model = os.environ.get("GEMINI_IMAGE_MODEL", DEFAULT_MODEL)
        self.limiter = RateLimiter(rpm)
        self.timeout_s = timeout_s

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
