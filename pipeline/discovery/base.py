"""What a discovery backend is, and what it has to hand back.

Discovery answers one question: *for this product, in this region, aimed at
this audience -- what comparable thing is currently working on this channel,
and what does its creative look like?*

That question is the input to strategy. It is deliberately not "what is
trending", which is a question about the whole market and produces the same
answer for every product a brand sells. A look-alike is anchored to the
product: same category, same price band, same job-to-be-done, chosen because
it is a thing this product's buyer would plausibly see instead.

The interface is the same shape as `providers/` and `storage/`: several
backends, one protocol, credentials reported rather than assumed, and a
fallback that always runs so a reviewer with no keys still gets a working
tool. The honesty rule from `insights.py` carries over intact -- anything not
actually observed is flagged `synthetic: true` and says so on screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Protocol, runtime_checkable


class DiscoveryError(RuntimeError):
    """A backend could not answer. Never raised to mean 'found nothing'."""


@dataclass(frozen=True)
class Lookalike:
    """One competing/comparable post that is doing well right now.

    `evidence_url` is the whole point of the record: a strategy built on
    numbers nobody can go and look at is a strategy nobody can argue with,
    and the ones you cannot argue with are the ones that quietly go wrong.
    Synthetic rows carry an empty url and `synthetic: True`, so the two can
    never be confused in the UI or in the saved JSON.
    """
    channel: str                    # tiktok | instagram | youtube | facebook
    handle: str                     # who posted it
    brand: str                      # the competing brand, where known
    title: str                      # caption / video title, trimmed
    product_category: str
    posted_days_ago: int
    views: int
    engagement_rate: float          # per cent
    velocity: float                 # multiple vs. that account's median post
    ratio: str                      # the aspect it was published in
    format: str                     # "video" | "image" | "carousel"
    hook: str                       # the first line / first 2s, if readable
    surface_cues: list[str] = field(default_factory=list)
    palette: list[str] = field(default_factory=list)
    evidence_url: str = ""
    # The post's own cover image. `thumb_url` is where the network serves it;
    # `thumb` is our cached copy, because hotlinking somebody's CDN from a
    # local tool is fragile (referer checks, expiring signatures) and means the
    # evidence disappears the moment you are offline. A look-alike you cannot
    # SEE is a row of numbers, and the whole argument for looking at competitor
    # creative is that you look at it.
    thumb_url: str = ""
    thumb: str = ""
    synthetic: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryRequest:
    product_id: str
    product_name: str
    category: str
    locale: str
    region: str
    audience: str
    channel: str
    limit: int = 8


@runtime_checkable
class Discovery(Protocol):
    name: str
    synthetic: bool

    def find(self, req: DiscoveryRequest) -> list[Lookalike]:
        ...


# What each backend needs before it can claim to be usable. Reported, never
# assumed -- the same distinction the provider registry makes between "this
# module imported" and "this module can actually run".
DISCOVERY_CREDENTIALS: dict[str, list[str]] = {
    "synthetic": [],
    "apify": ["APIFY_TOKEN"],
    "playwright": [],
}

# The channels the engine actually runs. THIS is the source of truth -- the
# schedule, the UI, the history lookup and the cost arithmetic all derive from
# it, so adding or removing one is a change here and nowhere else.
#
# Facebook is deliberately absent. Its actor scrapes NAMED PAGES; Facebook has
# no public keyword post search, so "find look-alikes for a face serum" is not
# a question it can be asked. Every live run fell back to synthetic for this
# channel alone, which is a worse outcome than not offering it: a channel tab
# full of invented evidence sitting next to three tabs of real posts invites
# exactly the mistake this repo keeps guarding against.
#
# Re-enabling it means giving discovery a LIST OF COMPETITOR PAGES to crawl
# rather than a category, which is a different input shape and a real feature,
# not a config change. The maps below keep their Facebook entries so that day
# is a one-word edit here.
CHANNELS = ("tiktok", "instagram", "youtube")

CHANNEL_NAMES = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "facebook": "Facebook",
}

# The native aspect each channel actually rewards. Used to bias both what we
# go looking for and what the strategy asks the pipeline to produce.
CHANNEL_RATIO = {
    "tiktok": "9:16",
    "instagram": "4:5",
    "youtube": "16:9",
    "facebook": "1:1",
}
