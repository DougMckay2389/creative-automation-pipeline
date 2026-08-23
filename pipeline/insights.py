"""Channel performance, external trend signals, and what they should change.

**Every number in this module is synthetic.** Nothing connects to Google
Analytics, Meta, TikTok or YouTube; nothing is scraped. It is generated from a
fixed seed, so it is stable between runs and identical on every machine, and
the UI labels it as sample data on every surface it appears. That is stated
first, in the module that makes it, because a fabricated metric that escapes
into a conversation as a real one is the worst thing a demo can do.

Why this exists
---------------
The exercise's fifth business goal is "gain actionable insights: track
effectiveness at scale and learn what content, creative and localization
drives the best business outcomes." A pipeline that only pushes creative out
answers four of five. The fifth is a loop: what performed should decide what
you make next.

So this module is shaped to close that loop rather than to decorate it. Two
sources feed one output:

    INTERNAL   how our own posts performed, per channel, per day
    EXTERNAL   what is moving in each market right now, independent of us

    ->  a suggested SURFACE PROMPT, with the evidence that produced it,
        which can be rendered as a single sample and adopted into the brief

The last step matters most. A dashboard that produces a feeling is decoration.
One that produces a prompt you can run and then accept is part of the pipeline.

What a real integration would need
----------------------------------
Named so the gap is explicit rather than implied:

    Google Analytics  GA4 Data API. Property id per market, OAuth service
                      account, `runReport` with date + dimension breakdowns.
    Facebook / IG     Graph API `insights` edge on the ad account and on each
                      IG media object. Long-lived token, app review for
                      ads_read + instagram_basic.
    TikTok            Business API, /report/integrated/get/. Sandbox first,
                      advertiser id per market.
    YouTube           YouTube Analytics API (separate from Data API v3);
                      channel-level OAuth.
    External trends   No clean API exists for "what is trending here". In
                      practice this is paid social listening (Brandwatch,
                      Talkwalker), platform trend endpoints where they exist,
                      and scraping -- which carries real terms-of-service and
                      rate-limit exposure and is better bought than built.

All of them are per-market, rate-limited, and return a different shape for the
same idea -- which is exactly the argument for an adapter layer like the one
`providers/` and `storage/` already use. This module is shaped like one of
those adapters so a real source could take its place without the UI noticing.
"""
from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import os
import random
from dataclasses import asdict, dataclass

# Fixed, so two people looking at the same screen see the same numbers and a
# screenshot in a README stays true. Wall-clock randomness in a demo means the
# story changes between the rehearsal and the interview.
SEED = 20260823

# The channels the app reports on. `metric` is the engagement number each
# channel's own reporting leads with -- naming them per channel rather than
# flattening everything to "engagement" is the difference between a report a
# marketer recognises and one they have to translate.
CHANNELS = [
    {"id": "ga",       "name": "Google Analytics", "short": "GA",
     "metric": "Sessions", "api": "GA4 Data API · runReport"},
    {"id": "facebook", "name": "Facebook / IG",    "short": "Meta",
     "metric": "Reach",    "api": "Graph API · insights edge"},
    {"id": "tiktok",   "name": "TikTok",           "short": "TikTok",
     "metric": "Views",    "api": "Business API · integrated report"},
    {"id": "youtube",  "name": "YouTube",          "short": "YouTube",
     "metric": "Views",    "api": "YouTube Analytics API"},
]
CHANNEL_IDS = [c["id"] for c in CHANNELS]

# Which placement each aspect ratio serves. Ratios are the pipeline's unit of
# work, but nobody buys "9:16" -- they buy Stories and Reels, and that is the
# vocabulary performance data arrives in.
RATIO_PLACEMENT = {
    "9:16": "Stories / Reels / Shorts",
    "1:1":  "Feed / carousel",
    "4:5":  "Feed (tall) / PDP",
    "16:9": "In-stream / display",
}

