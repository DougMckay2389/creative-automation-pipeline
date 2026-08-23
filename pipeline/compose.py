"""Turning one master image into a finished creative at a given spec.

This is where the brief's "at least three aspect ratios" requirement is met,
and it is deliberately NOT met by generating three images.

The sequence, per variant:

    master (1600x1600)
        -> subject-aware crop to the target aspect ratio
        -> resize to exact delivery pixels
        -> scrim (a soft gradient) so text stays legible on any photo
        -> campaign message, wrapped and fitted
        -> logo, at brand scale, with clearspace respected
        -> measurements handed to the check engine

Everything returns measurements as well as pixels. That is the point: the
compliance checks in checks.py read what was actually rendered -- how much
area the message occupies, how big the type ended up, which colours dominate
-- rather than trusting the brief. A tool that validates the brief goes green
while the artwork is wrong.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

from .brief import Variant
from .localize import ResolvedFont, font_for


@dataclass
class Composition:
    """The rendered file plus everything measured about it."""
    path: str
    width: int
    height: int
    message: str
    font_family: str
    # measured, not assumed:
    message_px_height: float = 0.0        # cap height of the rendered type
    message_area: float = 0.0             # px^2 the text block occupies
    logo_box: tuple[int, int, int, int] | None = None
    logo_clearspace_ratio: float = 0.0
    dominant_hex: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Cropping
# --------------------------------------------------------------------------

def _subject_box(img: Image.Image) -> tuple[int, int, int, int]:
    """Estimate where the subject is, so a 9:16 crop does not behead it.

    Deliberately simple and explainable: downscale, measure per-column and
    per-row deviation from the image's own median tone, and take the densest
    band. A saliency model would be better and is the obvious upgrade -- but
    this is defensible, has no dependencies, and fails towards centre.
    """
    small = img.convert("L").resize((128, 128))
    px = small.load()
    col = [0.0] * 128
    row = [0.0] * 128
    vals = [px[x, y] for y in range(128) for x in range(128)]
    med = sorted(vals)[len(vals) // 2]
    for y in range(128):
        for x in range(128):
            d = abs(px[x, y] - med)
            col[x] += d
            row[y] += d

    def _band(sig: list[float]) -> tuple[int, int]:
        total = sum(sig) or 1.0
        target = total * 0.70            # the band holding 70% of the energy
        best = (0, len(sig) - 1)
        best_w = len(sig)
        acc, lo = 0.0, 0
        for hi in range(len(sig)):
            acc += sig[hi]
            while acc - sig[lo] >= target:
                acc -= sig[lo]
                lo += 1
            if acc >= target and (hi - lo) < best_w:
                best_w, best = hi - lo, (lo, hi)
        return best

    cx0, cx1 = _band(col)
    ry0, ry1 = _band(row)
    sx, sy = img.width / 128, img.height / 128
    return (int(cx0 * sx), int(ry0 * sy), int(cx1 * sx), int(ry1 * sy))


def crop_to_ratio(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Crop to the target aspect, keeping the subject centred, then resize."""
    tw, th = target_w / target_h, img.width / img.height
    sx0, sy0, sx1, sy1 = _subject_box(img)
    scx, scy = (sx0 + sx1) / 2, (sy0 + sy1) / 2

    if th > tw:                       # source is wider: crop left/right
        new_w = int(img.height * tw)
        x0 = int(min(max(scx - new_w / 2, 0), img.width - new_w))
        box = (x0, 0, x0 + new_w, img.height)
    else:                             # source is taller: crop top/bottom
        new_h = int(img.width / tw)
        # Bias upward: product shots put the subject above centre, and a
        # straight centre crop tends to cut the cap off a bottle.
        y0 = int(min(max(scy - new_h * 0.45, 0), img.height - new_h))
        box = (0, y0, img.width, y0 + new_h)

    return img.crop(box).resize((target_w, target_h), Image.LANCZOS)


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int,
          draw: ImageDraw.ImageDraw, cjk: bool) -> list[str]:
    """Wrap to a pixel width.

    CJK has no spaces, so word-wrapping on whitespace produces one enormous
    line that overflows the frame. Break per character instead when the market
    is CJK -- which is correct enough for a headline, and is exactly the class
    of bug that only shows up in the market you did not test.
    """
    units = list(text) if cjk else text.split()
    joiner = "" if cjk else " "
    lines, cur = [], ""
    for u in units:
        trial = (cur + joiner + u).strip(joiner) if cur else u
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = u
    if cur:
        lines.append(cur)
    return lines


