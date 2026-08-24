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
from .providers.base import VideoRequest
from .providers.cloudflare import CloudflareProvider
from .discovery import CHANNELS, DiscoveryRequest, discover
from .providers import default_provider, get_provider
from .runner import load_brand
from .storage import StorageError, default_storage, get_storage


def _noop(_rec: dict) -> None:
    pass


def plan(brief: Brief, product_id: str, days: int, images_per_day: int,
         videos_per_day: int, root: str = ".", channels: list[str] | None = None,
         start: str = "", backend: str = "", mode: str = "viral",
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

    out = {"product": pdict, "markets": {}, "backends": {}, "mode": mode,
           "ffmpeg": motion.available(),
           "ffmpeg_note": motion.why_unavailable()}

    for market in brief.markets:
        disc: dict[str, dict] = {}
        # The Internal engine does not crawl. Its evidence is our own account
        # data, which `history` already carries -- running four scrapers and
        # then ignoring their output would be a slow way to produce the same
        # document, and would put competitor rows on a tab that is explicitly
        # not about competitors.
        for ch in (channels if mode == "viral" else []):
            on_event({"event": "discover", "product": product.id,
                      "locale": market.locale, "channel": ch})
            req = DiscoveryRequest(
                product_id=product.id, product_name=product.name,
                category=pdict["category"], locale=market.locale,
                region=market.region, audience=market.audience,
                channel=ch, limit=8)
            d = discover(req, root=root, backend=backend)
            # The market trend that decides the LIGHTING clause of the prompt.
            # It comes from insights rather than from the crawl -- a scraped
            # post tells you what one account did, a trend tells you what the
            # market is reacting to, and those are different questions.
            d["top_trend"] = ((history or {}).get(market.locale, {})
                              .get(ch, {}).get("top_trend") or {})
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
            ratios_available=ratios, channels=channels, start=start, mode=mode)
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
        video_model: str = "", storage_name: str = "", on_event=_noop) -> dict:
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
    # Fired once, immediately -- the only way the UI can build a correct
    # /out/ URL for a slot WHILE the run is still going. `base` is decided
    # here, this call, from `stamp`; the plan the client already has has no
    # way to know it, and the first "slot" event is often minutes away.
    on_event({"event": "output_dir", "product": product_id,
              "output_dir": os.path.relpath(base, root).replace("\\", "/")})

    # Object storage, if one is configured -- same "empty means decide for
    # me" rule as runner.py: with S3 credentials in the environment, mirror
    # there automatically rather than requiring a flag nobody remembers to
    # pass. Keys are deterministic (backups/<path relative to root>), not the
    # classic runner's random share-token prefix -- this mirror exists so
    # Doug can find a given local file again in the bucket, not to hand out a
    # reviewable link, so there is nothing to keep unguessable here.
    if not storage_name:
        storage_name = default_storage()
    store = None
    storage_errors: list[str] = []
    if storage_name and storage_name != "local":
        store = get_storage(storage_name)
        on_event({"event": "storage-open", "product": product_id,
                  "backend": store.name, "target": store.uri("backups/")})

    def _mirror(local_path: str, content_type: str) -> tuple[str, str]:
        """Upload one file, return (the s3:// identifier, an openable link),
        or ("", "") and a shrug. A failed upload must never lose a render
        that already succeeded and already passed its checks -- the local
        copy is the deliverable, the bucket is a bonus. `backups/` sits
        outside PUBLIC_ROOT, so share_url signs a time-limited link rather
        than handing out a permanent public one -- these are Doug's own
        working files, not something to publish."""
        if store is None:
            return "", ""
        try:
            rel_key = os.path.relpath(local_path, root).replace("\\", "/")
            with open(local_path, "rb") as fh:
                obj = store.put(f"backups/{rel_key}", fh.read(), content_type)
            return obj.uri, store.share_url(obj.key)
        except StorageError as exc:
            storage_errors.append(f"{local_path}: {exc}")
            on_event({"event": "storage-error", "product": product_id,
                      "path": local_path, "error": str(exc)[:200]})
            return "", ""

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
                    comp = composer.compose(master.path, variant, still,
                                            layered_path=still[:-4] + ".psd")
                    res = evaluate(comp, variant, brand, brief.prohibited_terms)
                    slot["produced"] = os.path.relpath(still, base).replace("\\", "/")  # relative to THIS run's out_dir, matching runner.py's VariantResult.path -- the client builds an /out/ URL as out_dir + this path.
                    if comp.layered:
                        slot["layered"] = os.path.relpath(
                            comp.layered, base).replace("\\", "/")
                    slot["verdict"] = res.verdict.value
                    slot["score"] = compliance_score(res.findings)
                    slot["findings"] = [f.as_dict() for f in res.findings]
                    slot["stored_uri"], slot["share_url"] = _mirror(
                        still, "image/jpeg")
                    if comp.layered and os.path.isfile(comp.layered):
                        slot["layered_uri"], slot["layered_share"] = _mirror(
                            comp.layered, "image/vnd.adobe.photoshop")
                except Exception as exc:                      # noqa: BLE001
                    traceback.print_exc()
                    slot["error"] = f"{type(exc).__name__}: {exc}"
                    on_event({"event": "slot_error", "slot": slot["id"],
                              "error": slot["error"]})
                    results.append(slot)
                    continue

                if slot["kind"] == "video":
                    if render_video and video_model:
                        # Image-to-video, always -- `still` is the exact file
                        # `evaluate()` just scored a few lines up, so the
                        # checks have seen every pixel Veo starts from. See
                        # VideoRequest in pipeline/providers/base.py.
                        try:
                            mp4 = still[:-4] + ".mp4"
                            vt0 = time.monotonic()
                            with open(still, "rb") as fh:
                                still_bytes = fh.read()
                            vprov = CloudflareProvider()
                            vres = vprov.generate_video(
                                VideoRequest(prompt=variant.market.message
                                            or product.name,
                                            reference_png=still_bytes,
                                            seconds=slot.get("seconds") or 6,
                                            aspect_ratio=ratio.id),
                                model=video_model)
                            with open(mp4, "wb") as fh:
                                fh.write(vres.video_bytes)
                            slot["video"] = os.path.relpath(mp4, base).replace("\\", "/")
                            slot["video_seconds"] = vres.seconds
                            slot["video_bytes"] = len(vres.video_bytes)
                            slot["video_model"] = vres.model
                            slot["video_stored_uri"], slot["video_share_url"] = _mirror(mp4, "video/mp4")
                            on_event({"event": "video", "product": product_id,
                                      "locale": locale, "channel": ch,
                                      "slot": slot["id"], "model": vres.model,
                                      "latency_s": round(time.monotonic() - vt0, 1)})
                        except Exception as exc:                # noqa: BLE001
                            slot["video_error"] = f"{type(exc).__name__}: {exc}"
                            on_event({"event": "video_error", "slot": slot["id"],
                                      "error": slot["video_error"]})
                    elif render_video and motion.available():
                        mp4 = still[:-4] + ".mp4"
                        try:
                            info = motion.render(still, mp4, channel=ch,
                                                 seconds=slot.get("seconds") or 0)
                            slot["video"] = os.path.relpath(
                                info["path"], base).replace("\\", "/")
                            slot["video_seconds"] = info["seconds"]
                            slot["video_bytes"] = info["bytes"]
                            slot["video_stored_uri"], slot["video_share_url"] = \
                                _mirror(mp4, "video/mp4")
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
                          "message": mkt.message,
                          "layered": slot.get("layered", ""),
                          "stored_uri": slot.get("stored_uri", ""),
                          "share_url": slot.get("share_url", ""),
                          "layered_uri": slot.get("layered_uri", ""),
                          "layered_share": slot.get("layered_share", ""),
                          "video_stored_uri": slot.get("video_stored_uri", ""),
                          "video_share_url": slot.get("video_share_url", ""),
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
        "storage": ({"backend": store.name, "target": store.uri("backups/"),
                     "errors": storage_errors} if store is not None else None),
    }
    with open(os.path.join(base, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    return summary
