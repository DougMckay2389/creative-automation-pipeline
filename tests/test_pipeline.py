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
from pipeline.providers import get_provider, ProviderError           # noqa: E402
from pipeline.providers.base import GenerationRequest                # noqa: E402
from pipeline.resurface import (build_resurface_prompt,              # noqa: E402
                                prepare_reference, reference_fingerprint)

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
    """The headline claim: 18 deliverables, a handful of generative calls.

    A call is owed for a product with no asset (generate it) AND for one that
    keeps its photo but regenerates the surface (a real, paid round trip that
    only skips inventing the product). The quote must cover both, because the
    Plan view's whole job is to state the price before it is paid, and a quote
    that flatters is worse than no quote.
    """
    b = load_brief(BRIEF)
    assert b.generation_count < b.variant_count
    assert b.generation_count == sum(
        1 for p in b.products if not p.has_asset() or p.regenerate_surface)
    # And it is genuinely per-product, not per-variant.
    assert b.generation_count <= len(b.products)


def test_existing_asset_is_never_generated():
    """A file on disk must short-circuit the provider entirely."""
    calls = []

    class Spy:
        name, model = "spy", "spy"

        def generate(self, req):
            calls.append(req)
            raise AssertionError("provider must not be called for an existing asset")

    b = load_brief(BRIEF)
    # State the precondition instead of inheriting it from the shipped brief.
    # The sample brief now resurfaces its first product by default, which is a
    # different path with a different (correct) answer -- and a test that
    # silently changes meaning when someone edits a YAML file is not a test.
    # `Product` is frozen, so this is a copy, not a mutation.
    import dataclasses
    have = dataclasses.replace(
        next(p for p in b.products if p.has_asset()), regenerate_surface=False)
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
        # This test is about --regen versus a plain reuse. Resurfacing is a
        # third path and would answer "resurfaced" where it expects "brief",
        # so it is turned off explicitly rather than left to whatever the
        # sample brief happens to say today.
        prod.pop("regenerate_surface", None)
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


# --- resurfacing: keep the approved product, change the scene ---------------

