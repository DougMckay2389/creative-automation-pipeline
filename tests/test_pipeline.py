"""Tests for the behaviours that are expensive to get wrong.

Run:  python tests/test_pipeline.py      (no pytest required)
      python -m pytest tests/ -q         (if you have it)

These are not coverage theatre. Each one pins a claim the README makes, so
that if someone changes the code the claim either stays true or the test goes
red. The four that matter most:

* cost:      N deliverables must not cost N generations
* reuse:     a real asset on disk must never trigger a generative call
* fail-safe: a rule that raises must not silently pass an asset
* anti-cry-wolf: an on-brand image that has been through JPEG must still pass
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                                # noqa: E402

from pipeline.assets import AssetResolver                            # noqa: E402
from pipeline.brief import BriefError, load_brief                    # noqa: E402
from pipeline.checks import (Severity, Verdict, evaluate,            # noqa: E402
                             redmean_distance, rule_catalogue)
from pipeline.compose import crop_to_ratio                           # noqa: E402
from pipeline.localize import font_for                               # noqa: E402
from pipeline.providers import get_provider                          # noqa: E402
from pipeline.providers.base import GenerationRequest                # noqa: E402

BRIEF = "campaigns/aurora-spring.yaml"
BRAND = "brandkit/brand.yaml"


# --- brief -----------------------------------------------------------------

def test_brief_loads_and_expands():
    b = load_brief(BRIEF)
    assert len(b.products) >= 2
    assert len(b.ratios) >= 3
    assert b.variant_count == len(b.products) * len(b.markets) * len(b.ratios)
    assert len({v.id for v in b.variants()}) == b.variant_count


def test_sigv4_matches_the_published_aws_vector():
    """The signature is hand-rolled, so it is checked against AWS's own answer.

    A signature that is subtly wrong is indistinguishable from a bad key: you
    get a 403 that says nothing useful, in front of whoever you are demoing
    to. This is the worked GET Object example from the S3 documentation --
    fixed credentials, fixed date, published expected values. If the canonical
    request or the key derivation drifts, this fails here rather than there.
    """
    import hashlib
    import hmac as _hmac
    from pipeline.storage.s3 import (ALGORITHM, _sha256, canonical_request,
                                     signing_key)

    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    access = "AKIAIOSFODNN7EXAMPLE"
    region, amzdate, datestamp = "us-east-1", "20130524T000000Z", "20130524"
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    creq, signed = canonical_request("GET", "/test.txt", "", {
        "host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9",
        "x-amz-content-sha256": empty, "x-amz-date": amzdate}, empty)

    assert signed == "host;range;x-amz-content-sha256;x-amz-date"
    assert _sha256(creq.encode()) == \
        "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972", \
        "canonical request does not match the published example"

    scope = f"{datestamp}/{region}/s3/aws4_request"
    to_sign = "\n".join([ALGORITHM, amzdate, scope, _sha256(creq.encode())])
    sig = _hmac.new(signing_key(secret, datestamp, region, "s3"),
                    to_sign.encode(), hashlib.sha256).hexdigest()
    assert sig == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41", \
        f"signature does not match AWS's published value: {sig}"
    assert access  # named for readability of the vector above


def test_local_storage_round_trips_and_refuses_to_escape():
    """A product id comes from the brief, which is user input."""
    from pipeline.storage import get_storage
    from pipeline.storage.base import StorageError

    root = tempfile.mkdtemp()
    st = get_storage("local", root=root)
    obj = st.put("runs/r1/p/1x1/a.jpg", b"pixels", "image/jpeg")
    assert st.exists("runs/r1/p/1x1/a.jpg")
    assert st.get("runs/r1/p/1x1/a.jpg") == b"pixels"
    assert obj.size == 6 and obj.backend == "local"
    assert os.path.isfile(os.path.join(root, "runs", "r1", "p", "1x1", "a.jpg"))

    try:
        st.put("../../escaped.jpg", b"nope")
    except StorageError:
        pass
    else:
        raise AssertionError("a key climbing out of the root must be refused")


def test_a_storage_failure_does_not_lose_the_run():
    """The creatives already exist on disk and have already been checked.

    A network blip during the mirror is worth reporting; it is not worth
    throwing away eighteen finished files. The run must complete, the folder
    must be full, and the manifest must say what failed.
    """
    from pipeline.storage.base import Storage, StorageError

    class Broken(Storage):
        name = "broken"
        def put(self, key, data, content_type="application/octet-stream"):
            raise StorageError("connection reset")
        def uri(self, key):
            return f"broken://{key}"

    import pipeline.runner as runner
    real = runner.get_storage
    runner.get_storage = lambda name, **kw: Broken()
    try:
        out = tempfile.mkdtemp()
        summary = runner.run_campaign(BRIEF, provider_name="mock", quiet=True,
                                      out_root=out, cache_dir=os.path.join(out, "c"),
                                      storage_name="broken")
    finally:
        runner.get_storage = real

    assert len(summary.results) == summary.variants_planned, \
        "every deliverable must still be produced"
    assert summary.storage and len(summary.storage["errors"]) == summary.variants_planned, \
        "and every failure must be recorded rather than swallowed"
    assert summary.storage["objects"] == 0
    on_disk = sum(len([f for f in fs if f.endswith(".jpg")])
                  for _, _, fs in os.walk(summary.output_dir))
    assert on_disk == summary.variants_planned, "the local folder must be complete"


def test_brief_rejects_duplicate_ids():
    """A repeated id is silent data loss, not a style problem.

    output_path_for() keys on product, locale and ratio, so two aspect_ratios
    entries sharing an id write the same file: the run reports the full
    deliverable count and the folder holds fewer files than it claims. The
    brief form makes adding a second "16:9" one click, which is exactly why
    this is enforced on the data rather than in the editor.
    """
    import yaml
    base = yaml.safe_load(open(BRIEF, encoding="utf-8"))

    for field, dupe in (("aspect_ratios", {"id": "16:9", "width": 1920, "height": 1080}),
                        ("products", dict(base["products"][0])),
                        ("markets", dict(base["markets"][0]))):
        raw = yaml.safe_load(open(BRIEF, encoding="utf-8"))
        raw[field] = raw[field] + [dupe]
        path = os.path.join(tempfile.mkdtemp(), "dupe.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, allow_unicode=True)
        try:
            load_brief(path)
        except BriefError as exc:
            assert field in str(exc), f"{field}: error must name the field, got {exc}"
            assert "same output file" in str(exc), "and must say why it matters"
        else:
            raise AssertionError(f"{field}: a duplicated id must be rejected")


def test_brief_rejects_single_product():
    import yaml
    raw = yaml.safe_load(open(BRIEF, encoding="utf-8"))
    raw["products"] = raw["products"][:1]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh)
        p = fh.name
    try:
        try:
            load_brief(p)
            raise AssertionError("should have refused a single-product brief")
        except BriefError as exc:
            assert "products" in str(exc)
    finally:
        os.unlink(p)


def test_seeds_are_deterministic_per_variant():
    b = load_brief(BRIEF)
    once = {v.id: v.seed for v in b.variants()}
    twice = {v.id: v.seed for v in load_brief(BRIEF).variants()}
    assert once == twice, "same brief must produce the same seeds"


def test_seeds_are_stable_ACROSS_processes():
    """The bug this pins: builtin hash() is salted per interpreter run.

    A within-process check passes happily while every fresh run produces
    different seeds -- which silently breaks reproducibility AND makes the
    generated-asset cache miss every time. Only a subprocess catches it.
    """
    import subprocess
    code = ("import sys; sys.path.insert(0,'.');"
            "from pipeline.brief import stable_seed;"
            "print(stable_seed('aurora-spring-2026','velvet-matte-lip'))")
    runs = {subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=os.getcwd()).stdout.strip()
            for _ in range(3)}
    assert len(runs) == 1 and runs != {""}, f"seed differed across processes: {runs}"


# --- the cost claim --------------------------------------------------------

def test_generation_count_is_per_product_not_per_variant():
    """The headline claim: 18 deliverables, 1 generative call."""
    b = load_brief(BRIEF)
    assert b.generation_count < b.variant_count
    assert b.generation_count == sum(1 for p in b.products if not p.has_asset())


def test_existing_asset_is_never_generated():
    """A file on disk must short-circuit the provider entirely."""
    calls = []

    class Spy:
        name, model = "spy", "spy"

        def generate(self, req):
            calls.append(req)
            raise AssertionError("provider must not be called for an existing asset")

    b = load_brief(BRIEF)
    have = next(p for p in b.products if p.has_asset())
    with tempfile.TemporaryDirectory() as tmp:
        r = AssetResolver(Spy(), cache_dir=tmp)
        m = r.resolve(have, seed=1)
    assert m.origin == "brief" and not calls


def test_regen_ignores_the_asset_on_disk_and_the_cache():
    """--regen must actually produce a NEW picture, not just spend a call.

    Three things have to be true together, and two of them are easy to get
    wrong in a way that still looks like it worked:

      * a product with an asset on disk must be generated anyway -- otherwise
        editing its subject and surface does nothing at all, because the disk
        asset short-circuits the resolver before the prompt is ever built;
      * the cache must be bypassed;
      * the SEED must move. The provider honours the seed, so regenerating at
        a fixed seed spends a real call to get back the picture you already
        had. That is the failure that looks like the flag is broken.
    """
    import yaml
    tmp = tempfile.mkdtemp()
    raw = yaml.safe_load(open(BRIEF, encoding="utf-8"))
    # give BOTH products an asset that really exists, so nothing would
    # normally be generated at all
    shot = os.path.join(tmp, "on-disk.png")
    Image.new("RGB", (64, 64), (200, 120, 90)).save(shot)
    for prod in raw["products"]:
        prod["asset"] = shot
    path = os.path.join(tmp, "brief.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, allow_unicode=True)

    brief = load_brief(path)
    provider = get_provider("mock")

    normal = AssetResolver(provider, cache_dir=os.path.join(tmp, "c1"))
    assert normal.resolve(brief.products[0], 7).origin == "brief", \
        "without --regen an asset on disk must be reused"

    forced = AssetResolver(provider, cache_dir=os.path.join(tmp, "c2"), force=True)
    first = forced.resolve(brief.products[0], 7)
    assert first.origin == "generated", "with --regen the disk asset must be ignored"

    # same seed twice -> the cache is bypassed, but the pixels are identical,
    # which is exactly why the runner salts the seed with the run id.
    again = forced.resolve(brief.products[0], 7)
    assert again.origin == "generated", "the cache must be bypassed too"
    assert open(first.path, "rb").read() == open(again.path, "rb").read(), \
        "a fixed seed is expected to reproduce the same pixels"

    # a different seed -> a genuinely different picture
    moved = forced.resolve(brief.products[0], 8)
    assert open(moved.path, "rb").read() != open(first.path, "rb").read(), \
        "moving the seed must actually change the image"


def test_cache_prevents_a_second_generation():
    b = load_brief(BRIEF)
    missing = next(p for p in b.products if not p.has_asset())
    prov = get_provider("mock")

    class Counting:
        name, model = prov.name, prov.model

        def __init__(self):
            self.n = 0

        def generate(self, req):
            self.n += 1
            return prov.generate(req)

    c = Counting()
    with tempfile.TemporaryDirectory() as tmp:
        r = AssetResolver(c, cache_dir=tmp)
        a = r.resolve(missing, seed=7)
        bres = r.resolve(missing, seed=7)
    assert c.n == 1, f"expected one generation, got {c.n}"
    assert a.origin == "generated" and bres.origin == "cache"


# --- providers -------------------------------------------------------------

def test_mock_provider_is_deterministic():
    p = get_provider("mock")
    req = GenerationRequest(prompt="a matte lipstick on stone", seed=1234)
    assert p.generate(req).png_bytes == p.generate(req).png_bytes


def test_mock_provider_needs_no_credentials():
    assert "mock" in __import__("pipeline.providers", fromlist=["x"]).available_providers()


# --- composition -----------------------------------------------------------

def test_crop_hits_exact_delivery_dimensions():
    src = Image.new("RGB", (1600, 1600), (200, 190, 180))
    for w, h in ((1080, 1080), (1080, 1920), (1920, 1080)):
        out = crop_to_ratio(src, w, h)
        assert out.size == (w, h), f"expected {(w, h)}, got {out.size}"


# --- localization ----------------------------------------------------------

def test_japanese_resolves_a_font_that_can_draw_japanese():
    import yaml
    typo = (yaml.safe_load(open(BRAND, encoding="utf-8")) or {}).get("typography", {})
    rf = font_for("ja-JP", "肌が、目を覚ます。", typo)
    assert rf.is_cjk, "ja-JP must resolve to a CJK-capable face"
    name = rf.family.lower()
    assert any(k in name for k in ("cjk", "gothic", "noto", "hiragino", "yu", "meiryo")), name


def test_latin_market_uses_the_latin_stack():
    import yaml
    typo = (yaml.safe_load(open(BRAND, encoding="utf-8")) or {}).get("typography", {})
    rf = font_for("en-US", "Your skin, wide awake.", typo)
    assert not rf.is_cjk


# --- checks ----------------------------------------------------------------

class FakeComp:
    def __init__(self, **kw):
        self.width = kw.get("width", 1080)
        self.height = kw.get("height", 1080)
        self.message = kw.get("message", "Your skin, wide awake.")
        self.font_family = "x"
        self.message_px_height = kw.get("message_px_height", 80.0)
        self.message_area = 10000.0
        self.logo_box = kw.get("logo_box", (10, 10, 200, 60))
        self.logo_clearspace_ratio = kw.get("logo_clearspace_ratio", 1.0)
        self.dominant_hex = kw.get("dominant_hex", ["#101820", "#e8dfd3"])
        self.warnings = kw.get("warnings", [])


class FakeRatio:
    def __init__(self, w=1080, h=1080):
        self.width, self.height, self.id = w, h, "1:1"


class FakeVariant:
    def __init__(self, w=1080, h=1080):
        self.ratio = FakeRatio(w, h)


def _brand():
    import yaml
    return yaml.safe_load(open(BRAND, encoding="utf-8"))


def test_clean_creative_passes_with_no_findings():
    r = evaluate(FakeComp(), FakeVariant(), _brand(), [])
    assert r.verdict is Verdict.PASS, [f.as_dict() for f in r.findings]


def test_wrong_dimensions_block():
    r = evaluate(FakeComp(width=999), FakeVariant(), _brand(), [])
    assert r.verdict is Verdict.BLOCK
    assert any(f.rule_id == "SPEC-001" for f in r.findings)


def test_missing_logo_blocks_and_routes_to_brand():
    r = evaluate(FakeComp(logo_box=None), FakeVariant(), _brand(), [])
    assert r.verdict is Verdict.BLOCK
    assert "brand" in r.routing()


def test_prohibited_term_blocks_and_routes_to_legal():
    r = evaluate(FakeComp(message="Clinically proven glow"), FakeVariant(),
                 _brand(), ["clinically proven"])
    assert r.verdict is Verdict.BLOCK
    assert "legal" in r.routing()


def test_illegible_type_flags_but_does_not_block():
    """A small headline is a design problem, not a legal one. Blocking it
    would train the team to route around the gate."""
    r = evaluate(FakeComp(message_px_height=4.0), FakeVariant(), _brand(), [])
    assert r.verdict is Verdict.REVIEW
    assert all(f.severity is not Severity.BLOCKER for f in r.findings)


def test_palette_tolerance_survives_a_jpeg_round_trip():
    """The anti-cry-wolf test. Approved swatches nudged a few levels -- as any
    JPEG encode will do -- must NOT fire BRAND-001."""
    nudged = ["#12181e", "#e6ded1", "#c26b4c"]
    r = evaluate(FakeComp(dominant_hex=nudged), FakeVariant(), _brand(), [])
    assert not any(f.rule_id == "BRAND-001" for f in r.findings), \
        [f.as_dict() for f in r.findings]


def test_wildly_off_palette_is_flagged_but_only_as_minor():
    r = evaluate(FakeComp(dominant_hex=["#ff00ff", "#00ff00", "#ffff00"]),
                 FakeVariant(), _brand(), [])
    assert any(f.rule_id == "BRAND-001" for f in r.findings)
    assert r.verdict is Verdict.REVIEW


def test_redmean_is_not_naive_euclidean():
    """Sanity-check the distance function actually weights channels."""
    d1 = redmean_distance((255, 0, 0), (250, 0, 0))
    d2 = redmean_distance((0, 0, 255), (0, 0, 250))
    assert d1 != d2, "redmean must weight red and blue differently"


def test_a_raising_rule_never_silently_passes():
    """Fail-open is the one unacceptable failure mode."""
    bad = FakeComp()
    bad.dominant_hex = None          # will explode inside the palette rule
    r = evaluate(bad, FakeVariant(), _brand(), [])
    assert r.verdict is not Verdict.PASS


def test_rule_ids_are_unique():
    ids = rule_catalogue()
    assert len(ids) == len(set(ids))


# --- end to end ------------------------------------------------------------

def test_full_run_produces_every_deliverable():
    from pipeline.runner import run_campaign
    tmp = tempfile.mkdtemp()
    try:
        # isolated cache dir: otherwise a warm .cache from a previous run
        # makes this assert 0 generations and the test becomes meaningless
        s = run_campaign(BRIEF, BRAND, provider_name="mock", out_root=tmp,
                         quiet=True, cache_dir=os.path.join(tmp, "cache"))
        assert s.variants_planned == 18
        assert s.generative_calls == 1, "must not generate per variant"
        jpgs = [f for _r, _d, fs in os.walk(tmp) for f in fs if f.endswith(".jpg")]
        assert len(jpgs) == 18, f"expected 18 creatives, found {len(jpgs)}"
        assert sum(s.counts.values()) == 18
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok    {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:                                   # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