# Treatments the pipeline can actually PRODUCE, so a suggestion can always be
# acted on. Recommending "add motion" would be advice this tool cannot take.
TREATMENTS = [
    ("wet stone with soft water droplets",              "texture-led"),
    ("volcanic black rock with warm rim light",         "high-contrast"),
    ("polished white marble",                           "clean / premium"),
    ("brushed slate stone",                             "muted / editorial"),
    ("warm sand at golden hour",                        "warm / lifestyle"),
    ("frosted glass with cool blue backlight",          "cold / clinical"),
    ("moss and damp forest floor",                      "natural / botanical"),
]

# How each channel's audience skews, in the vocabulary a planner uses. Held
# per channel rather than per market because platform demographics are a
# property of the platform first and the country second.
CHANNEL_AUDIENCE = {
    "ga":       [("25-34", 31), ("35-44", 27), ("45-54", 19), ("18-24", 14), ("55+", 9)],
    "facebook": [("35-44", 30), ("45-54", 24), ("25-34", 23), ("55+", 15), ("18-24", 8)],
    "tiktok":   [("18-24", 41), ("25-34", 33), ("35-44", 15), ("45-54", 8), ("55+", 3)],
    "youtube":  [("25-34", 29), ("18-24", 26), ("35-44", 22), ("45-54", 14), ("55+", 9)],
}

# Cities per market, so "where" is concrete. A market-level number tells you
# nothing you can act on; a city list tells a media planner where to weight.
MARKET_CITIES = {
    "en-US": ["New York", "Los Angeles", "Chicago", "Miami", "Austin"],
    "ja-JP": ["Tokyo", "Osaka", "Yokohama", "Nagoya", "Fukuoka"],
    "de-DE": ["Berlin", "Munich", "Hamburg", "Cologne", "Frankfurt"],
    "en-GB": ["London", "Manchester", "Birmingham", "Glasgow", "Leeds"],
    "fr-FR": ["Paris", "Lyon", "Marseille", "Toulouse", "Bordeaux"],
}

# Trend vocabulary, per market. Deliberately in each market's own language
# where that is how the trend would actually surface -- a German trend report
# that renders everything in English has already lost the thing being reported.
MARKET_TRENDS = {
    "en-US": [("#glassskin", "beauty"), ("cold plunge", "wellness"),
              ("desert minimal", "aesthetic"), ("#skintok routines", "beauty"),
              ("volcanic minerals", "ingredient")],
    "ja-JP": [("ガラス肌", "beauty"), ("ミニマル", "aesthetic"),
              ("温泉ミネラル", "ingredient"), ("#スキンケア", "beauty"),
              ("静けさ", "aesthetic")],
    "de-DE": [("Glass Skin", "beauty"), ("Naturkosmetik", "ingredient"),
              ("Minimalismus", "aesthetic"), ("Thermalwasser", "ingredient"),
              ("#Hautpflege", "beauty")],
    "en-GB": [("#glassskin", "beauty"), ("cold water swimming", "wellness"),
              ("coastal minimal", "aesthetic"), ("mineral skincare", "ingredient"),
              ("#skincaretok", "beauty")],
    "fr-FR": [("peau de verre", "beauty"), ("minimalisme", "aesthetic"),
              ("eau thermale", "ingredient"), ("#soindelapeau", "beauty"),
              ("naturalité", "ingredient")],
}

# Which treatment a trend argues for. This is the join that turns "cold plunge
# is trending" into something the pipeline can render, and it is the only
# genuinely editorial mapping in the file -- so it is a table you can read and
# disagree with, rather than logic buried in a function.
TREND_TREATMENT = {
    "wellness":   "wet stone with soft water droplets",
    "ingredient": "volcanic black rock with warm rim light",
    "aesthetic":  "polished white marble",
    "beauty":     "frosted glass with cool blue backlight",
}

RATIO_BASE = {"9:16": 1.0, "1:1": 0.82, "4:5": 0.88, "16:9": 0.55}

# The velocity band trends are drawn from, and the band the virality score is
# normalised against. Declared together because they must agree: score against
# a narrower range than you generate and the meter clamps, which is exactly
# how a gauge ends up reading 100 for everything.
VELOCITY_MIN, VELOCITY_MAX = -0.3, 2.6


