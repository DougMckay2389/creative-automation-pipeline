"""Discovery with nothing to discover from.

This is the floor: it needs no credentials, no network and no browser, and it
is what a reviewer who cloned the repo two minutes ago gets. Everything it
returns is marked `synthetic=True`, carries no evidence URL, and is labelled
as sample data everywhere it is drawn.

Two things make it more useful than a placeholder:

* It is **seeded from the request**, so the same product in the same market on
  the same channel always produces the same look-alikes. A demo that shuffles
  its own evidence between runs is not a demo of anything.
* Its **shape is the real shape**. The per-channel biases below are the ones
  the live backends will surface too -- TikTok skewing short vertical video
  with a spoken hook, YouTube skewing 16:9 with a longer title, Instagram
  4:5 carousels. So the strategy code downstream is exercised against
  realistic inputs rather than against uniform noise, and swapping in Apify
  changes where the rows come from without changing what they are.
"""
from __future__ import annotations

import hashlib
import random

from .base import CHANNEL_RATIO, Discovery, DiscoveryRequest, Lookalike

SEED = 20260824

# Per channel: the formats that actually get distribution, how views are
# distributed, and how a hook tends to be written there.
CHANNEL_SHAPE = {
    "tiktok": {
        "formats": [("video", 88), ("image", 12)],
        "views": (40_000, 4_200_000),
        "er": (3.1, 11.5),
        "hooks": ["POV: you finally {benefit}", "no one talks about {problem}",
                  "3 days of {routine}", "I tried {category} for 30 days",
                  "stop doing {mistake}"],
    },
    "instagram": {
        "formats": [("image", 42), ("carousel", 30), ("video", 28)],
        "views": (18_000, 900_000),
        "er": (1.8, 6.4),
        "hooks": ["the {benefit} everyone asked about", "{routine}, simplified",
                  "save this for your next {category} run",
                  "before / after: {benefit}"],
    },
    "youtube": {
        "formats": [("video", 96), ("image", 4)],
        "views": (25_000, 2_600_000),
        "er": (1.2, 4.8),
        "hooks": ["Why {category} is changing in {year}",
                  "I tested every {category} so you don't have to",
                  "The truth about {problem}", "{benefit} in 60 seconds"],
    },
    "facebook": {
        "formats": [("image", 55), ("video", 33), ("carousel", 12)],
        "views": (9_000, 480_000),
        "er": (0.9, 3.6),
        "hooks": ["{benefit}, without {mistake}", "Rated best for {audience}",
                  "Now shipping to {region}", "{routine} that actually lasts"],
    },
}

# Handles are built rather than listed so they cannot accidentally collide
# with a real account, which a hard-coded list eventually does.
PREFIX = ["lumen", "verda", "aurelia", "noct", "kaya", "sable", "orin",
          "mira", "halden", "cerise", "vesper", "indra"]
SUFFIX = ["labs", "skin", "beauty", "co", "studio", "ritual", "atelier", "care"]

SURFACE_CUES = [
    "wet stone", "volcanic rock", "brushed marble", "frosted glass",
    "rippling water", "matte sand", "polished concrete", "linen drape",
    "moss and dew", "black granite", "sun-bleached wood", "cracked clay",
]
PALETTES = [
    ["#0f1c2e", "#c9d6e3", "#e8b04b"], ["#1d1a17", "#d8cfc4", "#b45f3f"],
    ["#132420", "#cfe3d8", "#6fae8f"], ["#241c2b", "#e3d9ec", "#9a6fd1"],
    ["#2b1f1c", "#efe2d6", "#d0714a"],
]
PROBLEMS = ["dull skin", "midday shine", "flaky patches", "tight skin",
            "uneven tone", "dry cuffs"]
BENEFITS = ["glass skin", "all-day hydration", "a calmer barrier",
            "colour that lasts", "visible bounce"]
MISTAKES = ["over-exfoliating", "skipping SPF", "layering wrong",
            "washing with hot water"]
ROUTINES = ["a 3-step routine", "the 60-second routine", "an evening routine",
            "a barrier-repair week"]


def _rng(*parts: str) -> random.Random:
    """Deterministic per row.

    Hashed rather than fed to `random.seed(tuple)` because the tuple form
    depends on Python's string hashing, which is salted per process -- the
    same bug this repo already fixed once in seed generation.
    """
    key = "|".join(str(p) for p in parts)
    h = hashlib.sha256(f"{SEED}|{key}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def _weighted(r: random.Random, pairs: list[tuple[str, int]]) -> str:
    return r.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


class SyntheticDiscovery:
    """Always available. Always labelled."""

    name = "synthetic"
    synthetic = True

    def find(self, req: DiscoveryRequest) -> list[Lookalike]:
        shape = CHANNEL_SHAPE.get(req.channel, CHANNEL_SHAPE["instagram"])
        out: list[Lookalike] = []

        for i in range(req.limit):
            r = _rng(req.product_id, req.locale, req.channel, i)
            fmt = _weighted(r, shape["formats"])
            lo, hi = shape["views"]
            # Long tail, not uniform: a handful of posts carry most of the
            # reach on every one of these networks, and a flat distribution
            # would teach the strategy layer the wrong lesson about which
            # signals are worth following.
            views = int(lo * (hi / lo) ** (r.random() ** 2.2))
            hook = r.choice(shape["hooks"]).format(
                benefit=r.choice(BENEFITS), problem=r.choice(PROBLEMS),
                mistake=r.choice(MISTAKES), routine=r.choice(ROUTINES),
                category=req.category, audience=req.audience.split(",")[0],
                region=req.region, year="2026")
            brand = f"{r.choice(PREFIX).title()} {r.choice(SUFFIX).title()}"

            out.append(Lookalike(
                channel=req.channel,
                handle="@" + brand.lower().replace(" ", ""),
                brand=brand,
                title=hook,
                product_category=req.category,
                posted_days_ago=r.randint(1, 28),
                views=views,
                engagement_rate=round(r.uniform(*shape["er"]), 2),
                # Velocity is against the account's OWN median, so a small
                # account with a breakout post outranks a large account
                # posting normally. That is the signal worth copying.
                velocity=round(r.uniform(0.6, 6.4), 2),
                ratio=CHANNEL_RATIO.get(req.channel, "1:1"),
                format=fmt,
                hook=hook,
                surface_cues=r.sample(SURFACE_CUES, 2),
                palette=r.choice(PALETTES),
                evidence_url="",
                synthetic=True,
            ))

        out.sort(key=lambda l: l.velocity * l.engagement_rate, reverse=True)
        return out


assert isinstance(SyntheticDiscovery(), Discovery)