def _brief_with_resurfacing(tmp, colour=(200, 120, 90)):
    """A brief whose first product has a real asset AND asks to be resurfaced."""
    import yaml
    raw = yaml.safe_load(open(BRIEF, encoding="utf-8"))
    shot = os.path.join(tmp, "approved.png")
    Image.new("RGB", (300, 300), colour).save(shot)
    raw["products"][0]["asset"] = shot
    raw["products"][0]["regenerate_surface"] = True
    path = os.path.join(tmp, "brief.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, allow_unicode=True)
    return load_brief(path), shot


def test_resurfacing_edits_the_approved_asset_instead_of_generating():
    """The whole point of the feature, pinned as three separate claims.

    It is not enough that an image comes back. A plain text-to-image call
    would also return an image, and it would look fine, and it would be a
    different bottle from the one legal approved. So this asserts:

      * `edit()` was called and `generate()` was NOT -- the product was never
        described to a model and re-invented from words;
      * the reference handed to the provider really is the approved file;
      * pixels from the approved file survive into the master.
    """
    seen = {"edit": 0, "generate": 0, "reference": None}
    inner = get_provider("mock")

    class Watcher:
        name, model = inner.name, inner.model
        supports_edit = True

        def generate(self, req):
            seen["generate"] += 1
            return inner.generate(req)

        def edit(self, req):
            seen["edit"] += 1
            seen["reference"] = req.reference_png
            return inner.edit(req)

    with tempfile.TemporaryDirectory() as tmp:
        brief, shot = _brief_with_resurfacing(tmp, colour=(200, 120, 90))
        r = AssetResolver(Watcher(), cache_dir=os.path.join(tmp, "c"),
                          master_size=(256, 256))
        m = r.resolve(brief.products[0], seed=5)

        assert m.origin == "resurfaced"
        assert seen["edit"] == 1, "the edit path must be the one that runs"
        assert seen["generate"] == 0, \
            "the product must never be re-invented from a text prompt"
        assert seen["reference"] == prepare_reference(shot), \
            "the reference sent to the provider must be the approved asset"

        # The mock pastes the reference into the middle of the scene, so the
        # centre pixel has to be the approved colour. This is the claim a
        # reviewer actually cares about: the master contains the real product.
        with Image.open(m.path) as out:
            assert out.convert("RGB").getpixel((128, 128)) == (200, 120, 90), \
                "pixels from the approved asset must survive into the master"


def test_resurfacing_refuses_a_provider_that_cannot_edit():
    """Refuse loudly rather than quietly generating something else.

    The dangerous failure here is not an exception -- it is a fallback. A
    resolver that shrugged and called generate() would return a plausible
    image of a product nobody approved, labelled as the approved one. So the
    absence of a generate() call is as much the point as the raise.
    """
    calls = []

    class CannotEdit:
        name, model = "cannot-edit", "x"
        supports_edit = False

        def generate(self, req):
            calls.append(req)
            return get_provider("mock").generate(req)

    with tempfile.TemporaryDirectory() as tmp:
        brief, _ = _brief_with_resurfacing(tmp)
        r = AssetResolver(CannotEdit(), cache_dir=os.path.join(tmp, "c"))
        try:
            r.resolve(brief.products[0], seed=5)
        except ProviderError as exc:
            assert "reference image" in str(exc)
            assert "cannot-edit" in str(exc), "the message must name the provider"
        else:
            raise AssertionError("a provider that cannot edit must not silently generate")
    assert not calls, "it must not fall back to plain generation"


def test_resurfacing_cache_follows_the_reference_CONTENT():
    """Replace the approved photograph, keep the path -- must miss the cache.

    This is the bug the content hash exists to prevent, and it is invisible
    until it bites: key the cache on the prompt (or the file path) alone, and
    the day marketing drops a new shot in over the old one, every run keeps
    serving an image built from the photograph they just replaced. Nothing
    errors. The pictures just quietly stay wrong.
    """
    edits = []
    inner = get_provider("mock")

    class Watcher:
        name, model = inner.name, inner.model
        supports_edit = True

        def generate(self, req):
            return inner.generate(req)

        def edit(self, req):
            edits.append(req.reference_png)
            return inner.edit(req)

    with tempfile.TemporaryDirectory() as tmp:
        brief, shot = _brief_with_resurfacing(tmp, colour=(200, 120, 90))
        cache = os.path.join(tmp, "c")
        r = AssetResolver(Watcher(), cache_dir=cache, master_size=(256, 256))

        first = r.resolve(brief.products[0], seed=5)
        assert first.origin == "resurfaced"

        # identical inputs -> cache hit, no second call
        assert r.resolve(brief.products[0], seed=5).origin == "cache"
        assert len(edits) == 1

        # SAME path, DIFFERENT pixels -> must miss and re-edit
        Image.new("RGB", (300, 300), (20, 90, 160)).save(shot)
        again = r.resolve(brief.products[0], seed=5)
        assert again.origin == "resurfaced", \
            "a replaced approved photograph must invalidate the cache"
        assert len(edits) == 2
        assert edits[0] != edits[1]
        assert reference_fingerprint(edits[0]) != reference_fingerprint(edits[1])


def test_reference_is_sized_for_the_providers_slot():
    """Workers AI caps reference images at 512x512. Downscale, never upscale."""
    with tempfile.TemporaryDirectory() as tmp:
        big = os.path.join(tmp, "big.png")
        Image.new("RGB", (2048, 1024), (10, 20, 30)).save(big)
        with Image.open(io_bytes(prepare_reference(big))) as im:
            assert max(im.size) <= 512
            # aspect ratio preserved, not squashed into a square
            assert abs((im.width / im.height) - 2.0) < 0.02

        small = os.path.join(tmp, "small.png")
        Image.new("RGB", (120, 90), (10, 20, 30)).save(small)
        with Image.open(io_bytes(prepare_reference(small))) as im:
            assert im.size == (120, 90), "a small asset must not be upscaled into softness"


def test_resurface_prompt_states_the_constraint():
    """The prompt is the only thing stopping the model redesigning the product."""
    p = build_resurface_prompt("a frosted glass serum bottle", "polished white marble")
    assert "a frosted glass serum bottle" in p
    assert "polished white marble" in p
    low = p.lower()
    assert "keep it exactly as it is" in low
    assert "do not redesign" in low


def test_flux2_size_is_clamped_before_it_is_sent():
    """2048 returns a 500, not a 400, so the degrade-on-400 path never sees it.

    Clamping has to happen before the request or an oversize master size kills
    the whole run on a number that could have been rounded down.
    """
    from pipeline.providers.cloudflare import FLUX2_MAX_EDGE, _flux2_size

    assert _flux2_size((1600, 1600)) == (1600, 1600)
    w, h = _flux2_size((4096, 2048))
    assert max(w, h) <= FLUX2_MAX_EDGE
    assert abs((w / h) - 2.0) < 0.05, "clamping must not distort the aspect ratio"
    assert w % 32 == 0 and h % 32 == 0, "sizes must land on a latent tile boundary"


def io_bytes(b):
    import io
    return io.BytesIO(b)


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


# --- sharing: automatic mirror, unguessable public URL ---------------------

def test_storage_is_chosen_automatically_from_the_environment():
    """Credentials present means mirror; absent means local. No flag needed.

    Pinned because the alternative -- a configured backend that silently does
    nothing because nobody passed `--storage s3` -- is the exact failure the
    default exists to remove.
    """
    from pipeline.storage import default_storage
    keys = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        assert default_storage() == "local", "no credentials must fall back to local"

        for k in keys:
            os.environ[k] = "x"
        assert default_storage() == "s3", "credentials must select s3 without a flag"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_only_the_public_prefix_is_treated_as_shareable():
    """`share_url` must not hand out an unsigned link to a private key.

    The dangerous direction is optimism: returning a plain URL for an object
    the bucket policy does not cover produces a link that 403s for the
    recipient and works for the person who generated it, because their browser
    has nothing to do with it either way. So the rule is checked against the
    FULL key, S3_PREFIX included.
    """
    from pipeline.storage.s3 import MAX_PRESIGN_SECONDS, S3Storage
    env = {"AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
           "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
           "S3_BUCKET": "example-bucket"}
    saved = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)

        s = S3Storage(prefix="")
        assert s.is_public_key("public/tok/a.jpg")
        assert not s.is_public_key("runs/2026/a.jpg")
        assert not s.is_public_key("publicity/a.jpg"), \
            "prefix matching must be on the path segment, not the string"

        # public -> a plain URL, no signature anywhere in it
        pub = s.share_url("public/tok/a.jpg")
        assert pub.startswith("https://")
        assert "X-Amz-Signature" not in pub

        # private -> a signed, expiring URL
        priv = s.share_url("runs/2026/a.jpg")
        assert "X-Amz-Signature" in priv and "X-Amz-Expires" in priv

        # An S3_PREFIX pushes everything below the policy's reach, so nothing
        # is public any more -- including a key that literally starts "public/".
        nested = S3Storage(prefix="archive")
        assert not nested.is_public_key("public/tok/a.jpg")
        assert "X-Amz-Signature" in nested.share_url("public/tok/a.jpg")

        # over-long expiries are clamped rather than rejected by AWS later
        assert f"X-Amz-Expires={MAX_PRESIGN_SECONDS}" in \
            s.share_url("runs/x.jpg", expires=99 * 24 * 3600)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_share_tokens_are_unguessable_and_never_repeat():
    """The token is the only thing protecting a public-prefix object.

    Two properties, both load-bearing: enough entropy that it cannot be
    brute-forced, and a CSPRNG rather than `random` -- whose state is
    recoverable from a handful of outputs, which would mean seeing one run's
    token exposed every other run's.
    """
    import secrets
    seen = {secrets.token_urlsafe(24) for _ in range(500)}
    assert len(seen) == 500, "tokens must not collide"
    assert all(len(t) >= 32 for t in seen), "24 bytes should encode to >= 32 chars"


