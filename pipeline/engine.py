"""The social content engine: brief in, a scheduled channel campaign out.

The order of operations is the whole design, so it is worth stating plainly:

    duration + daily volume    ->  dated slots, spread across channels
    region + audience + product ->  discovery, per channel
    look-alikes + our history  ->  ONE strategy JSON per product x market
    strategy                   ->  one master per channel  (the only paid step)
    master                     ->  every slot composed locally, then checked
    approved still             ->  the video slots rendered from it

Two properties are load-bearing and both come from the existing pipeline
rather than being reinvented here:

* **Generate once, compose many.** A fortnight of daily posts across four
  channels is a lot of files and almost no generative calls -- one per
  channel that has work, because the surface is what costs money and the
  surface is per channel. Adding a day, a ratio or a caption is free.
* **Nothing is auto-approved.** Every still goes through the same
  `evaluate()` the CLI uses, and every video is rendered from a still that
  already has a verdict, so a video can never carry a pixel the checks have
  not seen.

Events are emitted through `on_event` in the same shape `runner.py` uses, so
the app can follow an engine run with the machinery it already has.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import time
import traceback

from . import motion, strategy
from .assets import AssetResolver
from .brief import Brief, Market, Product, Variant, stable_seed
from .checks import compliance_score, evaluate
from .compose import Composer
from .discovery import CHANNELS, DiscoveryRequest, discover
from .providers import default_provider, get_provider
from .runner import load_brand


def _noop(_rec: dict) -> None:
    pass


def plan(brief: Brief, product_id: str, days: int, images_per_day: int,
         videos_per_day: int, root: str = ".", channels: list[str] | None = None,
         start: str = "", backend: str = "",
         history: dict | None = None, on_event=_noop) -> dict:
    """Discovery + strategy for one product, across every market in the brief.

    Costs nothing generative. This is the reviewable artefact that exists so
    somebody can say "wrong audience" before any money is spent -- the same
    reason `run.py plan` exists for the base pipeline.
    """
    channels = channels or list(CHANNELS)
    product = next((p for p in brief.products if p.id == product_id), None)
    if product is None:
        raise ValueError(f"no product '{product_id}' in this brief")

    pdict = dataclasses.asdict(product)
    pdict["category"] = strategy._category(pdict)
    ratios = [r.id for r in brief.ratios]

    out = {"product": pdict, "markets": {}, "backends": {},
           "ffmpeg": motion.available(),
           "ffmpeg_note": motion.why_unavailable()}

    for market in brief.markets:
        disc: dict[str, dict] = {}
        for ch in channels:
            on_event({"event": "discover", "product": product.id,
                      "locale": market.locale, "channel": ch})
            req = DiscoveryRequest(
                product_id=product.id, product_name=product.name,
                category=pdict["category"], locale=market.locale,
                region=market.region, audience=market.audience,
                channel=ch, limit=8)
            d = discover(req, root=root, backend=backend)
            disc[ch] = d
            out["backends"][ch] = d.get("backend")
            on_event({"event": "discovered", "product": product.id,
                      "locale": market.locale, "channel": ch,
                      "count": len(d.get("lookalikes") or []),
                      "backend": d.get("backend"),
                      "synthetic": d.get("synthetic"),
                      "cached": d.get("cached")})

        doc = strategy.build(
            product=pdict,
            market=dataclasses.asdict(market),
            discovery_by_channel=disc,
            history_by_channel=(history or {}).get(market.locale, {}),
            days=days, images_per_day=images_per_day,
            videos_per_day=videos_per_day,
            ratios_available=ratios, channels=channels, start=start)
        out["markets"][market.locale] = doc
        on_event({"event": "strategy", "product": product.id,
                  "locale": market.locale,
                  "slots": doc["totals"]["slots"],
                  "calls": doc["totals"]["generative_calls"]})

    out["totals"] = {
        "slots": sum(m["totals"]["slots"] for m in out["markets"].values()),
        "images": sum(m["totals"]["images"] for m in out["markets"].values()),
        "videos": sum(m["totals"]["videos"] for m in out["markets"].values()),
        "generative_calls": sum(m["totals"]["generative_calls"]
                                for m in out["markets"].values()),
    }
    return out


def run(brief: Brief, planned: dict, out_root: str, root: str = ".",
        provider_name: str = "", render_video: bool = True,
        on_event=_noop) -> dict:
    """Execute a plan. The only place in the engine that spends anything."""
    product_id = planned["product"]["id"]
    product = next(p for p in brief.products if p.id == product_id)
    ratios = {r.id: r for r in brief.ratios}
    brand = load_brand(os.path.join(root, "brandkit", "brand.yaml"))

    name = provider_name or default_provider()
    provider = get_provider(name)
    resolver = AssetResolver(provider,
                             cache_dir=os.path.join(root, ".cache", "masters"))
    composer = Composer(brand)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(out_root, f"engine-{stamp}", product_id)
    os.makedirs(base, exist_ok=True)

    results: list[dict] = []
    calls = 0
    t0 = time.monotonic()

    for locale, doc in planned["markets"].items():
        market = next(m for m in brief.markets if m.locale == locale)
        strategy.save(doc, os.path.join(base, locale, "strategy.json"))

        for ch, cdata in doc["channels"].items():
            slots = cdata.get("slots") or []
            if not slots:
                continue

            # One master per channel. The surface is what a generative call
            # buys, and the surface is per channel -- every date, ratio and
            # caption below is composed from this one image locally.
            on_event({"event": "master", "product": product_id,
                      "locale": locale, "channel": ch,
                      "surface": cdata["surface"]})
            trial = dataclasses.replace(product, surface=cdata["surface"])
            master = resolver.resolve(
                trial, stable_seed(brief.campaign_id, trial.id,
                                   cdata["surface"], ch))
            if master.origin in ("generated", "resurfaced"):
                calls += 1
            on_event({"event": "mastered", "product": product_id,
                      "locale": locale, "channel": ch,
                      "origin": master.origin, "calls": calls})

            for slot in slots:
                ratio = ratios.get(slot["ratio"]) or brief.ratios[0]
                # The slot's caption is the market message for this post. The
                # composer draws whatever the market carries, so the caption
                # is injected by replacing the market rather than by teaching
                # the composer a second source of copy.
                mkt = dataclasses.replace(market, message=slot["caption"]
                                          or market.message)
                variant = Variant(trial, mkt, ratio)
                rel = os.path.join(locale, ch, slot["date"],
                                   ratio.id.replace(":", "x"))
                still = os.path.join(base, rel, f"{slot['id']}.jpg")
                os.makedirs(os.path.dirname(still), exist_ok=True)

                try:
                    comp = composer.compose(master.path, variant, still)
                    res = evaluate(comp, variant, brand, brief.prohibited_terms)
                    slot["produced"] = os.path.relpath(still, root).replace("\\", "/")
                    slot["verdict"] = res.verdict.value
                    slot["score"] = compliance_score(res.findings)
                    slot["findings"] = [f.as_dict() for f in res.findings]
                except Exception as exc:                      # noqa: BLE001
                    traceback.print_exc()
                    slot["error"] = f"{type(exc).__name__}: {exc}"
                    on_event({"event": "slot_error", "slot": slot["id"],
                              "error": slot["error"]})
                    results.append(slot)
                    continue

                if slot["kind"] == "video":
                    if render_video and motion.available():
                        mp4 = still[:-4] + ".mp4"
                        try:
                            info = motion.render(still, mp4, channel=ch,
                                                 seconds=slot.get("seconds") or 0)
                            slot["video"] = os.path.relpath(
                                info["path"], root).replace("\\", "/")
                            slot["video_seconds"] = info["seconds"]
                            slot["video_bytes"] = info["bytes"]
                        except motion.MotionError as exc:
                            slot["video_error"] = str(exc)
                    else:
                        # Planned, captioned, and honestly unrendered. The
                        # slot is not quietly downgraded to a still.
                        slot["video_error"] = motion.why_unavailable() or \
                            "video rendering was switched off for this run"

                results.append(slot)
                on_event({"event": "slot", "product": product_id,
                          "locale": locale, "channel": ch,
                          "slot": slot["id"], "kind": slot["kind"],
                          "ratio": slot["ratio"], "date": slot["date"],
                          "verdict": slot.get("verdict", ""),
                          "score": slot.get("score", 0),
                          "path": slot.get("produced", ""),
                          "video": slot.get("video", ""),
                          "out_dir": os.path.relpath(base, root).replace("\\", "/")})

        # Rewritten after execution so the saved strategy carries what each
        # slot actually produced, not just what it intended to.
        strategy.save(doc, os.path.join(base, locale, "strategy.json"))

    counts: dict[str, int] = {}
    for s in results:
        counts[s.get("verdict", "error")] = counts.get(s.get("verdict", "error"), 0) + 1

    summary = {
        "product": product_id,
        "output_dir": os.path.relpath(base, root).replace("\\", "/"),
        "slots": len(results),
        "images": sum(1 for s in results if s["kind"] == "image"),
        "videos": sum(1 for s in results if s.get("video")),
        "videos_planned": sum(1 for s in results if s["kind"] == "video"),
        "generative_calls": calls,
        "provider": getattr(provider, "name", name),
        "counts": counts,
        "seconds": round(time.monotonic() - t0, 1),
        "ffmpeg": motion.available(),
        "results": results,
    }
    with open(os.path.join(base, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    return summary
