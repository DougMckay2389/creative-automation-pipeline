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
import secrets
import time
from dataclasses import asdict, dataclass, field

import yaml

from .assets import AssetResolver, MasterAsset
from .brief import Brief, Variant, load_brief, stable_seed
from .storage import PUBLIC_ROOT, StorageError, default_storage, get_storage
from .checks import (Finding, Verdict, compliance_score, evaluate,
                     preflight_brief)
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
    stored_uri: str = ""          # where the mirror put it; "" when local-only
    # An https link a person can actually open. Separate from stored_uri
    # because `s3://bucket/key` is an identifier and not a URL -- the two look
    # interchangeable in a manifest and exactly one of them works in a browser.
    share_url: str = ""
    # The layered source beside the deliverable, and where it landed.
    layered: str = ""
    layered_uri: str = ""
    layered_share: str = ""
    # 0-100 conformance to the rules in checks.py. Recorded next to the
    # verdict, never instead of it -- the verdict decides shipping, the score
    # only orders a review queue.
    score: int = 100
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
    storage: dict | None = None   # backend/target/counts; None when local-only


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
                 cache_dir: str = ".cache/masters", on_event=None,
                 force_generate: bool = False,
                 storage_name: str = "",
                 model: str = "") -> RunSummary:
    t0 = time.monotonic()
    run_id = time.strftime("%Y%m%d-%H%M%S")

    # Empty means "decide for me", which is the default: if the environment
    # has object-storage credentials, mirror there. Passing "local"
    # explicitly still opts out. This is deliberately not the same as None --
    # the caller saying "local" is a decision and must be honoured.
    if not storage_name:
        storage_name = default_storage()

    # The unguessable half of every shared link.
    #
    # `secrets`, not `random`: this token is the ONLY thing standing between a
    # public-prefix object and anyone on the internet, and `random` is a
    # Mersenne Twister seeded from the clock -- observing a few outputs
    # recovers its state and therefore every other run's token. `token_urlsafe`
    # reads from the OS CSPRNG. 24 bytes is 32 characters and about 190 bits,
    # which is not brute-forceable and is still short enough to paste.
    #
    # Per RUN rather than per object: one link gives a reviewer the whole run,
    # and a single token is one thing to revoke by deleting one prefix.
    share_token = secrets.token_urlsafe(24)

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
    # Only pass a model if one was chosen. Sending model="" would override the
    # adapter's own default with nothing.
    if model:
        kwargs["model"] = model
    provider = get_provider(provider_name, **kwargs)
    resolver = AssetResolver(provider, cache_dir=cache_dir, log=log,
                             force=force_generate)

    # Object storage, if one is configured.
    #
    # A MIRROR, never a replacement: the task requires outputs saved to a
    # folder organised by product and aspect ratio, so the local tree is
    # written either way and the backend receives a copy. Opened HERE, before
    # any generative call, so a bad key or a missing bucket fails at once
    # rather than after eighteen paid-for renders.
    #
    # Everything for this run lands under ONE key prefix, and that prefix
    # carries the random token. `public/` is the only root the bucket policy
    # exposes, so putting the run there is what makes the links openable; the
    # token is what keeps them private. Both halves are needed and neither is
    # sufficient alone.
    store = None
    key_prefix = f"{PUBLIC_ROOT}/{share_token}"
    if storage_name and storage_name != "local":
        store = get_storage(storage_name)
        log("storage-open", backend=store.name,
            target=store.uri(key_prefix + "/"),
            share=store.share_url(f"{key_prefix}/manifest.json"))
    composer = Composer(brand)
    # Where per-stage thumbnails go. Inside the run folder, so a run is still
    # one self-contained directory you can zip, and named with a leading
    # underscore so it sorts away from the deliverables somebody came here to
    # find.
    stage_dir = os.path.join(out_dir, "_stages")

    # --- 3. one master per product ----------------------------------------
    masters: dict[str, MasterAsset] = {}
    for p in brief.products:
        # The seed has to move for a forced regeneration to mean anything.
        #
        # The provider honours it -- two calls at a fixed seed return
        # byte-identical images, which is measured and is the whole basis for
        # "the same brief regenerates the same pixels". So forcing a
        # regeneration WITHOUT changing the seed spends a real generative call
        # to receive the picture you already had, and looks from the outside
        # like the button does nothing.
        #
        # Salted with the run id rather than randomised, so the seed still
        # lands in the manifest and any image here can be reproduced exactly
        # later. Two runs inside the same second would collide; that is a
        # second of resolution against a call that takes several.
        seed = (stable_seed(brief.campaign_id, p.id, run_id) if force_generate
                else stable_seed(brief.campaign_id, p.id))
        masters[p.id] = resolver.resolve(p, seed)

    # "resurfaced" counts as a generative call, because it IS one -- a paid,
    # rate-limited round trip to the model. Counting only "generated" made the
    # cost line under-report by one per resurfaced product, and a report that
    # quietly understates spend is worse than no report: the entire argument
    # this pipeline makes is about how few model calls it takes, and that
    # argument is only worth anything if the number is honest.
    PAID = ("generated", "resurfaced")
    gen = sum(1 for m in masters.values() if m.origin in PAID)
    resurfaced = sum(1 for m in masters.values() if m.origin == "resurfaced")
    from_brief = sum(1 for m in masters.values() if m.origin == "brief")
    from_cache = sum(1 for m in masters.values() if m.origin == "cache")
    log("masters-ready", generated=gen, resurfaced=resurfaced,
        from_brief=from_brief, from_cache=from_cache)

    # --- 4 + 5. compose and check every variant ---------------------------
    results: list[VariantResult] = []
    uploaded: list = []
    storage_errors: list[str] = []
    counts = {"pass": 0, "review": 0, "block": 0}
    for v in brief.variants():
        master = masters[v.product.id]
        path = output_path_for(out_dir, v)
        log("variant-start", variant=v.id, product=v.product.id,
            locale=v.market.locale, ratio=v.ratio.id)
        comp = composer.compose(
            master.path, v, path,
            # `_v=v` binds the loop variable at definition time. Without it
            # every closure would report the LAST variant, because Python
            # closes over the name and not the value -- a classic way to get
            # eighteen stage events that all claim to be the same creative.
            on_stage=(lambda name, params=None, ms=0.0, preview="", _v=v:
                      log("stage", stage=name, variant=_v.id,
                          params=params or {}, ms=round(ms, 1),
                          preview=preview)),
            stage_dir=stage_dir, stage_key=v.id,
            # A .psd beside every JPEG, with the copy on its own layer. The
            # flat file is the deliverable; this is what makes the last mile
            # -- a market that wants its own headline -- somebody else's to
            # drive without coming back through the tool.
            layered_path=path[:-4] + ".psd")
        res = evaluate(comp, v, brand, brief.prohibited_terms)
        log("stage", stage="checks", variant=v.id,
            rules=[f["rule"] for f in [x.as_dict() for x in res.findings]])
        counts[res.verdict.value] += 1
        rel = os.path.relpath(path, out_dir).replace(os.sep, "/")
        stored_uri = ""
        share_url = ""
        layered_uri = ""
        layered_share = ""
        if store is not None:
            # An upload failure must not lose the run. The creative already
            # exists on disk and has already been checked; a network blip is
            # something to report, not a reason to discard eighteen files.
            try:
                with open(path, "rb") as fh:
                    obj = store.put(f"{key_prefix}/{rel}", fh.read(), "image/jpeg")
                stored_uri = obj.uri
                # The openable link, recorded per object. `s3://` is an
                # identifier and nothing more -- paste it in a browser and
                # nothing happens -- so the thing a person actually needs is
                # stored alongside it rather than reconstructed later by
                # somebody who has to know the sharing rules.
                share_url = store.share_url(obj.key)
                uploaded.append(obj)
                # The layered file goes up too. A share link that only ever
                # points at a flattened JPEG makes the PSD a thing you have to
                # know to ask for, and the person who needs it most is the one
                # who was sent a link rather than given the folder.
                if comp.layered and os.path.isfile(comp.layered):
                    lrel = os.path.relpath(comp.layered, out_dir).replace(os.sep, "/")
                    with open(comp.layered, "rb") as fh:
                        lobj = store.put(f"{key_prefix}/{lrel}", fh.read(),
                                         "image/vnd.adobe.photoshop")
                    layered_uri = lobj.uri
                    layered_share = store.share_url(lobj.key)
                    uploaded.append(lobj)
            except StorageError as exc:
                storage_errors.append(f"{rel}: {exc}")
                log("storage-error", variant=v.id, error=str(exc)[:200])

        results.append(VariantResult(
            variant_id=v.id, product_id=v.product.id, locale=v.market.locale,
            ratio=v.ratio.id, path=os.path.relpath(path, out_dir),
            stored_uri=stored_uri, share_url=share_url,
            layered=(os.path.relpath(comp.layered, out_dir)
                     if comp.layered else ""),
            layered_uri=layered_uri, layered_share=layered_share,
            verdict=res.verdict.value, score=compliance_score(res.findings),
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
            score=compliance_score(res.findings),
            product=v.product.id, locale=v.market.locale, ratio=v.ratio.id,
            message=v.market.message, out_dir=out_dir,
            path=os.path.relpath(path, out_dir).replace(os.sep, "/"),
            stored_uri=stored_uri, share_url=share_url,
            layered=(os.path.relpath(comp.layered, out_dir).replace(os.sep, "/")
                     if comp.layered else ""),
            layered_uri=layered_uri, layered_share=layered_share,
            findings=[f.as_dict() for f in res.findings])

    summary = RunSummary(
        run_id=run_id, campaign_id=brief.campaign_id, provider=provider_name,
        model=getattr(provider, "model", ""), started_at=run_id,
        duration_s=time.monotonic() - t0,
        variants_planned=brief.variant_count, generative_calls=gen,
        reused_from_brief=from_brief, reused_from_cache=from_cache,
        counts=counts, preflight=[f.as_dict() for f in pre],
        results=results, output_dir=out_dir,
        storage=({"backend": store.name,
                  "target": store.uri(key_prefix + "/"),
                  # The token is recorded so the run can be found again, and
                  # so deleting exactly this prefix is the revocation story.
                  "share_token": share_token,
                  "share_url": store.share_url(f"{key_prefix}/manifest.json"),
                  "public": getattr(store, "is_public_key", lambda k: False)(
                      f"{key_prefix}/manifest.json"),
                  "objects": len(uploaded),
                  "bytes": sum(o.size for o in uploaded),
                  "errors": storage_errors} if store is not None else None))

    _write_manifest(summary, out_dir)
    # The manifest goes up too. A bucket full of creatives with no record of
    # which brief, model and seed produced them is an archive nobody can audit,
    # which is the opposite of the point of putting them there.
    #
    # Written LAST, deliberately: it is the only object that lists every
    # share_url, so it is the single link worth handing to somebody, and it
    # cannot be assembled until every variant has been uploaded.
    if store is not None:
        try:
            with open(os.path.join(out_dir, "manifest.json"), "rb") as fh:
                store.put(f"{key_prefix}/manifest.json", fh.read(), "application/json")
        except StorageError as exc:
            log("storage-error", error=f"manifest: {exc}"[:200])
    log("run-end", **counts, generative_calls=gen,
        duration_s=round(summary.duration_s, 2))
    log.close()
    return summary


def _write_manifest(summary: RunSummary, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(asdict(summary), fh, indent=2, ensure_ascii=False)