def _rng(*parts) -> random.Random:
    """A generator keyed by what it is describing.

    Seeding per row rather than once means adding a market cannot shift the
    numbers of the markets beside it -- en-US stays put whether or not de-DE
    exists. A single shared stream would reshuffle everything on any change,
    which for a demo means the story you rehearsed is gone.
    """
    key = f"{SEED}:" + ":".join(str(p) for p in parts)
    return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def _channel_bias(channel: str, ratio: str) -> float:
    """Platform reality, stated as a table rather than buried in noise.

    Vertical wins on TikTok; wide holds up on YouTube and dies on Meta feed.
    Baked in deliberately: synthetic data that contradicts what every media
    planner already knows produces recommendations nobody believes, and the
    point is to demonstrate the loop, not to invent a surprising finding.
    """
    base = RATIO_BASE.get(ratio, 0.7)
    if channel == "tiktok":
        base *= 1.30 if ratio == "9:16" else 0.55
    elif channel == "youtube":
        base *= 1.15 if ratio in ("9:16", "16:9") else 0.60
    elif channel == "facebook":
        base *= 1.10 if ratio in ("1:1", "4:5") else 0.80
    elif channel == "ga":
        base *= 0.95 if ratio in ("1:1", "4:5") else 0.75
    return base


# --------------------------------------------------------------------------
# Thumbnails for "previous posts"
# --------------------------------------------------------------------------

def _thumb_pool(root: str) -> list[str]:
    """Real images on disk, to stand in for previous posts.

    Reusing creatives this pipeline has already produced beats generating
    placeholder swatches: the calendar then shows the kind of thing the tool
    actually makes, and a reviewer who has run it once recognises their own
    output. Falls back to the input assets on a cold clone.
    """
    pool: list[str] = []
    for run in sorted(glob.glob(os.path.join(root, "output", "*", "*")),
                      reverse=True)[:4]:
        for p in sorted(glob.glob(os.path.join(run, "*", "*", "*.jpg")))[:24]:
            pool.append(os.path.relpath(p, root).replace("\\", "/"))
    if not pool:
        for p in sorted(glob.glob(os.path.join(root, "campaigns", "assets", "*"))):
            if os.path.splitext(p)[1].lower() in (".png", ".jpg", ".jpeg", ".webp"):
                pool.append(os.path.relpath(p, root).replace("\\", "/"))
    return pool


# --------------------------------------------------------------------------
# Internal: our own posts, per channel, per day
# --------------------------------------------------------------------------

@dataclass
class Post:
    id: str
    date: str
    channel: str
    ratio: str
    placement: str
    thumb: str
    caption: str
    impressions: int
    engagement_rate: float     # per cent
    ctr: float                 # per cent
    saves: int
    comments: int
    shares: int
    watch_through: float       # per cent; 0 for still placements
    treatment: str


