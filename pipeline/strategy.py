"""Evidence in, a strategy document out.

This is the middle of the engine. Discovery says what is working for
comparable products in a market; `insights.py` says what has worked for US on
that channel; this turns both into a single versioned JSON that the rest of
the pipeline can execute without re-reading any of the evidence.

Why JSON and not just "generate the images":

* It is **reviewable before it is expensive.** Every generative call in this
  repo is preceded by something a human can read and reject. A plan is the
  cheapest possible place to catch "that is the wrong audience".
* It is **the audit trail.** Every recommendation carries the evidence that
  produced it, including whether that evidence was observed or synthetic. A
  strategy you cannot interrogate is one you should not run.
* It **survives the run.** Saved next to the output, it answers "why does the
  campaign look like this" six weeks later, when nobody remembers.

WHAT THE EVIDENCE CAN AND CANNOT TELL US, stated because getting this wrong
would be the easiest way to make this whole feature dishonest:

  A scraped post gives us its FORMAT, its RATIO, its CADENCE, its reach and
  engagement, and the words of its hook. It does not give us what the image
  looked like -- captions do not describe their own art direction, and this
  repo does not run vision models over other people's creative.

  So look-alikes decide format, ratio priority, posting cadence and hook
  style. The SURFACE treatment comes from our own measured history, and only
  from a look-alike when its caption literally names a material. Each line in
  `why` says which of the two it came from, so the distinction is visible in
  the product rather than buried here.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import random
import re
from dataclasses import asdict, dataclass, field

from .discovery import CHANNEL_NAMES, CHANNEL_RATIO

SCHEMA = "social-strategy/1"

# Materials a caption might actually name. Mined from hook text rather than
# guessed at: if a competitor's caption says "wet stone", that IS observed
# evidence about their art direction and can be cited as such.
# What we LOOK FOR in a competitor's caption, and what we turn it into.
#
# The two halves matter separately. The key is the word we are willing to
# claim they said -- it has to be a plain noun a person actually types, or the
# match never fires. The value is the scene that word implies to a
# photographer, and it is the half that reaches the image model.
#
# Keeping them apart is the whole fix. An earlier version used the key for
# both, so a competitor writing "iced coffee vibes ✨" produced the prompt
# "ice, dewy micro-droplets..., soft diffused daylight..., tight
# three-quarter framing" -- one bare noun carrying the entire art direction,
# thinner than the pipeline's own hard-coded defaults and impossible to
# defend past "somebody typed ice". The citation still says they wrote
# "ice"; what we render is what ice actually looks like when it is lit.
#
# Ordered longest-first at match time, so "wet stone" wins over "stone" and
# "frosted glass" over "glass".
SURFACE_SCENE = {
    "wet stone":      "rain-dark stone with standing water and soft reflections",
    "volcanic rock":  "porous volcanic rock, matte black, warm rim light "
                      "catching the pitted texture",
    "black rock":     "smooth black basalt with a faint sheen along one edge",
    "marble":         "honed white marble with grey veining, cool and even",
    "frosted glass":  "frosted glass with a diffuse glow behind it and soft "
                      "condensation at the base",
    "glass":          "clear glass with clean specular edges and a shadow "
                      "cast through it",
    "water":          "a shallow water surface, slow ripples and broken "
                      "reflected light",
    "sand":           "fine pale sand in low raking light, every grain "
                      "throwing a small shadow",
    "concrete":       "poured concrete, flat and slightly chalky, one soft "
                      "directional highlight",
    "linen":          "rumpled natural linen, soft folds and warm shadow",
    "moss":           "damp moss and forest floor, deep green, light filtered "
                      "through leaves",
    "granite":        "speckled granite with a cool matte finish",
    "wood":           "warm oiled wood with visible open grain",
    "clay":           "unglazed terracotta clay, dry matte and porous",
    "silk":           "liquid silk in soft folds, catching a long soft "
                      "highlight",
    "mirror":         "a mirrored surface doubling the product against a dark "
                      "field",
    "ice":            "crushed ice with condensation and cold blue undertones",
    "steam":          "drifting steam backlit against a dark ground",
    "gradient":       "a smooth studio gradient sweeping from light to shadow",
}

SURFACE_VOCAB = sorted(SURFACE_SCENE, key=len, reverse=True)

# How each channel actually behaves, used to shape the plan rather than to
# decorate it. `slot_bias` is the share of the daily volume that goes here.
CHANNEL_PLAN = {
    "tiktok":    {"slot_bias": 0.34, "video_first": True,
                  "caption_len": 90,  "hashtags": 5,
                  "placement": "For You / Reels-style vertical"},
    "instagram": {"slot_bias": 0.28, "video_first": False,
                  "caption_len": 140, "hashtags": 8,
                  "placement": "Feed (4:5) + Stories"},
    "youtube":   {"slot_bias": 0.20, "video_first": True,
                  "caption_len": 120, "hashtags": 3,
                  "placement": "Shorts + in-stream companion"},
    "facebook":  {"slot_bias": 0.18, "video_first": False,
                  "caption_len": 120, "hashtags": 3,
                  "placement": "Feed + Marketplace-adjacent"},
}

# Fallback ratio order per channel: native first, then what still performs.
RATIO_ORDER = {
    "tiktok":    ["9:16", "1:1", "4:5", "16:9"],
    "instagram": ["4:5", "1:1", "9:16", "16:9"],
    "youtube":   ["16:9", "9:16", "1:1", "4:5"],
    "facebook":  ["1:1", "4:5", "9:16", "16:9"],
}


def _rng(*parts) -> random.Random:
    key = "|".join(str(p) for p in parts)
    h = hashlib.sha256(key.encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


@dataclass
class Slot:
    """One scheduled post. The unit the engine actually produces."""
    id: str
    date: str
    channel: str
    kind: str                  # "image" | "video"
    ratio: str
    placement: str
    surface: str
    message: str
    hook: str
    caption: str
    hashtags: list[str] = field(default_factory=list)
    seconds: float = 0.0       # video only
    produced: str = ""         # filled in by the engine
    verdict: str = ""
    score: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Reading the evidence
# --------------------------------------------------------------------------

def _surface_from_lookalikes(rows: list[dict]) -> tuple[str, dict | None]:
    """A material named in a competitor's own caption, if there is one.

    Deliberately conservative. It only fires on a literal mention, because the
    alternative -- inferring art direction from adjectives -- would be this
    tool inventing evidence, which is the one thing it must not do.
    """
    for row in rows:
        text = f"{row.get('title','')} {row.get('hook','')}".lower()
        for word in SURFACE_VOCAB:
            if word in text:
                return SURFACE_SCENE[word], row
        for cue in row.get("surface_cues") or []:
            return next((SURFACE_SCENE[w] for w in SURFACE_VOCAB
                         if w in cue.lower()), cue), row
    return "", None


def _dominant(rows: list[dict], key: str, default: str) -> str:
    if not rows:
        return default
    counts: dict[str, int] = {}
    for r in rows:
        v = str(r.get(key) or "")
        if v:
            # Weighted by reach: what the successful posts did, not what the
            # most numerous ones did.
            counts[v] = counts.get(v, 0) + max(int(r.get("views") or 0), 1)
    return max(counts, key=counts.get) if counts else default


def _hooks(rows: list[dict], n: int = 4) -> list[dict]:
    """The best-performing hooks, kept as structures so they stay citable."""
    ranked = sorted(rows, key=lambda r: (r.get("velocity") or 0)
                    * (r.get("engagement_rate") or 0), reverse=True)
    out = []
    for r in ranked[:n]:
        h = (r.get("hook") or r.get("title") or "").strip()
        if not h:
            continue
        out.append({"text": h[:120], "from": r.get("handle") or "",
                    "views": r.get("views") or 0,
                    "engagement_rate": r.get("engagement_rate") or 0,
                    "url": r.get("evidence_url") or "",
                    "synthetic": bool(r.get("synthetic"))})
    return out


# --------------------------------------------------------------------------
# Composing the surface prompt, so that it reads like the research
#
# The first version of this mined a single material word out of a competitor's
# caption -- "ice", "water", "wood" -- and handed that to the model as the
# whole art direction. It was traceable and it was nearly useless: one noun is
# not a scene, the pipeline's own default surfaces were richer than the thing
# claiming to be researched, and "why did it choose ice" had no better answer
# than "somebody typed ice".
#
# A prompt is now assembled from four PARTS, and every part names the row of
# evidence that chose it:
#
#   material   what the product sits on
#   light      direction, temperature and quality
#   framing    depth and distance, driven by the placement that performs
#   finish     the surface quality that carries the category
#
# Each part is a SurfacePart carrying its own `because`, so the UI can show
# the prompt beside the reason for every clause in it rather than beside one
# paragraph of prose that gestures at the whole thing. That distinction is the
# point: a strategy you cannot interrogate CLAUSE BY CLAUSE is one where the
# weak clause hides behind the strong ones.
# --------------------------------------------------------------------------

@dataclass
class SurfacePart:
    """One clause of the prompt, and the evidence that put it there."""
    slot: str                  # material | light | framing | finish
    text: str
    source: str                # lookalike | our-history | trend | channel | brief
    because: str
    evidence_url: str = ""
    synthetic: bool = False
    metric: str = ""           # the number that decided it, where there is one

    def as_dict(self) -> dict:
        return asdict(self)


# Light, by what the market is actually reacting to. These are the readings a
# photographer would take from the same brief -- a wellness trend is soft and
# even, an ingredient story is hard and raking because that is what shows
# texture, a beauty trend is cool and clean.
TREND_LIGHT = {
    "wellness":   ("soft diffused daylight from a low side angle",
                   "wellness content reads as calm, and hard light reads as clinical"),
    "ingredient": ("warm raking rim light from behind, picking out texture",
                   "ingredient stories have to SHOW the material, and only "
                   "raking light produces surface texture"),
    "aesthetic":  ("even high-key light, almost shadowless",
                   "aesthetic-led posts are graphic rather than photographic, "
                   "and shadow is what makes an image read as a photograph"),
    "beauty":     ("cool directional key with a clean specular highlight",
                   "beauty content is judged on finish, and a specular "
                   "highlight is how finish becomes visible"),
}

# Framing, by the placement that performs. A 9:16 that will be watched at arm
# length wants a tighter, shallower frame than a 16:9 that will be scrubbed
# past in a feed.
RATIO_FRAMING = {
    "9:16": ("tight three-quarter framing, shallow depth of field",
             "vertical video is held at arm's length and the product has to "
             "survive a thumb-sized crop"),
    "4:5":  ("close product framing with a little air above",
             "the tall feed crop rewards a product that fills the frame "
             "without touching the edges"),
    "1:1":  ("centred product, balanced negative space either side",
             "a square is symmetrical and anything off-centre in it reads as "
             "a mistake rather than a choice"),
    "16:9": ("wider environmental framing, product held off-centre",
             "a landscape frame has room for context, and a centred product "
             "in one looks like a stock photo"),
}

# Finish, by category. Not decoration -- these are the differences a buyer in
# that category is actually looking at.
CATEGORY_FINISH = {
    "serum":       "dewy micro-droplets and a wet specular sheen",
    "moisturiser": "soft matte bloom with a faint sheen at the edges",
    "moisturizer": "soft matte bloom with a faint sheen at the edges",
    "cleanser":    "clean water beading, nothing greasy",
    "sunscreen":   "bright clean finish, no cast",
    "lipstick":    "rich saturated pigment with a crisp edge",
    "mascara":     "deep matte black with fine separation",
    "foundation":  "even skin-like matte finish",
    "cream":       "thick soft texture catching the light",
    "oil":         "slow viscous highlights",
    "balm":        "soft translucent sheen",
    "toner":       "clear liquid clarity",
    "shampoo":     "clean fresh highlights",
}


def _material_from_lookalikes(rows: list[dict]) -> SurfacePart | None:
    """A material a competitor NAMED, in a post that worked.

    Conservative on purpose: only a literal mention counts. Inferring art
    direction from adjectives would be this tool inventing evidence, which is
    the one thing it must not do.
    """
    for row in rows:
        text = f"{row.get('title','')} {row.get('hook','')}".lower()
        for word in SURFACE_VOCAB:
            if word in text:
                scene = SURFACE_SCENE[word]
                return SurfacePart(
                    slot="material", text=scene, source="lookalike",
                    # The citation names the WORD, the prompt carries the
                    # SCENE, and this sentence is where the two are joined --
                    # so nobody reading the panel can mistake our art
                    # direction for something the competitor wrote.
                    because=(f"{row.get('handle','a competitor')} names "
                             f"'{word}' in a post at {row.get('views',0):,} "
                             f"views and {row.get('engagement_rate',0)}% "
                             f"engagement — a material stated in their own "
                             f"caption, not inferred from their image. Their "
                             f"word is '{word}'; the rendering of it is ours"),
                    evidence_url=row.get("evidence_url", ""),
                    synthetic=bool(row.get("synthetic")),
                    metric=f"{row.get('views',0):,} views")
        for cue in row.get("surface_cues") or []:
            # A cue is already a phrase rather than a single noun, but it can
            # still name something in the vocabulary, and the fuller scene is
            # the better prompt when it does.
            scene = next((SURFACE_SCENE[w] for w in SURFACE_VOCAB
                          if w in cue.lower()), cue)
            return SurfacePart(
                slot="material", text=scene, source="lookalike",
                because=(f"{row.get('handle','a competitor')} shot on {cue}, "
                         f"at {row.get('views',0):,} views"),
                evidence_url=row.get("evidence_url", ""),
                synthetic=bool(row.get("synthetic")),
                metric=f"{row.get('views',0):,} views")
    return None


def _material_from_history(hist: dict, fell_back: bool = False) -> SurfacePart | None:
    """The treatment OUR OWN posts did best on, with the number attached.

    `fell_back` distinguishes the two ways this gets used, because the reason
    matters as much as the choice. In the Internal engine our history is the
    intended source. In the Viral engine it is the fallback when no competitor
    caption named a material -- and saying "no competitor named one" on the
    Internal tab, where no competitor was ever consulted, would be a sentence
    that is simply untrue.
    """
    best = (hist.get("best_treatment") or "").strip()
    if not best:
        return None
    er = hist.get("best_treatment_er") or hist.get("engagement_rate")
    ctr = hist.get("best_treatment_ctr")
    n = hist.get("best_treatment_posts") or hist.get("posts") or 0
    core = (f"'{best}' is our strongest treatment on this channel in this "
            f"market: {er}% engagement"
            + (f" and {ctr}% CTR" if ctr else "")
            + f", impression-weighted across {n} posts")
    tail = (". No competitor caption named a material, and guessing one would "
            "be worse than using something we have measured." if fell_back
            else ". This is our own measurement, not a competitor's.")
    return SurfacePart(
        slot="material", text=best, source="our-history",
        because=core + tail,
        evidence_url="", synthetic=bool(hist.get("synthetic")),
        metric=f"{er}% engagement")


def compose_surface(parts: list[SurfacePart]) -> str:
    """Four clauses into one prompt.

    Ordered the way a photographer would brief it -- what it sits on, how it
    is lit, how it is framed, what the finish should read as -- because image
    models weight earlier tokens more heavily and the material is the thing
    that must survive.
    """
    order = {"material": 0, "finish": 1, "light": 2, "framing": 3}
    live = [p for p in parts if p and p.text]
    live.sort(key=lambda p: order.get(p.slot, 9))
    return ", ".join(p.text for p in live)


def surface_for(mode: str, rows: list[dict], hist: dict, trend: dict,
                ratio: str, category: str, product: dict) -> tuple[str, list[SurfacePart]]:
    """The whole art direction, and the evidence for every clause of it.

    `mode` decides which evidence gets to choose the MATERIAL, which is the
    clause that dominates the image:

        viral      a competitor's own caption, falling back to our history
        internal   our own measured performance, and only our own

    The other three clauses come from whichever signal is actually about them
    -- light from what the market is reacting to, framing from the placement
    that performs, finish from the category. Sourcing all four from one place
    would be tidier and would mean three of them were decoration.
    """
    parts: list[SurfacePart] = []

    # ---- material ------------------------------------------------------
    mat = None
    if mode == "viral":
        mat = (_material_from_lookalikes(rows)
               or _material_from_history(hist, fell_back=True))
    else:
        mat = _material_from_history(hist)
    if mat is None:
        mat = SurfacePart(
            slot="material", text=(product.get("surface")
                                   or "a clean neutral studio surface"),
            source="brief",
            because=("Neither the market nor our own history offered a "
                     "treatment for this channel, so the brief's surface is "
                     "kept rather than invented."))
    parts.append(mat)

    # ---- finish --------------------------------------------------------
    fin = CATEGORY_FINISH.get(category)
    if fin:
        parts.append(SurfacePart(
            slot="finish", text=fin, source="brief",
            because=(f"The finish a {category} is judged on. This clause is "
                     f"category knowledge rather than a measurement, and is "
                     f"marked as such rather than dressed up as research.")))

    # ---- light ---------------------------------------------------------
    if mode == "viral":
        kind = (trend or {}).get("kind") or ""
        term = (trend or {}).get("term") or ""
        lit = TREND_LIGHT.get(kind)
        if lit:
            parts.append(SurfacePart(
                slot="light", text=lit[0], source="trend",
                because=(f"'{term}' is the fastest-moving term in this market "
                         f"({(trend or {}).get('velocity', 0)}x week on week, "
                         f"virality {(trend or {}).get('virality', 0)}/100) and "
                         f"it is a {kind} trend — {lit[1]}"),
                synthetic=bool((trend or {}).get("synthetic", True)),
                metric=f"{(trend or {}).get('velocity', 0)}x w/w"))
    else:
        # Internal: the light follows the format our own audience watches to
        # the end, because watch-through is the only metric here that is about
        # how the image is READ rather than how it was distributed.
        wt = hist.get("watch_through") or 0
        if wt >= 45:
            parts.append(SurfacePart(
                slot="light", text="warm raking rim light, picking out texture",
                source="our-history",
                because=(f"Our watch-through on this channel is {wt}%, which "
                         f"is high — the audience is staying for the product "
                         f"itself, so the light should show its texture "
                         f"rather than flatter the scene"),
                synthetic=bool(hist.get("synthetic")), metric=f"{wt}% watched"))
        else:
            parts.append(SurfacePart(
                slot="light", text="bright even key light, high clarity",
                source="our-history",
                because=(f"Watch-through here is {wt}%, so the image has to "
                         f"land in the first moment. Even, bright light reads "
                         f"fastest at thumbnail size; a moody key does not"),
                synthetic=bool(hist.get("synthetic")), metric=f"{wt}% watched"))

    # ---- framing -------------------------------------------------------
    fr = RATIO_FRAMING.get(ratio)
    if fr:
        if mode == "internal" and hist.get("best_ratio") == ratio:
            why = (f"{ratio} is our best-performing placement on this channel "
                   f"at {hist.get('best_ratio_er', 0)}% engagement — {fr[1]}")
        else:
            why = f"{ratio} leads this channel — {fr[1]}"
        parts.append(SurfacePart(
            slot="framing", text=fr[0], source=(
                "our-history" if mode == "internal" else "channel"),
            because=why, synthetic=bool(hist.get("synthetic"))
            if mode == "internal" else False,
            metric=(f"{hist.get('best_ratio_er', 0)}% engagement"
                    if mode == "internal" and hist.get("best_ratio") == ratio
                    else "")))

    return compose_surface(parts), parts


# --------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------

def schedule(days: int, images_per_day: int, videos_per_day: int,
             channels: list[str], start: str = "") -> dict[str, list[dict]]:
    """Spread the daily volume across channels, by date.

    The per-day counts are TOTAL across channels, not per channel. Four
    channels x two images a day for a fortnight is a hundred and twelve posts
    per product, which is not a campaign, it is a denial-of-service on a
    social team. The counts are the publishing rate; `slot_bias` decides where
    each one lands.
    """
    start_d = (_dt.date.fromisoformat(start) if start else _dt.date.today())
    out: dict[str, list[dict]] = {c: [] for c in channels}
    if not channels or days <= 0:
        return out

    bias = [CHANNEL_PLAN.get(c, {}).get("slot_bias", 1 / len(channels))
            for c in channels]
    total = sum(bias) or 1.0
    bias = [b / total for b in bias]

    for kind, per_day in (("image", images_per_day), ("video", videos_per_day)):
        n = max(0, per_day) * days
        if not n:
            continue
        # Largest-remainder apportionment, not an independent weighted draw
        # per slot. Drawing per slot is the obvious way to write this and it
        # is wrong in a way that matters: over 21 slots it produced a 5/10/4/2
        # split against a stated 34/28/20/18 bias, so the plan disagreed with
        # its own description of itself. This gives the stated proportions
        # exactly, and gives them the same way every time.
        exact = [b * n for b in bias]
        take = [int(x) for x in exact]
        rem = n - sum(take)
        order = sorted(range(len(channels)),
                       key=lambda i: (exact[i] - take[i], bias[i]), reverse=True)
        for i in order[:rem]:
            take[i] += 1

        # Dates spread evenly across the window per channel, so a channel with
        # a third of the volume posts through the whole fortnight rather than
        # in a clump at the front of it.
        for ci, c in enumerate(channels):
            for k in range(take[ci]):
                day = (k * days) // max(take[ci], 1)
                out[c].append({
                    "date": (start_d + _dt.timedelta(days=day)).isoformat(),
                    "kind": kind, "index": k})

    for c in channels:
        out[c].sort(key=lambda s: (s["date"], s["kind"], s["index"]))
    return out


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

def build(product: dict, market: dict, discovery_by_channel: dict,
          history_by_channel: dict, days: int, images_per_day: int,
          videos_per_day: int, ratios_available: list[str],
          channels: list[str] | None = None, start: str = "",
          mode: str = "viral") -> dict:
    """One strategy document for one product in one market.

    `mode` decides what gets to choose the art direction:

        viral      what comparable products are doing in this market
        internal   what OUR OWN posts have measurably done on this channel

    Both produce the same document shape, so everything downstream -- the
    engine, the UI, the saved JSON -- is identical. Only the evidence differs,
    and every clause of the prompt says which of the two put it there.
    """
    channels = channels or list(CHANNEL_PLAN)
    plan = schedule(days, images_per_day, videos_per_day, channels, start)
    doc = {
        "schema": SCHEMA,
        "product": {"id": product.get("id"), "name": product.get("name"),
                    "category": product.get("category")
                    or _category(product), "asset": product.get("asset")},
        "market": {"locale": market.get("locale"), "region": market.get("region"),
                   "audience": market.get("audience"),
                   "message": market.get("message")},
        "mode": mode,
        "window": {"days": days, "start": (start or _dt.date.today().isoformat()),
                   "images_per_day": images_per_day,
                   "videos_per_day": videos_per_day},
        "channels": {},
        "totals": {"slots": 0, "images": 0, "videos": 0, "generative_calls": 0},
        "synthetic_evidence": False,
    }

    for ch in channels:
        disc = discovery_by_channel.get(ch) or {}
        rows = disc.get("lookalikes") or []
        hist = history_by_channel.get(ch) or {}
        cfg = CHANNEL_PLAN.get(ch, {})
        why: list[dict] = []

        # ---- format and ratio -------------------------------------------
        dom_format = _dominant(rows, "format", "video" if cfg.get("video_first") else "image")
        order = [r for r in RATIO_ORDER.get(ch, []) if r in ratios_available]
        order += [r for r in ratios_available if r not in order]
        if rows:
            top = rows[0]
            why.append({"source": "lookalike", "text":
                        f"{dom_format} is the dominant format here, weighted "
                        f"by reach across {len(rows)} comparable posts; the "
                        f"strongest is {top.get('handle','')} at "
                        f"{top.get('views',0):,} views, "
                        f"{top.get('engagement_rate',0)}% engagement.",
                        "url": top.get("evidence_url", ""),
                        "synthetic": bool(top.get("synthetic"))})
        if hist.get("best_ratio"):
            why.append({"source": "our-history", "text":
                        f"Our own best placement on this channel is "
                        f"{hist['best_ratio']} at "
                        f"{hist.get('best_ratio_er','?')}% engagement.",
                        "url": "", "synthetic": bool(hist.get("synthetic"))})

        if disc.get("synthetic"):
            doc["synthetic_evidence"] = True
            why.append({"source": "warning", "text":
                        "Discovery for this channel is SYNTHETIC — generated "
                        "from a fixed seed, not observed. "
                        + (disc.get("fell_back_because") or
                           "No live discovery backend was configured."),
                        "url": "", "synthetic": True})

        # ---- the surface, composed clause by clause ----------------------
        # Built AFTER the ratio order, because the framing clause depends on
        # which placement actually leads this channel.
        surface, sparts = surface_for(
            mode=mode, rows=rows, hist=hist,
            trend=(disc.get("top_trend") or hist.get("top_trend") or {}),
            ratio=(order[0] if order else "1:1"),
            category=doc["product"]["category"], product=product)
        for p in sparts:
            why.append({"source": p.source, "text": p.because,
                        "url": p.evidence_url, "synthetic": p.synthetic,
                        "clause": p.text, "slot": p.slot, "metric": p.metric})

        hooks = _hooks(rows)
        slots = _slots(ch, plan.get(ch, []), surface, order, hooks, product,
                       market, cfg)

        doc["channels"][ch] = {
            "name": CHANNEL_NAMES.get(ch, ch),
            "placement": cfg.get("placement", ""),
            "discovery": {"backend": disc.get("backend", "none"),
                          "synthetic": bool(disc.get("synthetic")),
                          "cached": bool(disc.get("cached")),
                          "count": len(rows),
                          "fell_back_because": disc.get("fell_back_because", "")},
            "lookalikes": rows[:6],
            "positioning": _positioning(product, market, dom_format, ch),
            "surface": surface,
            # The prompt taken apart. Shown beside it in the UI so the weak
            # clause cannot hide behind the strong ones.
            "surface_parts": [p.as_dict() for p in sparts],
            "dominant_format": dom_format,
            "ratio_order": order,
            "hooks": hooks,
            "hashtags": _hashtags(product, market, ch, cfg.get("hashtags", 4)),
            "cadence": {"slots": len(slots),
                        "images": sum(1 for s in slots if s.kind == "image"),
                        "videos": sum(1 for s in slots if s.kind == "video")},
            "why": why,
            "slots": [s.as_dict() for s in slots],
        }

    for ch, c in doc["channels"].items():
        doc["totals"]["slots"] += c["cadence"]["slots"]
        doc["totals"]["images"] += c["cadence"]["images"]
        doc["totals"]["videos"] += c["cadence"]["videos"]
    # One master per channel that has any work, because the surface differs
    # per channel and the master is what a surface costs. Every ratio, every
    # date and every video for that channel is composed from it locally --
    # the same "generate once, compose per spec" rule the pipeline already
    # enforces, applied one level up.
    doc["totals"]["generative_calls"] = sum(
        1 for c in doc["channels"].values() if c["cadence"]["slots"])
    return doc


def _category(product: dict) -> str:
    """A search term, from the product's own words. Never its brand name --
    searching your own product name finds your own posts."""
    text = f"{product.get('subject','')} {product.get('name','')}".lower()
    for word in ("serum", "lipstick", "moisturiser", "moisturizer", "cleanser",
                 "sunscreen", "mascara", "foundation", "cream", "oil", "balm",
                 "toner", "shampoo"):
        if word in text:
            return word
    return "skincare"


def _positioning(product: dict, market: dict, fmt: str, channel: str) -> str:
    return (f"{product.get('name')} for {market.get('audience')} in "
            f"{market.get('region')}, led with {fmt} on "
            f"{CHANNEL_NAMES.get(channel, channel)}.")


def _hashtags(product: dict, market: dict, channel: str, n: int) -> list[str]:
    cat = _category(product)
    base = [f"#{cat.replace(' ', '')}", f"#{product.get('id','').replace('-', '')}",
            "#skincare", "#routine", "#beforeandafter", "#dermapproved",
            "#glowup", "#selfcare", "#newin"]
    loc = (market.get("locale") or "").split("-")[-1].lower()
    if loc:
        base.insert(2, f"#{cat.replace(' ', '')}{loc}")
    seen, out = set(), []
    for t in base:
        if t not in seen and len(t) > 1:
            seen.add(t)
            out.append(t)
    return out[:n]


def _slots(channel: str, planned: list[dict], surface: str, order: list[str],
           hooks: list[dict], product: dict, market: dict, cfg: dict) -> list[Slot]:
    out: list[Slot] = []
    msg = market.get("message") or ""
    for i, p in enumerate(planned):
        r = _rng(product.get("id"), market.get("locale"), channel,
                 p["date"], p["kind"], p["index"])
        # Ratios rotate through the channel's order rather than repeating the
        # native one every day: a fortnight of identical crops is not a
        # campaign, and the order still front-loads what performs.
        ratio = order[i % len(order)] if order else CHANNEL_RATIO.get(channel, "1:1")
        if p["kind"] == "video":
            ratio = order[0] if order else ratio
        hook = hooks[i % len(hooks)]["text"] if hooks else msg
        out.append(Slot(
            id=f"{product.get('id')}-{market.get('locale')}-{channel}-{p['date']}-{p['kind']}-{p['index']}",
            date=p["date"], channel=channel, kind=p["kind"], ratio=ratio,
            placement=cfg.get("placement", ""), surface=surface,
            message=msg, hook=hook,
            caption=_caption(msg, hook, cfg.get("caption_len", 120)),
            hashtags=_hashtags(product, market, channel, cfg.get("hashtags", 4)),
            seconds=round(r.choice([6.0, 8.0, 10.0, 12.0]), 1)
            if p["kind"] == "video" else 0.0,
        ))
    return out


def _caption(message: str, hook: str, limit: int) -> str:
    """Our copy, plus our hashtags. Never the competitor's words.

    The first version pasted the winning look-alike's caption straight into
    the slot, so a competitor's hashtags and their brand mentions ended up
    burned into our creative. That is worth spelling out because it looked
    fine on screen: it is passing off someone else's copy as ours, it ships
    their @-mentions to our audience, and the on-image text stops being
    anything brand or legal ever approved.

    Competitor hooks stay where they belong -- shown beside the strategy,
    with attribution and a link, as a reference for whoever writes the real
    caption. A hook tells you the SHAPE that works on a channel. It is not
    licensed copy.
    """
    return (message or "")[:limit].rstrip(" —-")


def save(doc: dict, path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    return path
