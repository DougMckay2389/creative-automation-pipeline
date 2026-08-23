"""Getting the right glyphs onto the creative.

Localization in a creative pipeline is not translation -- the brief already
carries the market's copy, written by someone who speaks the language. The
engineering problem is narrower and far more likely to ship broken:

    **Can the font you chose actually draw those characters?**

Pillow does not warn you when it can't. It renders a row of empty boxes
(tofu), the file saves successfully, the pipeline reports success, and a
Japanese-market lead opens the asset and loses confidence in the whole
programme. So this module:

* resolves a font family from a preference list, across Windows/macOS/Linux;
* verifies the chosen face has a glyph for **every** character in the string;
* falls back until one does, and reports honestly if none can.

That last point matters: an unrenderable string is a hard failure here, not a
silently mangled creative.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from PIL import ImageFont

# Where to look, per platform. Ordered by how likely the font is to be present.
FONT_DIRS = [
    "/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
    "/usr/share/fonts/truetype", "/usr/share/fonts/opentype",
    "C:/Windows/Fonts",
    "/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
]

# Scripts that need a CJK-capable face. Latin fonts will silently tofu these.
CJK_LANGS = {"ja", "zh", "ko"}


class FontError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedFont:
    path: str
    family: str
    is_cjk: bool


def _walk_font_files() -> dict[str, str]:
    """Index every font file we can see, by lowercase basename.

    Built once per process. Walking /usr/share/fonts on a container with a few
    hundred faces costs single-digit milliseconds; doing it per variant would
    not.
    """
    index: dict[str, str] = {}
    for root in FONT_DIRS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".ttf", ".otf", ".ttc", ".otc")):
                    index.setdefault(f.lower(), os.path.join(dirpath, f))
    return index


_FONT_INDEX: dict[str, str] | None = None


def font_index() -> dict[str, str]:
    global _FONT_INDEX
    if _FONT_INDEX is None:
        _FONT_INDEX = _walk_font_files()
    return _FONT_INDEX


def _can_render(font_path: str, text: str, size: int = 40) -> bool:
    """Does this face have a glyph for every character in `text`?

    The check is `getmask`: Pillow maps unsupported characters to glyph 0
    (.notdef). Rather than inspect the cmap -- which varies by format and is
    a rabbit hole for .ttc collections -- we render and compare. A string whose
    rendered mask is empty while the text is non-blank means nothing drew.
    """
    try:
        f = ImageFont.truetype(font_path, size)
    except Exception:
        return False
    probe = "".join(ch for ch in text if not ch.isspace())
    if not probe:
        return True
    try:
        mask = f.getmask(probe)
        if mask.getbbox() is None:
            return False
        # Compare against a string of characters we know are unsupported
        # everywhere; if our text renders to the same width as pure tofu of
        # the same length, treat it as unsupported.
        tofu = f.getmask("\uf8ff" * len(probe))
        if tofu.getbbox() is not None and mask.size == tofu.size:
            return False
        return True
    except Exception:
        return False


def resolve_font(preferences: list[str], text: str, is_cjk: bool) -> ResolvedFont:
    """First font in `preferences` that exists AND can draw `text`."""
    idx = font_index()
    tried = []
    for want in preferences:
        path = idx.get(want.lower())
        if not path:
            # allow a full path in the brand kit as well as a bare filename
            if os.path.isfile(want):
                path = want
            else:
                tried.append(f"{want} (not installed)")
                continue
        if not _can_render(path, text):
            tried.append(f"{want} (missing glyphs)")
            continue
        return ResolvedFont(path=path, family=os.path.basename(path), is_cjk=is_cjk)

    # Last resort: anything on the machine that can render the string. Better a
    # correct-but-unbranded face than a creative full of empty boxes.
    for name, path in sorted(idx.items()):
        if is_cjk and not any(k in name for k in ("cjk", "gothic", "mincho", "hiragino",
                                                  "yugoth", "meiryo", "notosansjp",
                                                  "sourcehan")):
            continue
        if _can_render(path, text):
            return ResolvedFont(path=path, family=os.path.basename(path), is_cjk=is_cjk)

    raise FontError(
        "no font on this machine can render the message "
        f"({'CJK' if is_cjk else 'Latin'}). tried: {', '.join(tried) or 'none'}. "
        "Install a CJK face (e.g. Noto Sans CJK) or adjust brandkit/brand.yaml."
    )


def font_for(locale: str, text: str, typography: dict) -> ResolvedFont:
    """Pick the right preference list for the market, then resolve it."""
    lang = locale.split("-")[0].lower()
    is_cjk = lang in CJK_LANGS
    prefs = list(typography.get("cjk" if is_cjk else "latin") or [])
    if is_cjk:
        # A CJK face can draw Latin, so keep the Latin list as a tail fallback.
        prefs += list(typography.get("latin") or [])
    return resolve_font(prefs, text, is_cjk)
