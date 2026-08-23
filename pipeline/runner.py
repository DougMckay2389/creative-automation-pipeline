"""Orchestration: brief in, organised folder of checked creatives out.

The order of operations is the design:

    1. load + validate the brief          (free, catches typos)
    2. pre-flight                         (free, refuses doomed copy)
    3. resolve one master per product     (the only expensive step)
    4. compose every variant from masters (local, fast)
    5. check every composition            (local, fast)
    6. write manifest + HTML report       (local, fast)

Step 3 is deliberately outside the variant loop. Doing it per variant would
turn 1 generative call into 18 for the sample brief. That single decision is
the difference between a demo and something a brand can afford to run monthly.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field

import yaml

from .assets import AssetResolver, MasterAsset
from .brief import Brief, Variant, load_brief, stable_seed
from .checks import Finding, Verdict, evaluate, preflight_brief
from .compose import Composer
from .providers import get_provider


@dataclass
class VariantResult:
    variant_id: str
    product_id: str
    locale: str
    ratio: str
    path: str
    verdict: str
    findings: list[dict] = field(default_factory=list)
    font_family: str = ""
    message: str = ""
    dominant_hex: list[str] = field(default_factory=list)
    master_origin: str = ""


@dataclass
class RunSummary:
    run_id: str
    campaign_id: str
    provider: str
    model: str
    started_at: str
    duration_s: float
    variants_planned: int
    generative_calls: int
    reused_from_brief: int
    reused_from_cache: int
    counts: dict = field(default_factory=dict)
    preflight: list[dict] = field(default_factory=list)
    results: list[VariantResult] = field(default_factory=list)
    output_dir: str = ""


class JsonLogger:
    """One structured line per event. Greppable, and it is the audit trail."""

    def __init__(self, path: str, echo: bool = True, sink=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")
        self.echo = echo
        # A UI subscribes here. It receives exactly the events the log file
        # receives -- so the picture on screen cannot drift from the record.
        self.sink = sink

    def __call__(self, event: str, **fields):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        if self.sink:
            try:
                self.sink(rec)
            except Exception:
                pass                      # a broken UI must never fail a run
        if self.echo:
            extra = " ".join(f"{k}={v}" for k, v in fields.items()
                             if k in ("product", "variant", "verdict", "source", "count"))
            print(f"  {event:<12} {extra}")

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def load_brand(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def output_path_for(out_root: str, v: Variant) -> str:
    """Outputs organised by product, then aspect ratio -- as the brief asks.

    Locale lives in the filename rather than another directory level: a
    reviewer wants to see all three languages of a spec side by side, and
    nesting one more level makes that a chore.
    """
    return os.path.join(out_root, v.product.id, v.ratio.slug,
                        f"{v.product.id}_{v.market.locale}_{v.ratio.slug}.jpg")


def run_campaign(brief_path: str, brand_path: str = "brandkit/brand.yaml",
                 provider_name: str = "mock", out_root: str = "output",
                 rpm: float | None = None, quiet: bool = False,
                 cache_dir: str = ".cache/masters", on_event=None) -> RunSummary:
    t0 = time.monotonic()
    run_id = time.strftime("%Y%m%d-%H%M%S")

    brief: Brief = load_brief(brief_path)
    brand = load_brand(brand_path)

    out_dir = os.path.join(out_root, brief.campaign_id, run_id)
    log = JsonLogger(os.path.join(out_dir, "run.log.jsonl"), echo=not quiet,
                     sink=on_event)
    log("run-start", campaign=brief.campaign_id, provider=provider_name,
        variants=brief.variant_count)

    # --- 2. pre-flight -----------------------------------------------------
    pre: list[Finding] = preflight_brief(brief)
    for f in pre:
        log("preflight", rule=f.rule_id, severity=f.severity.value, message=f.message)
    if any(f.severity.value == "blocker" for f in pre):
        log("run-abort", reason="pre-flight blocker: no generative credits spent")
        summary = RunSummary(
            run_id=run_id, campaign_id=brief.campaign_id, provider=provider_name,
            model="", started_at=run_id, duration_s=time.monotonic() - t0,
            variants_planned=brief.variant_count, generative_calls=0,
            reused_from_brief=0, reused_from_cache=0,
            counts={"block": brief.variant_count},
            preflight=[f.as_dict() for f in pre], output_dir=out_dir)
        _write_manifest(summary, out_dir)
        log.close()
        return summary

    kwargs = {"rpm": rpm} if rpm else {}
    provider = get_provider(provider_name, **kwargs)
    resolver = AssetResolver(provider, cache_dir=cache_dir, log=log)
    composer = Composer(brand)

    # --- 3. one master per product ----------------------------------------
    masters: dict[str, MasterAsset] = {}
    for p in brief.products:
        seed = stable_seed(brief.campaign_id, p.id)
        masters[p.id] = resolver.resolve(p, seed)

    gen = sum(1 for m in masters.values() if m.origin == "generated")
    from_brief = sum(1 for m in masters.values() if m.origin == "brief")
    from_cache = sum(1 for m in masters.values() if m.origin == "cache")
    log("masters-ready", generated=gen, from_brief=from_brief, from_cache=from_cache)

    # --- 4 + 5. compose and check every variant ---------------------------
    results: list[VariantResult] = []
    counts = {"pass": 0, "review": 0, "block": 0}
    for v in brief.variants():
        master = masters[v.product.id]
        path = output_path_for(out_dir, v)
        log("variant-start", variant=v.id, product=v.product.id,
            locale=v.market.locale, ratio=v.ratio.id)
        comp = composer.compose(
            master.path, v, path,
            on_stage=lambda name, _v=v: log("stage", stage=name, variant=_v.id))
        res = evaluate(comp, v, brand, brief.prohibited_terms)
        log("stage", stage="checks", variant=v.id,
            rules=[f["rule"] for f in [x.as_dict() for x in res.findings]])
        counts[res.verdict.value] += 1
        results.append(VariantResult(
            variant_id=v.id, product_id=v.product.id, locale=v.market.locale,
            ratio=v.ratio.id, path=os.path.relpath(path, out_dir),
            verdict=res.verdict.value,
            findings=[f.as_dict() for f in res.findings],
            font_family=comp.font_family, message=comp.message,
            dominant_hex=comp.dominant_hex, master_origin=master.origin))
        # Everything the UI needs to show this creative the moment it exists.
        #
        # The event used to carry the id and the verdict, which is enough to
        # advance a progress graph and not enough to draw the thing. The local
        # app shows creatives as they land rather than in one batch at the
        # end, and it should not have to guess a path to do it.
        log("variant", variant=v.id, verdict=res.verdict.value,
            product=v.product.id, locale=v.market.locale, ratio=v.ratio.id,
            message=v.market.message, out_dir=out_dir,
            path=os.path.relpath(path, out_dir).replace(os.sep, "/"),
            findings=[f.as_dict() for f in res.findings])

    summary = RunSummary(
        run_id=run_id, campaign_id=brief.campaign_id, provider=provider_name,
        model=getattr(provider, "model", ""), started_at=run_id,
        duration_s=time.monotonic() - t0,
        variants_planned=brief.variant_count, generative_calls=gen,
        reused_from_brief=from_brief, reused_from_cache=from_cache,
        counts=counts, preflight=[f.as_dict() for f in pre],
        results=results, output_dir=out_dir)

    _write_manifest(summary, out_dir)
    log("run-end", **counts, generative_calls=gen,
        duration_s=round(summary.duration_s, 2))
    log.close()
    return summary


def _write_manifest(summary: RunSummary, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(asdict(summary), fh, indent=2, ensure_ascii=False)