def _fit_message(draw: ImageDraw.ImageDraw, text: str, rf: ResolvedFont,
                 max_w: int, max_h: int, start_px: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink the type until the wrapped block fits the reserved area.

    Fitting rather than truncating: a campaign message that is cut off is a
    defect, a campaign message that is slightly smaller is a design decision.
    """
    size = start_px
    while size > 12:
        font = ImageFont.truetype(rf.path, size)
        lines = _wrap(text, font, max_w, draw, rf.is_cjk)
        line_h = size * 1.28
        if len(lines) * line_h <= max_h and len(lines) <= 4:
            return font, lines
        size = int(size * 0.92)
    font = ImageFont.truetype(rf.path, 12)
    return font, _wrap(text, font, max_w, draw, rf.is_cjk)


# --------------------------------------------------------------------------
# Colour measurement
# --------------------------------------------------------------------------

def dominant_colors(img: Image.Image, n: int = 5) -> list[str]:
    """The n most common colours, as hex, measured on the finished creative."""
    small = img.convert("RGB").resize((160, 160))
    quant = small.quantize(colors=max(8, n * 3), method=Image.MEDIANCUT)
    pal = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], reverse=True)[:n]
    out = []
    for _count, idx in counts:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        out.append(f"#{r:02x}{g:02x}{b:02x}")
    return out


# --------------------------------------------------------------------------
# The composer
# --------------------------------------------------------------------------

class Composer:
    def __init__(self, brand: dict, logo_path: str | None = None):
        self.brand = brand
        self.typography = brand.get("typography") or {}
        self.logo_cfg = brand.get("logo") or {}
        self.logo_path = logo_path or self.logo_cfg.get("path")
        self._logo: Image.Image | None = None

    def _logo_image(self) -> Image.Image | None:
        if self._logo is None and self.logo_path and os.path.isfile(self.logo_path):
            self._logo = Image.open(self.logo_path).convert("RGBA")
        return self._logo

    def compose(self, master_path: str, variant: Variant, out_path: str,
                on_stage=None) -> Composition:
        """Compose one deliverable.

        `on_stage(name)` is an optional callback fired as each real stage
        completes. It exists so a UI can light up a node graph from actual
        pipeline progress rather than from a timer -- an animation that is not
        driven by the work it depicts is a lie, and somebody will ask.
        """
        tick = on_stage or (lambda _n: None)
        r = variant.ratio
        base = Image.open(master_path).convert("RGB")
        canvas = crop_to_ratio(base, r.width, r.height).convert("RGBA")
        tick("crop")
        draw = ImageDraw.Draw(canvas)
        warnings: list[str] = []

        short_edge = min(r.width, r.height)
        margin = int(short_edge * 0.07)

        # --- scrim ---------------------------------------------------------
        # Text over an arbitrary photograph is a contrast lottery. A gradient
        # scrim across the lower third makes legibility a property of the
        # template rather than of whatever the model happened to generate.
        scrim_h = int(r.height * 0.42)
        scrim = Image.new("RGBA", (r.width, scrim_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(scrim)
        for i in range(scrim_h):
            a = int(190 * (i / scrim_h) ** 1.6)
            sd.line([(0, i), (r.width, i)], fill=(10, 12, 16, a))
        canvas.alpha_composite(scrim, (0, r.height - scrim_h))
        tick("scrim")

        # --- message -------------------------------------------------------
        rf = font_for(variant.market.locale, variant.market.message, self.typography)
        if not rf.is_cjk and variant.market.language in ("ja", "zh", "ko"):
            warnings.append("CJK market rendered with a Latin face")

        text_max_w = r.width - 2 * margin
        text_max_h = int(scrim_h * 0.62)
        font, lines = _fit_message(
            draw, variant.market.message, rf, text_max_w, text_max_h,
            start_px=int(short_edge * 0.085))

        line_h = font.size * 1.28
        block_h = line_h * len(lines)
        y = r.height - margin - block_h
        widest = 0.0
        for ln in lines:
            draw.text((margin, y), ln, font=font, fill=(255, 255, 255, 255))
            widest = max(widest, draw.textlength(ln, font=font))
            y += line_h

        # Measure the type we actually drew, not the size we asked for.
        probe = font.getbbox("Hxg")
        cap_px = float(probe[3] - probe[1]) if probe else float(font.size)
        tick("message")

        # --- logo ----------------------------------------------------------
        logo_box = None
        clear_ratio = 0.0
        logo = self._logo_image()
        if logo is not None:
            lw = int(short_edge * float(self.logo_cfg.get("scale", 0.16)))
            lh = max(1, int(logo.height * lw / logo.width))
            lg = logo.resize((lw, lh), Image.LANCZOS)
            # White knockout: the scrim is dark, the logo art is dark ink.
            white = Image.new("RGBA", lg.size, (255, 255, 255, 255))
            white.putalpha(lg.split()[3])
            lx, ly = margin, margin
            canvas.alpha_composite(white, (lx, ly))
            logo_box = (lx, ly, lx + lw, ly + lh)
            # Clearspace actually available on the tightest side, in logo heights.
            clear_ratio = min(lx, ly, r.width - (lx + lw), r.height - (ly + lh)) / max(1, lh)

        tick("logo")

        out = canvas.convert("RGB")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out.save(out_path, quality=92, optimize=True)

        dom = dominant_colors(out)
        tick("measure")

        return Composition(
            path=out_path, width=r.width, height=r.height,
            message=variant.market.message, font_family=rf.family,
            message_px_height=cap_px,
            message_area=float(widest * block_h),
            logo_box=logo_box, logo_clearspace_ratio=clear_ratio,
            dominant_hex=dom,
            warnings=warnings,
        )