def calendar(locale: str, channel: str, root: str, days: int = 28,
             end: str = "") -> dict:
    """One channel's posting history for a market, laid out by date.

    A calendar rather than a table because posting is periodic and the
    interesting questions are periodic too -- did the weekend posts do better,
    did that run of Stories in week three actually work. A sortable table
    answers "which post won"; a calendar answers "what were we doing".
    """
    pool = _thumb_pool(root)
    ratios = list(RATIO_PLACEMENT)
    end_d = (_dt.date.fromisoformat(end) if end else _dt.date(2026, 8, 23))
    start = end_d - _dt.timedelta(days=days - 1)

    posts: list[Post] = []
    for i in range(days):
        d = start + _dt.timedelta(days=i)
        r = _rng(locale, channel, d.isoformat())
        # Real calendars are sparse. A grid where every cell is full teaches
        # the wrong thing about cadence.
        for k in range(r.choices([0, 1, 2], weights=[38, 47, 15])[0]):
            rr = _rng(locale, channel, d.isoformat(), k)
            ratio = rr.choice(ratios)
            surface, label = rr.choice(TREATMENTS)
            base = _channel_bias(channel, ratio)
            posts.append(Post(
                id=f"{channel}-{d.isoformat()}-{k}",
                date=d.isoformat(), channel=channel, ratio=ratio,
                placement=RATIO_PLACEMENT[ratio],
                thumb=pool[rr.randrange(len(pool))] if pool else "",
                caption=f"{label} · {surface}",
                impressions=int(rr.uniform(8_000, 240_000) * base),
                engagement_rate=round(max(0.2, base * rr.uniform(1.1, 4.2)), 2),
                ctr=round(max(0.1, base * rr.uniform(0.7, 2.6)), 2),
                saves=int(rr.uniform(20, 3_400) * base),
                comments=int(rr.uniform(3, 480) * base),
                shares=int(rr.uniform(2, 900) * base),
                watch_through=(round(rr.uniform(18, 72), 1)
                               if ratio in ("9:16", "16:9") else 0.0),
                treatment=surface,
            ))

    tot = sum(p.impressions for p in posts) or 1
    best = max(posts, key=lambda p: p.engagement_rate * p.impressions) if posts else None
    return {
        "locale": locale, "channel": channel,
        "start": start.isoformat(), "end": end_d.isoformat(), "days": days,
        "posts": [asdict(p) for p in posts],
        "best_id": best.id if best else "",
        "totals": {
            "posts": len(posts),
            "impressions": tot,
            # Impression-weighted, not a plain mean. A 6% rate on 900 views
            # must not outrank 2% on 900,000, and un-weighted averages are how
            # dashboards end up celebrating the smallest sample they have.
            "engagement_rate": round(
                sum(p.engagement_rate * p.impressions for p in posts) / tot, 2),
            "ctr": round(sum(p.ctr * p.impressions for p in posts) / tot, 2),
            "saves": sum(p.saves for p in posts),
        },
    }


# --------------------------------------------------------------------------
# External: what is moving in the market, independent of us
# --------------------------------------------------------------------------

def external(locale: str, channel: str) -> dict:
    """Trend, audience and location signal for one market and channel.

    Separated from `calendar()` on purpose. Internal metrics say how OUR posts
    did; external says what the market is doing whether we post or not.
    Conflating them is how teams optimise into a niche -- your own history can
    only ever rank the things you already tried.
    """
    trends = []
    for term, kind in MARKET_TRENDS.get(locale, MARKET_TRENDS["en-US"]):
        r = _rng(locale, channel, "trend", term)
        # Virality is VELOCITY, not volume. A term with a million mentions
        # that is flat is not a trend; one with ten thousand doubling weekly
        # is. Scored 0-100 so it compares across markets of very different
        # sizes -- raw mention counts never can.
        velocity = r.uniform(VELOCITY_MIN, VELOCITY_MAX)
        # Normalised across the velocity range rather than scaled by a
        # constant. The first version was `38 + velocity*26`, which pushed
        # everything above ~2.4x past 100 and clamped -- four of five terms
        # scored 98-100 and the meter stopped distinguishing anything. A
        # gauge pinned at full is not a gauge.
        t = (velocity - VELOCITY_MIN) / (VELOCITY_MAX - VELOCITY_MIN)
        virality = max(1, min(99, int(8 + t * 84 + r.uniform(-6, 6))))
        trends.append({
            "term": term, "kind": kind,
            "volume": int(r.uniform(4_000, 900_000)),
            "velocity": round(velocity, 2),
            "virality": virality,
            "spark": [max(2, int(50 + velocity * 9 * (i / 7) + r.uniform(-11, 11)))
                      for i in range(8)],
            "treatment": TREND_TREATMENT.get(kind, TREATMENTS[0][0]),
        })
    trends.sort(key=lambda t: -t["virality"])

    cities = MARKET_CITIES.get(locale, MARKET_CITIES["en-US"])
    cr = _rng(locale, channel, "cities")
    weights = sorted((cr.uniform(0.06, 0.34) for _ in cities), reverse=True)
    total = sum(weights) or 1
    locations = [{"city": c, "share": round(w / total * 100, 1),
                  "index": round(cr.uniform(0.7, 1.9), 2)}
                 for c, w in zip(cities, weights)]

    ar = _rng(locale, channel, "audience")
    audience = [{"bucket": b, "share": max(1, int(s + ar.uniform(-3, 3)))}
                for b, s in CHANNEL_AUDIENCE.get(channel, CHANNEL_AUDIENCE["ga"])]

    return {
        "locale": locale, "channel": channel, "synthetic": True,
        "virality": trends[0]["virality"] if trends else 0,
        "top_term": trends[0]["term"] if trends else "",
        "trends": trends,
        "locations": locations,
        "audience": audience,
        "female_share": int(ar.uniform(58, 79)),
    }


