"""A deterministic offline image provider.

This is not a stub that returns a grey rectangle. It renders a plausible
product scene from the prompt, deterministically seeded, so that:

* the composition, cropping, text and compliance stages all get real pixels
  with real colour distributions to work on;
* two runs of the same brief produce byte-identical output, which is what
  makes the reuse cache and the audit story testable;
* a reviewer with no API key sees the whole pipeline behave exactly as it does
  in production.

Determinism comes from seeding Python's RNG with the variant seed. No wall
clock, no `random()` without a seed, no vendor call.
"""
from __future__ import annotations

import hashlib
import io
import random
import time

from PIL import Image, ImageDraw, ImageFilter

from .base import EditRequest, GenerationRequest, GenerationResult


def _palette_from_prompt(prompt: str) -> list[tuple[int, int, int]]:
    """Derive a stable colour scheme from the prompt text.

    Hashing the prompt means 'a terracotta lipstick' and 'a glass serum
    bottle' reliably look different from each other, and identical to
    themselves across runs, without storing anything.
    """
    h = hashlib.sha256(prompt.encode("utf-8")).digest()
    base = []
    for i in range(3):
        r, g, b = h[i * 3], h[i * 3 + 1], h[i * 3 + 2]
        # push towards muted, photographic tones rather than saturated noise
        base.append((120 + r // 3, 110 + g // 3, 100 + b // 3))
    return base


class MockProvider:
    name = "mock"
    model = "offline-deterministic-v1"
    # The offline provider implements edit() too, so the resurface path is
    # covered by tests that never touch the network. A mock that only
    # implements the easy half of the interface lets the hard half rot.
    supports_edit = True

    def __init__(self, **_ignored):
        pass

    def generate(self, req: GenerationRequest) -> GenerationResult:
        t0 = time.monotonic()
        w, h = req.size
        rnd = random.Random(req.seed)
        c1, c2, c3 = _palette_from_prompt(req.prompt)

        img = Image.new("RGB", (w, h), c1)
        d = ImageDraw.Draw(img)

        # ---背景: vertical gradient, so the crop stage has real tonal range
        for y in range(h):
            t = y / h
            d.line([(0, y), (w, y)], fill=(
                int(c1[0] * (1 - t) + c2[0] * t),
                int(c1[1] * (1 - t) + c2[1] * t),
                int(c1[2] * (1 - t) + c2[2] * t)))

        # --- a soft light source, off centre, seeded
        gx = int(w * (0.25 + 0.5 * rnd.random()))
        gy = int(h * (0.15 + 0.3 * rnd.random()))
        glow = Image.new("L", (w, h), 0)
        ImageDraw.Draw(glow).ellipse(
            (gx - w // 3, gy - h // 3, gx + w // 3, gy + h // 3), fill=90)
        img.paste(Image.new("RGB", (w, h), (255, 250, 242)),
                  (0, 0), glow.filter(ImageFilter.GaussianBlur(w // 8)))

        # --- cast shadow, then subject, so the subject sits ON the surface
        cx, cy = w // 2, int(h * 0.56)
        bw, bh = int(w * 0.22), int(h * 0.46)
        shadow = Image.new("L", (w, h), 0)
        ImageDraw.Draw(shadow).ellipse(
            (cx - bw, cy + bh // 2 - 30, cx + bw, cy + bh // 2 + 60), fill=120)
        img.paste(Image.new("RGB", (w, h), (60, 52, 46)),
                  (0, 0), shadow.filter(ImageFilter.GaussianBlur(w // 26)))

        d.rounded_rectangle(
            (cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2),
            radius=int(bw * 0.18), fill=c3)
        # highlight down one edge -- reads as a cylindrical object
        d.rounded_rectangle(
            (cx - bw // 2 + int(bw * 0.10), cy - bh // 2 + int(bh * 0.04),
             cx - bw // 2 + int(bw * 0.30), cy + bh // 2 - int(bh * 0.06)),
            radius=int(bw * 0.10),
            fill=tuple(min(255, v + 46) for v in c3))
        # cap
        d.rounded_rectangle(
            (cx - int(bw * 0.30), cy - bh // 2 - int(bh * 0.16),
             cx + int(bw * 0.30), cy - bh // 2 + int(bh * 0.02)),
            radius=int(bw * 0.10),
            fill=tuple(min(255, v + 24) for v in c2))

        img = img.filter(ImageFilter.GaussianBlur(0.6))

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return GenerationResult(
            png_bytes=buf.getvalue(),
            provider=self.name,
            model=self.model,
            prompt=req.prompt,
            seed=req.seed,
            latency_s=time.monotonic() - t0,
            cost_units=0.0,          # offline: free, and we say so rather than lying with None
        )

    def edit(self, req: EditRequest) -> GenerationResult:
        """Offline stand-in for reference-image editing.

        It cannot relight anything, but it models the ONE property the real
        feature is judged on and that a grey rectangle could not: **pixels from
        the reference survive into the output.** The reference is pasted at a
        known scale into the centre of a prompt-derived scene, so a test can
        read the centre of the result and assert it matches the reference
        rather than merely asserting "an image came back".

        That is the difference between a mock that tests the plumbing and one
        that tests the contract.
        """
        t0 = time.monotonic()
        w, h = req.size

        scene_png = self.generate(GenerationRequest(
            prompt=req.prompt, seed=req.seed, size=(w, h))).png_bytes
        scene = Image.open(io.BytesIO(scene_png)).convert("RGB")

        with Image.open(io.BytesIO(req.reference_png)) as ref_src:
            ref = ref_src.convert("RGB")
            target_h = max(1, int(h * self.EDIT_FILL))
            scale = target_h / ref.height
            ref = ref.resize((max(1, int(ref.width * scale)), target_h), Image.LANCZOS)

        scene.paste(ref, ((w - ref.width) // 2, (h - ref.height) // 2))

        buf = io.BytesIO()
        scene.save(buf, format="PNG", optimize=True)
        return GenerationResult(
            png_bytes=buf.getvalue(),
            provider=self.name,
            model=self.model,
            prompt=req.prompt,
            seed=req.seed,
            latency_s=time.monotonic() - t0,
            cost_units=0.0,
        )

    # Fraction of the frame height the pasted reference occupies. Exposed as a
    # class attribute so a test can compute exactly where it landed instead of
    # hard-coding a number that drifts out of sync with this file.
    EDIT_FILL = 0.6
