"""Generate the repo's placeholder brand logo and the one pre-existing input asset.

These are committed to the repo so a reviewer can clone and run with nothing
else. They are synthetic stand-ins for what would really be a brand logo and a
photographed product shot. Kept as a script rather than mystery binaries so it
is obvious where they came from.

    python tools/make_placeholders.py
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _font(size: int):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_logo(path: str) -> None:
    """A wordmark on transparency, so compositing over any surface works."""
    w, h = 720, 200
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = (16, 24, 32, 255)

    # mark: a filled circle with a bite taken out, suggesting a sunrise
    d.ellipse((14, 52, 110, 148), fill=ink)
    d.ellipse((44, 40, 140, 136), fill=(0, 0, 0, 0))

    f = _font(78)
    d.text((156, 58), "AURORA", font=f, fill=ink)
    img.save(path)
    print("wrote", path, img.size)


def make_product_shot(path: str) -> None:
    """A stand-in 'photographed' serum bottle on a soft surface.

    Deliberately 1600x1600 -- larger than any single delivery spec, because the
    pipeline crops down from one master rather than generating per ratio.
    """
    s = 1600
    img = Image.new("RGB", (s, s), (232, 223, 211))
    d = ImageDraw.Draw(img)

    # soft vignette / surface gradient
    for y in range(s):
        t = y / s
        d.line([(0, y), (s, y)], fill=(
            int(240 - 26 * t), int(233 - 26 * t), int(223 - 24 * t)))

    # cast shadow first, blurred separately so it reads as light not outline
    shadow = Image.new("L", (s, s), 0)
    ImageDraw.Draw(shadow).ellipse((480, 1120, 1160, 1260), fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(38))
    img.paste(Image.new("RGB", (s, s), (120, 108, 96)), (0, 0), shadow)

    # bottle body
    body = (612, 470, 1000, 1180)
    d.rounded_rectangle(body, radius=44, fill=(214, 224, 226))
    d.rounded_rectangle((640, 500, 800, 1150), radius=30, fill=(228, 236, 238))
    # cap
    d.rounded_rectangle((700, 300, 912, 476), radius=26, fill=(247, 244, 239))
    d.rounded_rectangle((742, 262, 870, 312), radius=18, fill=(236, 231, 224))
    # label
    d.rounded_rectangle((648, 700, 964, 940), radius=14, fill=(247, 244, 239))
    f1, f2 = _font(46), _font(26)
    d.text((676, 742), "HYDRA", font=f1, fill=(16, 24, 32))
    d.text((676, 800), "GLOW", font=f1, fill=(16, 24, 32))
    d.text((678, 872), "FACIAL SERUM  30ml", font=f2, fill=(90, 96, 102))

    img = img.filter(ImageFilter.GaussianBlur(0.4))
    img.save(path, quality=94)
    print("wrote", path, img.size)


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "brandkit"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "campaigns", "assets"), exist_ok=True)
    make_logo(os.path.join(HERE, "brandkit", "logo.png"))
    make_product_shot(
        os.path.join(HERE, "campaigns", "assets", "hydra-glow-serum.png"))
    print("\nNote: velvet-matte-lip.png is intentionally NOT created -- the "
          "pipeline generates that one, which is how the demo shows both the "
          "reuse path and the generate path in a single run.")
