"""Load and validate a campaign brief, and expand it into concrete variants.

Why this file exists
--------------------
Every failure mode in a creative pipeline that is expensive to debug starts
with a brief that was subtly wrong -- a market with no message, an aspect
ratio with no dimensions, a product whose asset path points at nothing. Those
mistakes are cheap to catch here and expensive to catch after you have spent
generative credits on them.

So this module does two jobs and nothing else:

1. Parse YAML into typed objects, refusing anything malformed with an error
   that names the field.
2. Expand the brief into the full list of Variants (product x market x ratio)
   so that the rest of the pipeline never has to think about the cross
   product again.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

import yaml


def stable_seed(*parts: str) -> int:
    """A process-stable 31-bit seed from any set of strings.

    Used everywhere a seed is needed. Deliberately NOT builtin hash(): that is
    salted per interpreter run, so it produces a different value every time the
    program starts.
    """
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (2 ** 31)


class BriefError(ValueError):
    """Raised when a brief cannot be trusted. Always names the offending field."""


# --------------------------------------------------------------------------
# Typed pieces of a brief
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Product:
    id: str
    name: str
    asset: str | None          # path to a pre-existing hero asset, if any
    subject: str               # what to generate if the asset is missing
    surface: str
    # Reuse the approved photograph but generate a NEW scene around it.
    # Off by default: reusing an asset exactly as shot is the safe reading of
    # "reuse", and putting an approved product somewhere it has never been
    # photographed is a decision somebody should make on purpose.
    regenerate_surface: bool = False

    def has_asset(self) -> bool:
        """True only if the file is actually there AND is non-empty.

        A path in a brief is a claim, not a fact. Checking the filesystem here
        is what makes 'reuse when available' a real behaviour rather than a
        hopeful one.
        """
        return bool(self.asset) and os.path.isfile(self.asset) and os.path.getsize(self.asset) > 0


@dataclass(frozen=True)
class Market:
    locale: str                # BCP-47-ish, e.g. en-US, ja-JP
    region: str
    audience: str
    message: str

    @property
    def language(self) -> str:
        return self.locale.split("-")[0].lower()


@dataclass(frozen=True)
class Ratio:
    id: str                    # "1:1"
    width: int
    height: int
    channel: str

    @property
    def slug(self) -> str:
        """Filesystem-safe form. '1:1' is legal on POSIX and illegal on Windows."""
        return self.id.replace(":", "x")


@dataclass(frozen=True)
class Variant:
    """One deliverable file: this product, in this market, at this spec."""
    product: Product
    market: Market
    ratio: Ratio

    @property
    def id(self) -> str:
        return f"{self.product.id}__{self.market.locale}__{self.ratio.slug}"

    @property
    def seed(self) -> int:
        """Deterministic seed derived from the variant identity.

        Reproducibility is a compliance property, not a convenience. Six
        months from now somebody will ask why a particular asset looks the way
        it does; a random seed makes that question unanswerable. Deriving the
        seed from the id means the same brief regenerates the same pixels.

        NOTE the use of sha256 rather than the builtin hash(). Python
        randomises string hashing per process (PYTHONHASHSEED), so hash()
        would give a different seed on every run -- which silently breaks both
        reproducibility and the generated-asset cache. This is the kind of bug
        that only shows up as "why did it regenerate?" three weeks later.
        """
        return stable_seed(self.id)


@dataclass
class Brief:
    campaign_id: str
    campaign_name: str
    brand: str
    default_message: str
    products: list[Product]
    markets: list[Market]
    ratios: list[Ratio]
    prohibited_terms: list[str] = field(default_factory=list)
    source_path: str = ""

    def variants(self) -> Iterator[Variant]:
        """The full cross product, in a stable order.

        Stable order matters: it makes runs diffable and the report readable.
        """
        for p in self.products:
            for m in self.markets:
                for r in self.ratios:
                    yield Variant(p, m, r)

    @property
    def variant_count(self) -> int:
        return len(self.products) * len(self.markets) * len(self.ratios)

    @property
    def generation_count(self) -> int:
        """How many generative calls this brief actually costs.

        This is the number worth arguing about. It is NOT variant_count: we
        generate one master per product that lacks an asset, and compose every
        market and every ratio from it. See runner.py.
        """
        return sum(1 for p in self.products if not p.has_asset())


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise BriefError(f"{where}: missing required field '{key}'")
    return d[key]


def load_brief(path: str) -> Brief:
    """Parse a brief file into a validated Brief.

    Deliberately strict. A brief that is 90% right produces creatives that are
    90% right, which is worse than a hard failure because nobody notices.
    """
    if not os.path.isfile(path):
        raise BriefError(f"brief not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    camp = raw.get("campaign") or {}
    campaign_id = _require(camp, "id", "campaign")

    products_raw = raw.get("products") or []
    if len(products_raw) < 2:
        # The exercise asks for at least two products; more importantly, a
        # pipeline that has only ever been run on one product has not been
        # tested on the thing that makes it a pipeline.
        raise BriefError("products: at least two products are required")

    products = []
    for i, p in enumerate(products_raw):
        where = f"products[{i}]"
        products.append(Product(
            id=str(_require(p, "id", where)),
            name=str(_require(p, "name", where)),
            asset=p.get("asset"),
            subject=str(p.get("subject") or p.get("name")),
            surface=str(p.get("surface") or "a clean neutral surface"),
            regenerate_surface=bool(p.get("regenerate_surface", False)),
        ))

    markets_raw = raw.get("markets") or []
    if not markets_raw:
        raise BriefError("markets: at least one market is required")

    default_message = str(raw.get("default_message") or "")
    markets = []
    for i, m in enumerate(markets_raw):
        where = f"markets[{i}]"
        msg = m.get("message") or default_message
        if not msg:
            raise BriefError(
                f"{where}: no 'message' and no top-level 'default_message' to fall back to")
        markets.append(Market(
            locale=str(_require(m, "locale", where)),
            region=str(_require(m, "region", where)),
            audience=str(_require(m, "audience", where)),
            message=str(msg),
        ))

    ratios_raw = raw.get("aspect_ratios") or []
    if len(ratios_raw) < 3:
        raise BriefError("aspect_ratios: at least three ratios are required")

    ratios = []
    for i, r in enumerate(ratios_raw):
        where = f"aspect_ratios[{i}]"
        w, h = int(_require(r, "width", where)), int(_require(r, "height", where))
        if w <= 0 or h <= 0:
            raise BriefError(f"{where}: width and height must be positive")
        ratios.append(Ratio(
            id=str(_require(r, "id", where)),
            width=w, height=h,
            channel=str(r.get("channel") or ""),
        ))

    # Nothing downstream keys on anything but these ids, so a duplicate is not
    # a stylistic problem -- it is silent data loss.
    #
    # output_path_for() builds <product>/<ratio>/<product>_<locale>_<ratio>.jpg.
    # Two aspect_ratios entries sharing an id therefore write the SAME file,
    # and the second quietly overwrites the first: the run reports the full
    # deliverable count, the folder holds fewer files than it claims, and
    # nobody notices until a channel is missing an asset. Same for a repeated
    # product id or locale.
    #
    # Found by the brief form, which made adding a second "16:9" a single
    # click -- a good argument for validating the data rather than trusting
    # the editor.
    for label, values in (("products", [p.id for p in products]),
                          ("markets", [m.locale for m in markets]),
                          ("aspect_ratios", [r.id for r in ratios])):
        seen, dupes = set(), []
        for v in values:
            if v in seen and v not in dupes:
                dupes.append(v)
            seen.add(v)
        if dupes:
            raise BriefError(
                f"{label}: duplicated {'entry' if len(dupes) == 1 else 'entries'} "
                f"{', '.join(repr(d) for d in dupes)} - each must be unique, "
                f"because two of them would write the same output file")

    return Brief(
        campaign_id=str(campaign_id),
        campaign_name=str(camp.get("name") or campaign_id),
        brand=str(camp.get("brand") or ""),
        default_message=default_message,
        products=products,
        markets=markets,
        ratios=ratios,
        prohibited_terms=[str(t) for t in (raw.get("prohibited_terms") or [])],
        source_path=path,
    )