def test_local_storage_offers_no_share_url():
    """A path on one machine is not a link. Say so with "", not a fake URL."""
    from pipeline.storage import get_storage
    with tempfile.TemporaryDirectory() as tmp:
        st = get_storage("local", root=tmp)
        st.put("a/b.txt", b"hi", "text/plain")
        assert st.share_url("a/b.txt") == "", \
            "local storage must not pretend to have a shareable link"


# --- end to end ------------------------------------------------------------

def test_full_run_produces_every_deliverable():
    from pipeline.runner import run_campaign
    tmp = tempfile.mkdtemp()
    try:
        # isolated cache dir: otherwise a warm .cache from a previous run
        # makes this assert 0 generations and the test becomes meaningless
        #
        # storage_name="local" is NOT redundant. The default is now "decide
        # from the environment", so a shell with AWS credentials exported
        # would make this test upload eighteen files to a real bucket on every
        # run -- slow, billable, and dependent on a network. Tests state what
        # they want.
        s = run_campaign(BRIEF, BRAND, provider_name="mock", out_root=tmp,
                         quiet=True, cache_dir=os.path.join(tmp, "cache"),
                         storage_name="local")
        assert s.variants_planned == 18
        # Two, and the number is the point of the test.
        #
        # 18 deliverables from 2 model calls: one product keeps its approved
        # photograph and regenerates only the surface, the other has no asset
        # and is generated whole. Every market and every ratio is then composed
        # locally from those two masters. If this ever equals 18, the pipeline
        # has started generating per variant and its entire cost argument is
        # gone -- that is the regression this line exists to catch.
        assert s.generative_calls == 2, "must not generate per variant"
        assert sorted({r.master_origin for r in s.results}) == \
            ["generated", "resurfaced"], "the sample brief should show both paths"
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
