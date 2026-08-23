"""Adobe Firefly Services adapter.

Written against the real v3 async API, not a guess. It is unused in the
offline demo (no enterprise credentials), but it is here because "swap the
provider" is only a credible claim if the swap target actually exists.

Set these and pass `--provider firefly`:

    FIREFLY_CLIENT_ID       # Adobe Developer Console -> Firefly Services
    FIREFLY_CLIENT_SECRET
    FIREFLY_CUSTOM_MODEL_ID # optional: urn:aaid:sc:... for a brand model

Things that are easy to get wrong here, encoded as behaviour rather than as
comments you have to remember:

* **The synchronous generate endpoint was removed.** Async only. Several
  published tutorials still show the old one.
* **The 202 response carries a per-tenant shard host** (e.g.
  `firefly-epo852211.adobe.io`), not `firefly-api.adobe.io`. Follow the
  returned `statusUrl` verbatim; never rebuild it from the base URL.
* **`x-api-key` is the client id**, not a separate key.
* **Cancel is a PUT.** Not POST, not DELETE.
* **`customModelId` is v3 only**, and requires `x-model-version:
  image4_custom`. If a brand model matters more than the newest base model,
  you stay on v3 -- that is a real architectural trade, not a preference.
* **The IMS token lasts ~24h.** Minting one per call is a self-inflicted rate
  limit, so it is cached.
"""
from __future__ import annotations

import base64
import os
import time

import requests

from .base import GenerationRequest, GenerationResult, ProviderError, RateLimiter

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FIREFLY_BASE = "https://firefly-api.adobe.io"
SCOPES = "openid,AdobeID,session,additional_info,read_organizations,firefly_api,ff_apis"


class FireflyProvider:
    name = "firefly"
    model = "firefly-image-v3"

    def __init__(self, rpm: float = 4.0, timeout_s: float = 120.0, **_ignored):
        self.client_id = os.environ.get("FIREFLY_CLIENT_ID", "")
        self.client_secret = os.environ.get("FIREFLY_CLIENT_SECRET", "")
        self.custom_model_id = os.environ.get("FIREFLY_CUSTOM_MODEL_ID", "")
        if not (self.client_id and self.client_secret):
            raise ProviderError(
                "firefly provider needs FIREFLY_CLIENT_ID and FIREFLY_CLIENT_SECRET")
        # Documented default entitlement is 4 requests/minute. Queue, don't spray.
        self.limiter = RateLimiter(rpm)
        self.timeout_s = timeout_s
        self._token = ""
        self._token_expires_at = 0.0
        if self.custom_model_id:
            self.model = "firefly-image4-custom"

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        """Server-to-server OAuth, cached until shortly before expiry."""
        if self._token and time.time() < self._token_expires_at - 300:
            return self._token
        r = requests.post(IMS_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": SCOPES,
        }, timeout=30)
        if r.status_code != 200:
            raise ProviderError(f"IMS token failed: {r.status_code} {r.text[:300]}")
        payload = r.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 86400))
        return self._token

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._access_token()}",
            "x-api-key": self.client_id,          # the client id IS the api key
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.custom_model_id:
            h["x-model-version"] = "image4_custom"
        return h

    # -- generation ---------------------------------------------------------

    def generate(self, req: GenerationRequest) -> GenerationResult:
        t0 = time.monotonic()
        self.limiter.acquire()

        body: dict = {
            "prompt": req.prompt,
            "numVariations": 1,
            "seeds": [req.seed],
            "size": {"width": req.size[0], "height": req.size[1]},
        }
        if req.negative:
            body["negativePrompt"] = req.negative
        if self.custom_model_id:
            body["customModelId"] = self.custom_model_id

        r = requests.post(f"{FIREFLY_BASE}/v3/images/generate-async",
                          headers=self._headers(), json=body, timeout=60)
        if r.status_code not in (200, 202):
            raise ProviderError(f"generate-async failed: {r.status_code} {r.text[:300]}")

        job = r.json()
        # v3 returns {jobId, statusUrl, cancelUrl}; v4 returns {links:{result,cancel}}.
        status_url = job.get("statusUrl") or (job.get("links") or {}).get("result")
        if not status_url:
            raise ProviderError(f"no status url in response: {job}")

        result = self._poll(status_url)
        png = self._download_first_image(result)

        return GenerationResult(
            png_bytes=png,
            provider=self.name,
            model=self.model,
            prompt=req.prompt,
            seed=req.seed,
            latency_s=time.monotonic() - t0,
            cost_units=None,          # Firefly does not return per-call credits
        )

    def _poll(self, status_url: str) -> dict:
        """Poll the shard host we were handed, until done or timeout."""
        deadline = time.monotonic() + self.timeout_s
        delay = 1.0
        while time.monotonic() < deadline:
            r = requests.get(status_url, headers=self._headers(), timeout=30)
            if r.status_code != 200:
                raise ProviderError(f"status poll failed: {r.status_code} {r.text[:200]}")
            payload = r.json()
            state = (payload.get("status") or "").lower()
            if state in ("succeeded", "success", "done", "complete", "completed"):
                return payload
            if state in ("failed", "error", "cancelled", "canceled"):
                raise ProviderError(f"job {state}: {str(payload)[:300]}")
            time.sleep(delay)
            delay = min(delay * 1.5, 8.0)          # gentle backoff, capped
        raise ProviderError(f"job did not finish within {self.timeout_s}s")

    @staticmethod
    def _download_first_image(payload: dict) -> bytes:
        """Firefly returns presigned URLs (or, on some paths, base64)."""
        outputs = (payload.get("result") or payload).get("outputs") or []
        if not outputs:
            raise ProviderError(f"no outputs in result: {str(payload)[:300]}")
        first = outputs[0]
        img = first.get("image") or {}
        if img.get("url"):
            r = requests.get(img["url"], timeout=60)
            if r.status_code != 200:
                raise ProviderError(f"image download failed: {r.status_code}")
            return r.content
        if first.get("base64"):
            return base64.b64decode(first["base64"])
        raise ProviderError(f"output had neither url nor base64: {str(first)[:200]}")