# --------------------------------------------------------------------------
# The join: a prompt you can run
# --------------------------------------------------------------------------

def suggest(locale: str, channel: str, root: str) -> dict:
    """Turn both sources into one surface prompt, with its reasoning shown.

    The rule is simple and stated rather than tuned: take the treatment that
    performed best in OUR history, take the treatment the market's strongest
    trend argues for, and if they agree, say so -- agreement between an
    internal and an external signal is worth more than either alone. When they
    disagree the external signal wins, on the grounds that internal history
    can only rank things already tried, and the disagreement is surfaced
    rather than hidden.

    Returns the prompt, the evidence and a confidence, because a
    recommendation you cannot interrogate is one you should not take.
    """
    cal = calendar(locale, channel, root)
    ext = external(locale, channel)

    def weighted_best(key):
        acc: dict[str, float] = {}
        wt: dict[str, int] = {}
        for p in cal["posts"]:
            acc[p[key]] = acc.get(p[key], 0.0) + p["engagement_rate"] * p["impressions"]
            wt[p[key]] = wt.get(p[key], 0) + p["impressions"]
        scored = {k: acc[k] / max(1, wt[k]) for k in acc}
        order = sorted(scored, key=lambda k: -scored[k])
        return (order[0] if order else ""), scored, order

    internal_best, treat_scores, _ = weighted_best("treatment")
    _, ratio_scores, ratio_order = weighted_best("ratio")

    top = ext["trends"][0] if ext["trends"] else None
    external_best = top["treatment"] if top else internal_best
    agree = bool(internal_best) and internal_best == external_best
    chosen = external_best or internal_best or TREATMENTS[0][0]

    cname = next((c["name"] for c in CHANNELS if c["id"] == channel), channel)
    why = []
    if internal_best:
        why.append({"source": "internal", "text":
                    f"'{internal_best}' is our strongest treatment on {cname} in "
                    f"{locale}: {treat_scores[internal_best]:.2f}% engagement, "
                    f"impression-weighted across {cal['totals']['posts']} posts."})
    if top:
        why.append({"source": "external", "text":
                    f"'{top['term']}' is the fastest-moving term in this market "
                    f"— virality {top['virality']}/100, mentions "
                    f"{top['velocity']:+.2f}x week on week — which argues for a "
                    f"{top['kind']} treatment."})
    why.append({"source": "agreement" if agree else "conflict", "text":
                ("Both signals point at the same treatment, which is the "
                 "strongest case available here."
                 if agree else
                 f"The signals disagree: our history favours '{internal_best}', "
                 f"the market favours '{external_best}'. The external signal is "
                 f"taken, because our own history can only rank treatments we "
                 f"have already tried.")})
    if ratio_order:
        why.append({"source": "internal", "text":
                    f"{RATIO_PLACEMENT.get(ratio_order[0], ratio_order[0])} leads "
                    f"on this channel at {ratio_scores[ratio_order[0]]:.2f}%."})

    return {
        "locale": locale, "channel": channel, "synthetic": True,
        "surface": chosen,
        "ratio_order": ratio_order,
        "confidence": "high" if agree else "medium",
        "agree": agree,
        "internal_best": internal_best,
        "external_best": external_best,
        "why": why,
    }


def report(locale: str, channel: str, root: str) -> dict:
    """Everything one channel tab needs for one market."""
    return {
        "synthetic": True,
        "locale": locale,
        "channel": channel,
        "channels": CHANNELS,
        "calendar": calendar(locale, channel, root),
        "external": external(locale, channel),
        "suggestion": suggest(locale, channel, root),
    }
