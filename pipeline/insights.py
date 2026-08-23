"""Historic channel performance, and what it should change about a brief.

**Every number this module produces is synthetic.** Nothing here connects to
Meta, TikTok, X or Google. It is generated from a fixed seed so it is stable
between runs and identical on every machine, and the UI labels it as sample
data everywhere it appears. That is stated first, in the module that makes it,
because a fabricated metric that escapes into a conversation as a real one is
the worst thing a demo can do.

Why include it at all
---------------------
The exercise's fifth business goal is "gain actionable insights: track
effectiveness at scale and learn what content, creative and localization
drives the best business outcomes." A pipeline that only pushes creative out
answers four of five. The interesting question is the loop back: performance
data should decide what you make next, not just describe what you already
made.

So this module does one thing that matters and does it honestly: it turns
channel history into **specific, applicable changes to the brief** -- reorder
the aspect ratios so the best-performing placement is produced first, and
surface which creative treatments are actually working per market. A dashboard
that produces a feeling is decoration. One that produces a diff is a tool.

What a real integration would need
----------------------------------
Named so the gap is explicit rather than implied:

    Meta (Facebook/Instagram)  Graph API, `insights` edge on an ad account.
                               Long-lived token, app review for ads_read.
    TikTok                     Business API, /report/integrated/get/.
                               Sandbox first; advertiser id per market.
    X (Twitter)                Ads API. Paid tier; separate from the v2 API.
    Google                     Search Console API for query/impression data;
                               separate from Analytics for on-site behaviour.

All four are per-market, rate-limited, and return different shapes for the
same idea -- which is exactly the argument for an adapter layer like the one
`providers/` and `storage/` already use here. This module is deliberately
shaped like one of those adapters so a real source could take its place.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass, field

# Fixed, so two people looking at the same screen see the same numbers and a
# screenshot in a README stays true. Wall-clock randomness in a demo means the
# story changes between the rehearsal and the interview.
SEED = 20260823

SOURCES = [
    {"id": "meta",   "name": "Meta (FB/IG)",   "api": "Graph API · insights edge"},
    {"id": "tiktok", "name": "TikTok",         "api": "Business API · integrated report"},
    {"id": "x",      "name": "X (Twitter)",    "api": "Ads API · paid tier"},
    {"id": "gsc",    "name": "Search Console", "api": "Search Analytics API"},
]

# Which placement each aspect ratio serves. Ratios are the pipeline's unit of
# work, but nobody buys "9:16" -- they buy Stories and Reels, and that is the
# vocabulary performance data arrives in.
RATIO_PLACEMENT = {
    "9:16": "Stories / Reels / TikTok feed",
    "1:1":  "Feed / carousel",
    "4:5":  "Feed (tall) / PDP",
    "16:9": "In-stream video / display",
}

# The one editorial judgement in this file, and it is the honest kind: these
# are the treatments the pipeline can actually PRODUCE, so a recommendation
# can always be acted on. Recommending "add motion" would be advice this tool
# cannot take.
TREATMENTS = [
    ("wet stone with soft water droplets",      "texture-led"),
    ("volcanic black rock with warm rim light", "high-contrast"),
    ("polished white marble",                   "clean / premium"),
    ("brushed slate stone",                     "muted / editorial"),
    ("warm sand at golden hour",                "warm / lifestyle"),
]


@dataclass
class ChannelRow:
    source: str
    placement: str
    ratio: str
    impressions: int
    ctr: float                 # per cent
    cvr: float                 # per cent
    cpa_index: float           # 1.0 = campaign average; lower is better
    trend: list[int] = field(default_factory=list)   # 8 weeks, indexed to 100


def _rng(*parts: str) -> random.Random:
    """A generator keyed by what it is describing.

    Seeding per row rather than once means adding a market cannot shift the
    numbers of the markets beside it -- the en-US figures stay put whether or
    not de-DE exists. A single shared stream would reshuffle everything on any
    change, which for a demo means the story you rehearsed is gone.
    """
    key = f"{SEED}:" + ":".join(parts)
    return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def channel_rows(locale: str, ratios: list[str]) -> list[ChannelRow]:
    """Synthetic performance for one market, one row per source x ratio."""
    rows: list[ChannelRow] = []
    for src in SOURCES:
        for ratio in ratios:
            r = _rng(locale, src["id"], ratio)
            # Vertical wins on TikTok and Stories; square wins in feed; wide
            # under-performs on social and holds up on display. Baked in on
            # purpose -- synthetic data that contradicts what every media
            # planner knows would produce recommendations nobody believes,
            # and the point is to demonstrate the LOOP, not to invent a
            # surprising finding.
            base = {"9:16": 1.00, "1:1": 0.82, "4:5": 0.88, "16:9": 0.55}.get(ratio, 0.7)
            if src["id"] == "tiktok":
                base *= 1.25 if ratio == "9:16" else 0.6
            if src["id"] == "gsc":
                base *= 0.9 if ratio in ("1:1", "4:5") else 0.7
            if src["id"] == "x":
                base *= 0.85

            ctr = round(max(0.15, base * r.uniform(1.4, 2.4)), 2)
            cvr = round(max(0.05, base * r.uniform(0.5, 1.1)), 2)
            rows.append(ChannelRow(
                source=src["id"],
                placement=RATIO_PLACEMENT.get(ratio, ratio),
                ratio=ratio,
                impressions=int(r.uniform(40_000, 900_000) * base),
                ctr=ctr,
                cvr=cvr,
                cpa_index=round(1.0 / max(0.4, base) * r.uniform(0.82, 1.18), 2),
                trend=[int(100 * base * r.uniform(0.72, 1.3)) for _ in range(8)],
            ))
    return rows


def treatment_rows(locale: str) -> list[dict]:
    """How each creative treatment has performed in one market."""
    out = []
    for surface, label in TREATMENTS:
        r = _rng(locale, "treatment", surface)
        out.append({
            "surface": surface,
            "label": label,
            "ctr": round(r.uniform(0.6, 2.6), 2),
            "saves": round(r.uniform(0.2, 3.1), 2),
            "runs": r.randint(3, 40),
        })
    return sorted(out, key=lambda x: -x["ctr"])


def recommend(locale: str, ratios: list[str]) -> dict:
    """Turn the history into changes somebody can apply to the brief.

    Two, because two are actionable and a longer list is a wish:

      ratio_order  produce the best-performing placement first. Not a
                   filter -- dropping a placement because eight weeks of
                   synthetic data disliked it is exactly the over-fitting a
                   media team would (rightly) refuse.
      surface      the treatment with the strongest engagement in THIS market,
                   which becomes a one-click edit to the product's surface
                   prompt.

    Every recommendation carries the evidence that produced it, because a
    recommendation you cannot interrogate is one you should not take.
    """
    rows = channel_rows(locale, ratios)

    # Weighted by impressions: a 4% CTR on 900 views should not outrank a 2%
    # CTR on 900,000. Un-weighted averages are how dashboards end up
    # recommending whatever has the smallest sample.
    by_ratio: dict[str, list[float]] = {}
    weight: dict[str, int] = {}
    for row in rows:
        by_ratio.setdefault(row.ratio, []).append(row.ctr * row.impressions)
        weight[row.ratio] = weight.get(row.ratio, 0) + row.impressions
    scored = {rt: sum(v) / max(1, weight[rt]) for rt, v in by_ratio.items()}
    order = sorted(scored, key=lambda rt: -scored[rt])

    treatments = treatment_rows(locale)
    best = treatments[0]
    worst_ratio = order[-1] if order else ""

    return {
        "locale": locale,
        "ratio_order": order,
        "ratio_scores": {rt: round(scored[rt], 2) for rt in scored},
        "surface": best["surface"],
        "surface_label": best["label"],
        "why": [
            f"{RATIO_PLACEMENT.get(order[0], order[0])} carries the strongest "
            f"impression-weighted CTR in {locale} at {scored[order[0]]:.2f}%."
            if order else "",
            f"'{best['label']}' treatments lead on CTR ({best['ctr']}%) and "
            f"saves ({best['saves']}%) across {best['runs']} historic runs."
            ,
            f"{RATIO_PLACEMENT.get(worst_ratio, worst_ratio)} trails at "
            f"{scored.get(worst_ratio, 0):.2f}% -- still produced, just last "
            f"in the queue." if worst_ratio else "",
        ],
    }


def report(locale: str, ratios: list[str]) -> dict:
    """Everything the analytics tab needs for one market."""
    return {
        "locale": locale,
        "synthetic": True,
        "sources": SOURCES,
        "rows": [asdict(r) for r in channel_rows(locale, ratios)],
        "treatments": treatment_rows(locale),
        "recommendation": recommend(locale, ratios),
    }
