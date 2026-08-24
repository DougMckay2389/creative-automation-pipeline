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
SURFACE_VOCAB = [
    "wet stone", "volcanic rock", "black rock", "marble", "frosted glass",
    "glass", "water", "sand", "concrete", "linen", "moss", "granite",
    "wood", "clay", "silk", "mirror", "ice", "steam", "gradient",
]

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
                return word, row
        for cue in row.get("surface_cues") or []:
            return cue, row
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
          channels: list[str] | None = None, start: str = "") -> dict:
    """One strategy document for one product in one market."""
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

        # ---- surface -----------------------------------------------------
        mined, mined_row = _surface_from_lookalikes(rows)
        our_best = (hist.get("best_treatment") or "").strip()
        if mined:
            surface = mined
            why.append({"source": "lookalike", "text":
                        f"{mined_row.get('handle','a competitor')} names "
                        f"'{mined}' in a post at {mined_row.get('views',0):,} "
                        f"views — a material stated in their own caption, not "
                        f"inferred from the image.",
                        "url": mined_row.get("evidence_url", ""),
                        "synthetic": bool(mined_row.get("synthetic"))})
        elif our_best:
            surface = our_best
            why.append({"source": "our-history", "text":
                        f"No competitor caption names a material, so the "
                        f"surface comes from our own best-performing "
                        f"treatment on {CHANNEL_NAMES.get(ch, ch)} in "
                        f"{market.get('locale')}: '{our_best}'.",
                        "url": "", "synthetic": bool(hist.get("synthetic"))})
        else:
            surface = product.get("surface") or "soft neutral studio surface"
            why.append({"source": "brief", "text":
                        "Neither the market nor our history offered a "
                        "treatment, so the brief's own surface is kept "
                        "unchanged rather than invented.",
                        "url": "", "synthetic": False})

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
